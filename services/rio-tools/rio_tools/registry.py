from __future__ import annotations
from contextlib import contextmanager
import json
import os
from pathlib import Path
import sqlite3
import threading
import time
from core.tools.contracts import LOW_RISK, ToolError, canonical, digest, validate_package


class Registry:
    def __init__(self, root):
        self.root = Path(root).absolute()
        if self.root.is_symlink() or any(p.is_symlink() for p in self.root.parents):
            raise ToolError('STATE_SYMLINK')
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        if self.root.stat().st_mode & 0o077:
            raise ToolError('STATE_PERMISSION')
        self.packages = self.root/'packages'
        if self.packages.is_symlink():raise ToolError('PACKAGE_PATH_FORBIDDEN')
        self.packages.mkdir(mode=0o700, exist_ok=True)
        if self.packages.stat().st_mode & 0o077:raise ToolError('STATE_PERMISSION')
        db = self.root/'registry.sqlite3'
        if db.is_symlink():
            raise ToolError('STATE_SYMLINK')
        self.db = sqlite3.connect(db, timeout=10, isolation_level=None, check_same_thread=False)
        db.chmod(0o600)
        self.db.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        self.db.executescript('''
          PRAGMA journal_mode=WAL;
          CREATE TABLE IF NOT EXISTS versions(
            hash TEXT PRIMARY KEY,name TEXT NOT NULL,version TEXT NOT NULL,contract TEXT NOT NULL,
            state TEXT NOT NULL,validated INTEGER NOT NULL DEFAULT 0,approval TEXT,
            errors INTEGER NOT NULL DEFAULT 0,UNIQUE(name,version));
          CREATE TABLE IF NOT EXISTS active(name TEXT PRIMARY KEY,hash TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS validations(hash TEXT NOT NULL,at REAL NOT NULL,report TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS jobs(
            id TEXT PRIMARY KEY,kind TEXT NOT NULL,request_hash TEXT NOT NULL,package_hash TEXT,
            status TEXT NOT NULL,input TEXT,result TEXT,created REAL NOT NULL,updated REAL NOT NULL);
          CREATE TABLE IF NOT EXISTS builds(
            id TEXT PRIMARY KEY,capability TEXT NOT NULL,status TEXT NOT NULL,repairs INTEGER NOT NULL DEFAULT 0,
            package_hash TEXT,error TEXT,updated REAL NOT NULL);
          CREATE TABLE IF NOT EXISTS events(at REAL NOT NULL,hash TEXT,action TEXT NOT NULL,code TEXT);
          CREATE TABLE IF NOT EXISTS build_attempts(
            request_id TEXT NOT NULL,attempt INTEGER NOT NULL,metadata TEXT NOT NULL,
            PRIMARY KEY(request_id,attempt));
        ''')

    @contextmanager
    def transaction(self):
        with self.lock:
            self.db.execute('BEGIN IMMEDIATE')
            try:
                yield
                self.db.execute('COMMIT')
            except BaseException:
                self.db.execute('ROLLBACK')
                raise

    def candidate(self, package):
        c = validate_package(package)
        key = digest(package)
        with self.transaction():
            prior = self.db.execute('SELECT hash FROM versions WHERE name=? AND version=?',
                                    (c['name'], c['version'])).fetchone()
            if prior:
                if prior['hash'] != key:
                    raise ToolError('VERSION_ALREADY_EXISTS')
                return key
            path = self.packages/(key+'.json')
            try:
                fd = os.open(path, os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW, 0o400)
                with os.fdopen(fd, 'wb') as stream:
                    stream.write(canonical(package)); stream.flush(); os.fsync(stream.fileno())
            except FileExistsError:
                if self.load_package(key) != package:
                    raise ToolError('PACKAGE_HASH_MISMATCH')
            self.db.execute('INSERT INTO versions(hash,name,version,contract,state) VALUES(?,?,?,?,?)',
                            (key,c['name'],c['version'],canonical(c).decode(),'QUARANTINED'))
        return key

    def load_package(self, key):
        if not isinstance(key,str) or len(key) != 64 or any(x not in '0123456789abcdef' for x in key):
            raise ToolError('INVALID_HASH')
        if self.packages.is_symlink():
            raise ToolError('PACKAGE_PATH_FORBIDDEN')
        try:
            fd = os.open(self.packages/(key+'.json'), os.O_RDONLY|os.O_NOFOLLOW)
            with os.fdopen(fd,'rb') as stream:
                raw = stream.read(256*1024+1)
            package = json.loads(raw)
            validate_package(package)
            if digest(package) != key:
                raise ToolError('PACKAGE_HASH_MISMATCH')
            return package
        except (OSError,ValueError,TypeError):
            raise ToolError('PACKAGE_READ_FAILED') from None

    def validated(self, key, report):
        self.load_package(key)
        passed = report.get('static_pass') is True and report.get('fixed_tests_pass') is True and report.get('isolation_pass') is True
        with self.transaction():
            self.db.execute('INSERT INTO validations VALUES(?,?,?)',(key,time.time(),canonical(report).decode()))
            self.db.execute('UPDATE versions SET validated=?,state=? WHERE hash=?',
                            (int(passed),'PENDING_APPROVAL' if passed else 'QUARANTINED',key))
            # Revalidation failure cannot leave an old active pointer dispatchable.
            if not passed:
                self.db.execute('DELETE FROM active WHERE hash=?',(key,))

    def activate(self, key, *, human_scope=None):
        """Trusted admin/validator method; never exposed by the public socket."""
        c = self.load_package(key)['contract']
        permissions = set(c['permissions'])
        scope = LOW_RISK if human_scope is None else set(human_scope)
        if not permissions <= scope:
            raise ToolError('APPROVAL_REQUIRED')
        with self.transaction():
            row = self.db.execute('SELECT * FROM versions WHERE hash=?',(key,)).fetchone()
            if not row or not row['validated'] or row['state'] not in {'PENDING_APPROVAL','ACTIVE'}:
                raise ToolError('VALIDATION_REQUIRED')
            approval = {'package_hash':key,'permissions':sorted(permissions),
                        'source':'preapproved-low-risk' if human_scope is None else 'local-human-admin'}
            self.db.execute('UPDATE versions SET state=?,approval=?,errors=0 WHERE hash=?',
                            ('ACTIVE',canonical(approval).decode(),key))
            self.db.execute('INSERT INTO active VALUES(?,?) ON CONFLICT(name) DO UPDATE SET hash=excluded.hash',(c['name'],key))
            self.db.execute('INSERT INTO events VALUES(?,?,?,?)',(time.time(),key,'ACTIVATE',None))

    def describe(self, name, *, active_only=True):
        with self.lock:
            if active_only:
                row = self.db.execute('SELECT v.* FROM versions v JOIN active a ON a.hash=v.hash WHERE a.name=? AND v.state=?',(name,'ACTIVE')).fetchone()
            else:
                row = self.db.execute('SELECT * FROM versions WHERE name=? ORDER BY rowid DESC LIMIT 1',(name,)).fetchone()
            if not row:
                raise ToolError('TOOL_NOT_REGISTERED')
            return dict(row)|{'contract':json.loads(row['contract'])}

    def listing(self, search='', active_only=True):
        with self.lock:
            rows = self.db.execute('SELECT DISTINCT name FROM versions ORDER BY name').fetchall()
        result=[]
        for row in rows:
            try:
                item=self.describe(row['name'],active_only=active_only)
                if search.casefold() in (item['name']+' '+item['contract']['description']).casefold():
                    result.append(item)
            except ToolError:
                pass
        return result

    def suspend(self, key, state='DISABLED'):
        if state not in {'DISABLED','QUARANTINED'}:
            raise ToolError('INVALID_STATE')
        with self.transaction():
            self.db.execute('UPDATE versions SET state=? WHERE hash=?',(state,key))
            self.db.execute('DELETE FROM active WHERE hash=?',(key,))
            self.db.execute('INSERT INTO events VALUES(?,?,?,?)',(time.time(),key,state,None))

    def rollback(self, name, key):
        c=self.load_package(key)['contract']
        if c['name'] != name:
            raise ToolError('ROLLBACK_NAME_MISMATCH')
        with self.lock:
            row=self.db.execute('SELECT validated,approval,state FROM versions WHERE hash=?',(key,)).fetchone()
        if not row or not row['validated'] or not row['approval'] or row['state']=='QUARANTINED':
            raise ToolError('ROLLBACK_NOT_VERIFIED')
        approval=json.loads(row['approval'])
        if approval['package_hash']!=key or set(approval['permissions']) != set(c['permissions']):
            raise ToolError('APPROVAL_HASH_MISMATCH')
        with self.transaction():
            self.db.execute('UPDATE versions SET state=? WHERE hash=?',('PENDING_APPROVAL',key))
        self.activate(key,human_scope=approval['permissions'])

    def record_error(self,key):
        with self.transaction():
            self.db.execute('UPDATE versions SET errors=errors+1 WHERE hash=?',(key,))
            row=self.db.execute('SELECT errors FROM versions WHERE hash=?',(key,)).fetchone()
            if row and row['errors']>=3:
                self.db.execute('UPDATE versions SET state=? WHERE hash=?',('QUARANTINED',key))
                self.db.execute('DELETE FROM active WHERE hash=?',(key,))

    def close(self):
        self.db.close()
