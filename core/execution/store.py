"""Private durable task/session ledger. Summaries never carry execution authority."""
from __future__ import annotations

from contextlib import contextmanager
import os
import fcntl
from pathlib import Path
import sqlite3
import stat
import threading
import time
import uuid

import json
from .types import encoded, safe_text


def private_directory(path):
    path = Path(path).absolute()
    if any(p.is_symlink() for p in [path, *path.parents]):
        raise ValueError('STATE_SYMLINK')
    path.mkdir(parents=True, mode=0o700, exist_ok=True)
    info = path.stat()
    if info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ValueError('PRIVATE_STATE_REQUIRED')
    return path


class Store:
    def __init__(self, root, clock=time.time):
        self.root = private_directory(root)
        self.clock = clock
        path = self.root / 'core.sqlite3'
        fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077 or info.st_nlink != 1:
                raise ValueError('PRIVATE_DATABASE_REQUIRED')
        finally:
            os.close(fd)
        self.lock = threading.RLock()
        self.executor_token = None
        self.db = sqlite3.connect(path, timeout=10, check_same_thread=False, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.executescript('''
          PRAGMA journal_mode=WAL;
          CREATE TABLE IF NOT EXISTS sessions(id TEXT PRIMARY KEY,messages TEXT NOT NULL,
            summary TEXT NOT NULL DEFAULT '',browser TEXT NOT NULL DEFAULT '{}',updated REAL NOT NULL);
          CREATE TABLE IF NOT EXISTS tasks(id TEXT PRIMARY KEY,session_id TEXT NOT NULL,request TEXT NOT NULL,
            completion TEXT NOT NULL,state TEXT NOT NULL,steps INTEGER NOT NULL DEFAULT 0,
            elapsed REAL NOT NULL DEFAULT 0,created REAL NOT NULL,updated REAL NOT NULL,
            revision INTEGER NOT NULL DEFAULT 0,answer TEXT NOT NULL DEFAULT '',direct INTEGER NOT NULL DEFAULT 0);
          CREATE TABLE IF NOT EXISTS calls(id TEXT PRIMARY KEY,task_id TEXT NOT NULL,name TEXT NOT NULL,
            args TEXT NOT NULL,revision TEXT NOT NULL,effect TEXT NOT NULL,state TEXT NOT NULL,
            result TEXT,created REAL NOT NULL,updated REAL NOT NULL);
          CREATE TABLE IF NOT EXISTS events(seq INTEGER PRIMARY KEY AUTOINCREMENT,task_id TEXT,
            session_id TEXT NOT NULL,kind TEXT NOT NULL,payload TEXT NOT NULL,created REAL NOT NULL);
          CREATE TABLE IF NOT EXISTS interests(id TEXT PRIMARY KEY,task_id TEXT NOT NULL,topic TEXT NOT NULL,
            source TEXT NOT NULL,signature TEXT NOT NULL,kept INTEGER NOT NULL DEFAULT 0,
            excluded INTEGER NOT NULL DEFAULT 0,created REAL NOT NULL,checked REAL NOT NULL DEFAULT 0);
          CREATE TABLE IF NOT EXISTS suggestions(id TEXT PRIMARY KEY,task_id TEXT NOT NULL,
            text TEXT NOT NULL,evidence TEXT NOT NULL,created REAL NOT NULL,delivered REAL);
          CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS call_phases(seq INTEGER PRIMARY KEY AUTOINCREMENT,
            call_id TEXT NOT NULL,phase TEXT NOT NULL,state TEXT,source TEXT NOT NULL,
            owner_token INTEGER NOT NULL,fence INTEGER NOT NULL,created REAL NOT NULL);
        ''')
        # Additive migration: retain all old calls, IDs, results and events.
        with self.transaction() as db:
            for table, columns in {'tasks': {'owner_token': 'INTEGER NOT NULL DEFAULT 0', 'fence': 'INTEGER NOT NULL DEFAULT 0'},
                                   'calls': {'owner_token': 'INTEGER NOT NULL DEFAULT 0', 'fence': 'INTEGER NOT NULL DEFAULT 0',
                                             'phase': "TEXT NOT NULL DEFAULT 'LEGACY_UNCLASSIFIED'"}}.items():
                existing = {r['name'] for r in db.execute('PRAGMA table_info(' + table + ')')}
                for name, declaration in columns.items():
                    if name not in existing:
                        db.execute('ALTER TABLE ' + table + ' ADD COLUMN ' + name + ' ' + declaration)

    @contextmanager
    def dispatch_lock(self):
        """Takeover waits for a live adapter boundary; process death releases it."""
        fd = os.open(self.root/'dispatch.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            os.close(fd)

    def activate_executor(self):
        if self.executor_token is not None:
            return self.executor_token
        with self.dispatch_lock(), self.transaction() as db:
            row = db.execute("SELECT value FROM meta WHERE key='executor_token'").fetchone()
            token = int(row['value']) + 1 if row else 1
            db.execute("INSERT INTO meta VALUES('executor_token',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (str(token),))
        self.executor_token = token
        return token

    def assert_executor(self, db):
        if self.executor_token is not None:
            row = db.execute("SELECT value FROM meta WHERE key='executor_token'").fetchone()
            if row is None or int(row['value']) != self.executor_token:
                raise ValueError('EXECUTOR_FENCED')

    def executor_valid(self):
        with self.lock:
            try:
                self.assert_executor(self.db)
                return self.executor_token is not None
            except ValueError:
                return False

    def assert_claim(self, db, claim, *, running=True, revision=True):
        self.assert_executor(db)
        row = db.execute('SELECT * FROM tasks WHERE id=?', (claim['id'],)).fetchone()
        if (row is None or row['owner_token'] != self.executor_token or row['owner_token'] != claim['owner_token'] or row['fence'] != claim['fence']
                or (running and row['state'] != 'RUNNING') or (revision and row['revision'] != claim['revision'])):
            raise ValueError('TASK_FENCED')

    def claim_task(self, ident):
        with self.transaction() as db:
            row = db.execute('SELECT state FROM tasks WHERE id=?', (ident,)).fetchone()
            if not row or row['state'] != 'QUEUED':
                return None
            db.execute("UPDATE tasks SET state='RUNNING',owner_token=?,fence=fence+1,updated=? WHERE id=?",
                       (self.executor_token, self.clock(), ident))
            return dict(db.execute('SELECT * FROM tasks WHERE id=?', (ident,)).fetchone())

    def owns(self, claim, *, running=True, revision=True):
        try:
            with self.lock:
                self.assert_claim(self.db, claim, running=running, revision=revision)
            return True
        except ValueError:
            return False

    @contextmanager
    def transaction(self, claim=None, *, running=True, revision=True):
        with self.lock:
            self.db.execute('BEGIN IMMEDIATE')
            try:
                self.assert_executor(self.db)
                if claim is not None:
                    self.assert_claim(self.db, claim, running=running, revision=revision)
                yield self.db
                self.db.execute('COMMIT')
            except BaseException:
                self.db.execute('ROLLBACK')
                raise

    def row(self, sql, args=()):
        with self.lock:
            row = self.db.execute(sql, args).fetchone()
            return dict(row) if row else None

    def rows(self, sql, args=()):
        with self.lock:
            return [dict(r) for r in self.db.execute(sql, args).fetchall()]

    def session(self, ident):
        row = self.row('SELECT * FROM sessions WHERE id=?', (ident,))
        if not row:
            with self.transaction() as db:
                db.execute('INSERT OR IGNORE INTO sessions(id,messages,updated) VALUES(?,?,?)', (ident, '[]', self.clock()))
            row = self.row('SELECT * FROM sessions WHERE id=?', (ident,))
        return {**row, 'messages': json.loads(row['messages']), 'browser': json.loads(row['browser'])}

    def append_turn(self, ident, role, content):
        if role not in {'user', 'assistant'}:
            raise ValueError('INVALID_ROLE')
        with self.transaction() as db:
            self.append_turn_db(db, ident, role, content)

    def append_turn_db(self, db, ident, role, content):
        row = db.execute('SELECT messages,summary FROM sessions WHERE id=?', (ident,)).fetchone()
        messages = json.loads(row['messages'])
        messages.append({'role': role, 'content': safe_text(content, 8000)})
        summary = row['summary']
        if len(messages) > 12:
            # Extractive context never alters the separate authority ledgers.
            older = messages[:-12]
            summary = (summary + '\n' + '\n'.join(m['role'] + ': ' + m['content'][:120] for m in older))[-800:]
            messages = messages[-12:]
        db.execute('UPDATE sessions SET messages=?,summary=?,updated=? WHERE id=?',
                   (encoded(messages), summary, self.clock(), ident))

    def browser(self, ident, value, *, claim=None):
        with self.transaction(claim, running=False, revision=False) as db:
            db.execute('UPDATE sessions SET browser=? WHERE id=?', (encoded(value), ident))

    def event(self, session, task, kind, payload, *, claim=None):
        with self.transaction(claim) as db:
            db.execute('INSERT INTO events(task_id,session_id,kind,payload,created) VALUES(?,?,?,?,?)',
                       (task, session, kind, encoded(payload), self.clock()))

    def events(self, session, after=0):
        return [{**r, 'payload': json.loads(r['payload'])} for r in self.rows(
            'SELECT * FROM events WHERE session_id=? AND seq>? ORDER BY seq LIMIT 100', (session, after))]

    def create_task(self, session, request, *, direct=False):
        ident = uuid.uuid4().hex
        now = self.clock()
        with self.transaction() as db:
            if db.execute("SELECT COUNT(*) FROM tasks WHERE state IN ('QUEUED','RUNNING')").fetchone()[0] >= 16:
                raise ValueError('TASK_QUEUE_FULL')
            db.execute('INSERT INTO tasks(id,session_id,request,completion,state,created,updated,direct) VALUES(?,?,?,?,?,?,?,?)',
                       (ident, session, safe_text(request, 2000), safe_text(request, 2000), 'QUEUED', now, now, int(direct)))
        return self.task(ident, session)

    def task(self, ident, session=None):
        row = self.row('SELECT * FROM tasks WHERE id=?', (ident,))
        if row is None or (session is not None and row['session_id'] != session):
            raise ValueError('TASK_NOT_FOUND')
        return row

    def update_task(self, ident, *, claim=None, running=True, **values):
        if not set(values) <= {'state', 'steps', 'elapsed', 'revision', 'answer', 'completion', 'direct'}:
            raise ValueError('INVALID_TASK_UPDATE')
        values['updated'] = self.clock()
        with self.transaction(claim, running=running) as db:
            db.execute('UPDATE tasks SET ' + ','.join(k + '=?' for k in values) + ' WHERE id=?', (*values.values(), ident))

    def calls(self, ident):
        return [{**r, 'args': json.loads(r['args']), 'result': json.loads(r['result']) if r['result'] else None}
                for r in self.rows('SELECT * FROM calls WHERE task_id=? ORDER BY created,rowid', (ident,))]

    def interrupted(self):
        with self.transaction() as db:
            db.execute("UPDATE calls SET state='UNKNOWN' WHERE state IN ('IN_FLIGHT','PREPARED')")
            db.execute("UPDATE tasks SET state='PAUSED',updated=? WHERE state IN ('RUNNING','QUEUED')", (self.clock(),))

    def close(self):
        with self.lock:
            self.db.close()
