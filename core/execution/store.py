"""Private durable task/session ledger. Summaries never carry execution authority."""
from __future__ import annotations

from contextlib import contextmanager
import os
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
        ''')

    @contextmanager
    def transaction(self):
        with self.lock:
            self.db.execute('BEGIN IMMEDIATE')
            try:
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
            row = db.execute('SELECT messages,summary FROM sessions WHERE id=?', (ident,)).fetchone()
            messages = json.loads(row['messages'])
            messages.append({'role': role, 'content': safe_text(content, 8000)})
            summary = row['summary']
            if len(messages) > 12:
                # Extractive, source-labeled context; no model can turn this into
                # a new user command or alter the separate call/task ledgers.
                older = messages[:-12]
                summary = (summary + '\n' + '\n'.join(m['role'] + ': ' + m['content'][:120] for m in older))[-800:]
                messages = messages[-12:]
            db.execute('UPDATE sessions SET messages=?,summary=?,updated=? WHERE id=?',
                       (encoded(messages), summary, self.clock(), ident))

    def browser(self, ident, value):
        with self.transaction() as db:
            db.execute('UPDATE sessions SET browser=? WHERE id=?', (encoded(value), ident))

    def event(self, session, task, kind, payload):
        with self.transaction() as db:
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

    def update_task(self, ident, **values):
        if not set(values) <= {'state', 'steps', 'elapsed', 'revision', 'answer', 'completion', 'direct'}:
            raise ValueError('INVALID_TASK_UPDATE')
        values['updated'] = self.clock()
        with self.transaction() as db:
            db.execute('UPDATE tasks SET ' + ','.join(k + '=?' for k in values) + ' WHERE id=?', (*values.values(), ident))

    def calls(self, ident):
        return [{**r, 'args': json.loads(r['args']), 'result': json.loads(r['result']) if r['result'] else None}
                for r in self.rows('SELECT * FROM calls WHERE task_id=? ORDER BY created,rowid', (ident,))]

    def interrupted(self):
        with self.transaction() as db:
            db.execute("UPDATE calls SET state='UNKNOWN' WHERE state='IN_FLIGHT'")
            db.execute("UPDATE tasks SET state='PAUSED',updated=? WHERE state IN ('RUNNING','QUEUED')", (self.clock(),))

    def close(self):
        with self.lock:
            self.db.close()
