"""Global, process-safe home dispatch fence shared by both providers.

The flock covers admission through dispatch/observation. SQLite intent survives
SIGKILL; releasing a dead process's flock never authorizes a second send.
Legacy provider ledgers remain authoritative and are only read here.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import time


class DispatchBlocked(ValueError):
    pass


def primary_path():
    return Path(os.getenv('RIO_HOME_LEDGER', str(Path.home()/'.local/state/rio/home/example')))


def secondary_paths():
    return (Path.home()/'.local/state/rio/smartthings-direct/commands.sqlite3',
            Path(os.getenv('RIO_HOME_BACKUP_LEDGER', str(Path.home()/'.local/state/rio/home-backup/commands.sqlite3'))))


def digest(payload):
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def private_fd(path):
    fd = os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        os.close(fd)
        raise DispatchBlocked('DISPATCH_STORAGE_UNSAFE')
    return fd


class DispatchGuard:
    def __init__(self, primary, secondary=()):
        if not Path(primary).is_absolute():
            raise DispatchBlocked('ABSOLUTE_HOME_LEDGER_REQUIRED')
        self.primary = Path(primary).absolute()
        self.paths = tuple(dict.fromkeys((self.primary, *map(Path, secondary), *secondary_paths())))
        self.path = self.primary.with_name('dispatch-coordinator.sqlite3')
        self.lock = None
        self.db = None
        self.key = None

    def __enter__(self):
        try:
            self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            info = self.path.parent.lstat()
            if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
                raise DispatchBlocked('DISPATCH_STORAGE_UNSAFE')
            self.lock = private_fd(self.path.with_suffix('.lock'))
            try:
                fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise DispatchBlocked('HOME_DISPATCH_BUSY') from None
            existed = self.path.exists()
            os.close(private_fd(self.path))
            self.db = sqlite3.connect(self.path, timeout=2)
            if existed and self.db.execute('PRAGMA user_version').fetchone()[0] != 1:
                raise DispatchBlocked('DISPATCH_SCHEMA_INVALID')
            self.db.execute('CREATE TABLE IF NOT EXISTS dispatches '
                '(id TEXT PRIMARY KEY, digest TEXT NOT NULL, provider TEXT NOT NULL, '
                'phase TEXT NOT NULL, attempted REAL, answer TEXT NOT NULL, terminal INTEGER NOT NULL)')
            self.db.execute('CREATE TABLE IF NOT EXISTS ledger_paths (path TEXT PRIMARY KEY)')
            self.db.execute('CREATE TABLE IF NOT EXISTS coordinator_meta (key TEXT PRIMARY KEY,value TEXT NOT NULL)')
            self.db.execute("INSERT OR IGNORE INTO coordinator_meta VALUES ('primary_ledger',?)",(str(self.primary),))
            if self.db.execute("SELECT value FROM coordinator_meta WHERE key='primary_ledger'").fetchone()[0]!=str(self.primary):
                raise DispatchBlocked('PRIMARY_LEDGER_CHANGED')
            self.db.execute('PRAGMA user_version=1')
            self.db.commit()
            for (prior,) in self.db.execute('SELECT path FROM ledger_paths'):
                if not Path(prior).is_file():
                    raise DispatchBlocked('KNOWN_LEDGER_MISSING')
                if Path(prior) not in self.paths:
                    self.paths+= (Path(prior),)
            return self
        except Exception:
            self.__exit__(None, None, None)
            raise

    def __exit__(self, *args):
        if self.db is not None:
            self.db.close()
            self.db = None
        if self.lock is not None:
            os.close(self.lock)
            self.lock = None

    def _open_legacy(self, path):
        if not path.exists() and not path.is_symlink():
            return None
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise DispatchBlocked('LEGACY_LEDGER_UNSAFE')
        db = sqlite3.connect(path.as_uri()+'?mode=ro', uri=True, timeout=2)
        self.db.execute('INSERT OR IGNORE INTO ledger_paths VALUES (?)', (str(path),))
        self.db.commit()
        return db

    def _primary_resolved(self, db, old, key, payload):
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        row = db.execute('SELECT status FROM light_commands WHERE key=?', (old,)).fetchone()
        if row and row[0] in ('SUCCEEDED', 'REJECTED'):
            return True
        if not row or row[0]!='UNKNOWN':
            return False
        if 'human_command_reviews' in tables and db.execute(
                "SELECT 1 FROM human_command_reviews WHERE key=? AND observation IN "
                "('USER_CONFIRMED_NOT_APPLIED','SUPERSEDED_BY_HUMAN_CONFIRMED_POWER')", (old,)).fetchone():
            return True
        return ('human_retry_authorizations' in tables and db.execute(
            'SELECT 1 FROM human_retry_authorizations WHERE prior_key=? AND next_key=? AND digest=?',
            (old, key, digest(payload))).fetchone() is not None)

    @staticmethod
    def _secondary_observed(db, old, target=None):
        tables={r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not {'smartthings_observations','smartthings_attempts','smartthings_requests'} <= tables:
            return False
        row=db.execute('SELECT o.target,o.device_timestamp,o.observed_at,a.attempted_at,r.binding '
            'FROM smartthings_observations o JOIN smartthings_attempts a '
            'ON o.request_id=a.request_id AND o.target=a.target JOIN smartthings_requests r '
            'ON r.request_id=a.request_id WHERE o.request_id=? AND o.reported_state=a.action '
            "AND o.original_state='UNKNOWN'",(old,)).fetchone()
        if row is None or (target is not None and row[0]!=target):return False
        from datetime import datetime
        try:
            changed=datetime.fromisoformat(row[1].replace('Z','+00:00'))
            return (json.loads(row[4])['device_id']==row[0] and changed.tzinfo is not None
                    and row[3]<changed.timestamp()<=row[2])
        except (ValueError,TypeError,KeyError,OverflowError):
            return False

    def admit(self, key, provider, payload):
        if not isinstance(key, str) or not re.fullmatch(r'[A-Za-z0-9_-]{8,100}', key):
            return {'status': 'REJECTED', 'code': 'INVALID_IDEMPOTENCY_KEY'}
        fingerprint = digest({'provider': provider, 'payload': payload})
        prior = self.db.execute('SELECT digest,answer FROM dispatches WHERE id=?', (key,)).fetchone()
        if prior:
            return (json.loads(prior[1]) if prior[0] == fingerprint else
                    {'status': 'REJECTED', 'code': 'IDEMPOTENCY_CONFLICT'})
        with_primary = None
        opened = []
        try:
            for path in self.paths:
                db = self._open_legacy(path.absolute())
                if db is None:
                    continue
                opened.append(db)
                tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                if path == self.primary:
                    with_primary = db
                    if 'light_commands' not in tables:
                        raise DispatchBlocked('LEGACY_SCHEMA_INVALID')
                    for (old,) in db.execute("SELECT key FROM light_commands WHERE status IN ('DISPATCHING','DISPATCHED','UNKNOWN')"):
                        if old != key and self._primary_resolved(db, old, key, payload):
                            continue
                        raise DispatchBlocked('PRIMARY_UNRESOLVED')
                else:
                    if not {'wake_requests', 'shutdown_requests'} <= tables:
                        raise DispatchBlocked('LEGACY_SCHEMA_INVALID')
                    for table in ('wake_requests', 'shutdown_requests'):
                        for old, target, state in db.execute('SELECT id,target,state FROM '+table+
                                " WHERE state IN ('DISPATCHING','ACCEPTED','UNKNOWN','OBSERVED_SWITCH_ON',"
                                "'OBSERVED_SWITCH_OFF','OBSERVED_REACHABLE','OBSERVED_OFF_UNREACHABLE')"):
                            if state=='UNKNOWN' and self._secondary_observed(db,old,target):
                                continue
                            raise DispatchBlocked('SECONDARY_UNRESOLVED')
            for old, selected in self.db.execute('SELECT id,provider FROM dispatches WHERE terminal=0'):
                if selected == 'google-home' and with_primary and self._primary_resolved(with_primary, old, key, payload):
                    continue
                if selected=='smartthings' and any(self._secondary_observed(db,old) for db in opened if db is not with_primary):
                    continue
                raise DispatchBlocked('PREVIOUS_RESULT_UNRESOLVED')
        except sqlite3.Error:
            raise DispatchBlocked('LEGACY_LEDGER_UNREADABLE') from None
        finally:
            for db in opened:
                db.close()
        answer = {'status': 'UNKNOWN', 'code': 'EXECUTION_UNCERTAIN_DO_NOT_RETRY', 'request_id': key}
        self.db.execute('INSERT INTO dispatches VALUES (?,?,?,?,?,?,0)',
            (key, fingerprint, provider, 'PREPARED', None, json.dumps(answer)))
        self.db.commit()
        self.key = key
        return None

    def attempted(self):
        self.db.execute("UPDATE dispatches SET phase='ATTEMPTED',attempted=? WHERE id=? AND phase='PREPARED'", (time.time(), self.key))
        self.db.commit()

    def finish(self, answer, *, terminal):
        self.db.execute('UPDATE dispatches SET phase=?,answer=?,terminal=? WHERE id=?',
            ('RESULT_RECORDED', json.dumps(answer), int(terminal), self.key))
        self.db.commit()


def dispatch_primary(registry, key, payload, preflight, send):
    """Common GH light/device entry point; never selects another provider."""
    attempted=False
    try:
        with DispatchGuard(registry.ledger_path) as fence:
            if registry.ledger.known(key):
                return registry.ledger.reserve(key,payload)
            registry.ledger.refresh_dispatched(registry.transport.result)
            prior=fence.admit(key,'google-home',payload)
            if prior is not None:
                return prior
            prior=registry.ledger.reserve(key,payload)
            if prior is not None:
                fence.finish(prior,terminal=prior.get('status')=='REJECTED')
                return prior
            try:
                check=preflight()
                validator=getattr(registry,'revalidate',None)
                check=check is True and (validator is None or validator() is True)
            except Exception:
                check=False
            if not check:
                answer={'status':'REJECTED','code':'BINDING_OR_POLICY_CHANGED','request_id':key}
                registry.ledger.record(key,answer)
                fence.finish(answer,terminal=True)
                return answer
            fence.attempted()
            attempted=True
            try:
                response=send()
            except Exception:
                response={}
            from .tools import HomeToolRegistry
            answer=HomeToolRegistry.answer(response,key)
            if not isinstance(response,dict) or response.get('id')!=key or response.get('http_status') is not None:
                answer={'status':'UNKNOWN','code':'UNVERIFIED_GATEWAY_RESPONSE','request_id':key,'physical_state_confirmed':False}
            registry.ledger.record(key,answer)
            fence.finish(answer,terminal=answer['status'] in {'SUCCEEDED','REJECTED'})
            return answer
    except (DispatchBlocked,sqlite3.Error,OSError) as error:
        code=str(error) if isinstance(error,DispatchBlocked) else 'DISPATCH_STORAGE_UNAVAILABLE'
        if code=='PRIMARY_UNRESOLVED':code='PREVIOUS_RESULT_UNRESOLVED'
        return {'status':'UNKNOWN' if attempted else 'REJECTED','code':code,'request_id':key}
