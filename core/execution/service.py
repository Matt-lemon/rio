"""Single persistent user service; JSON IPC is local-only and bounded."""
from __future__ import annotations

import argparse
import fcntl
import json
import os
from pathlib import Path
import re
import signal
import socket
import socketserver
import stat
import struct
import threading
import time
import uuid

from .engine import Task
from .proactive import Suggestions
from .store import Store, private_directory
from .types import encoded, source_revision


class Application:
    def __init__(self, store, task=None, suggestions=None):
        self.store = store
        self.task = task or Task(store)
        self.suggestions = suggestions or Suggestions(store)
        self.presence = {}
        self.presence_lock = threading.RLock()
        self.source_revision = source_revision()

    def dispatch(self, data):
        if not isinstance(data, dict):
            raise ValueError('INVALID_REQUEST')
        op = data.get('op')
        fields = {
            'health': set(), 'catalog.list': {'search'}, 'catalog.describe': {'name'},
            'session.open': {'session'}, 'session.clear': {'session'},
            'task.submit': {'session', 'request'}, 'task.status': {'session', 'id'},
            'task.pause': {'session', 'id'}, 'task.resume': {'session', 'id'},
            'task.cancel': {'session', 'id'}, 'task.steer': {'session', 'id', 'request'},
            'task.events': {'session', 'after'}, 'tool.status': {'session', 'request_id'},
            'tool.history': {'session', 'request_id'},
            'session.presence': {'session', 'client', 'busy', 'voice_ready', 'user_input'},
            'session.detach': {'session', 'client'},
            'suggestions.list': {'session'}, 'suggestions.claim': {'session', 'client', 'mode'},
            'suggestions.delivered': {'session', 'id', 'audio_ok', 'mode'},
            'interests.list': {'session'}, 'interests.update': {'session', 'id', 'action'},
            'agency.ingest': {'kind', 'text', 'event_id'},
        }
        if op not in fields or set(data) - {'op'} - fields[op]:
            raise ValueError('PUBLIC_OPERATION_FORBIDDEN')
        if op == 'health':
            return {'ready': self.store.executor_valid(), 'version': 1, 'automatic_build': False, 'direct_docker': False,
                    'process_uid': os.geteuid(),
                    'docker_socket_access': os.access('/var/run/docker.sock', os.R_OK | os.W_OK),
                    'executor_token': self.task.executor_token,
                    'source_revision': self.source_revision}
        if op == 'catalog.list':
            search = data.get('search', '')
            if not isinstance(search, str) or len(search) > 200:
                raise ValueError('INVALID_SEARCH')
            return self.task.catalog.list(search)
        if op == 'catalog.describe':
            return self.task.catalog.describe(data['name'])
        if op == 'agency.ingest':
            return self.suggestions.ingest(data['kind'], data['text'], data['event_id'])
        session = data.get('session')
        if op == 'session.open' and session is None:
            session = uuid.uuid4().hex
        if not isinstance(session, str) or not re.fullmatch(r'[a-f0-9]{32}', session):
            raise ValueError('INVALID_SESSION')
        self.store.session(session)
        if op == 'session.open':
            pending = self.store.rows("SELECT id,state,request FROM tasks WHERE session_id=? AND state NOT IN ('COMPLETED','FAILED','CANCELLED') ORDER BY created DESC LIMIT 10", (session,))
            cursor = self.store.row('SELECT COALESCE(MAX(seq),0) AS n FROM events WHERE session_id=?', (session,))['n']
            return {'session': session, 'pending': pending, 'cursor': cursor}
        if op == 'session.clear':
            # Clearing context does not erase authority or unresolved effects.
            with self.store.transaction() as db:
                db.execute("UPDATE sessions SET messages='[]',summary='',browser='{}' WHERE id=?", (session,))
            self.store.event(session, None, 'SESSION_CLEARED', {})
            return {'cleared': True, 'task_history_preserved': True}
        if op == 'task.submit':
            return self.task.submit(session, data['request'])
        if op == 'task.status':
            return self.task.status(session, data.get('id'))
        if op in {'task.pause', 'task.resume', 'task.cancel'}:
            return getattr(self.task, op.split('.')[1])(session, data.get('id'))
        if op == 'task.steer':
            return self.task.steer(session, data['id'], data['request'])
        if op == 'task.events':
            after = data.get('after', 0)
            if type(after) is not int or after < 0:
                raise ValueError('INVALID_CURSOR')
            return self.task.events(session, after)
        if op == 'tool.status':
            return self.task.runner.status(data['request_id'], session).public()
        if op == 'tool.history':
            return self.task.runner.history(data['request_id'], session)
        if op.startswith('session.'):
            client = data.get('client')
            if not isinstance(client, str) or not re.fullmatch(r'[a-f0-9]{32}', client):
                raise ValueError('INVALID_CLIENT')
            with self.presence_lock:
                if op == 'session.detach':
                    self.presence.pop((session, client), None)
                    return {'detached': True}
                if any(type(data.get(k)) is not bool for k in ('busy', 'voice_ready', 'user_input')):
                    raise ValueError('INVALID_PRESENCE')
                now = self.store.clock()
                prior = self.presence.get((session, client), {})
                self.presence[(session, client)] = {'heartbeat': now, 'busy': data['busy'], 'voice_ready': data['voice_ready'],
                                                     'last_user': now if data['user_input'] else prior.get('last_user', 0)}
                return {'present': True}
        if op == 'suggestions.list':
            return self.suggestions.listing(session)
        if op == 'suggestions.claim':
            with self.presence_lock:
                presence = self.presence.get((session, data['client']))
                mode = data.get('mode', 'voice')
                if mode not in {'text', 'voice'}:
                    raise ValueError('INVALID_DELIVERY_MODE')
                return self.suggestions.claim_voice(session, presence) if mode == 'voice' else self.suggestions.claim_text(session, presence)
        if op == 'suggestions.delivered':
            if type(data['audio_ok']) is not bool:
                raise ValueError('INVALID_DELIVERY_RESULT')
            visible = self.store.row('SELECT suggestions.id FROM suggestions LEFT JOIN tasks ON tasks.id=suggestions.task_id WHERE suggestions.id=? AND (tasks.session_id=? OR suggestions.task_id LIKE ?)', (data['id'], session, 'event:%'))
            if not visible:
                raise ValueError('SUGGESTION_NOT_FOUND')
            self.store.event(session, None, 'SUGGESTION_AUDIO_RESULT', {'id': data['id'], 'audio_ok': data['audio_ok'], 'mode': data.get('mode', 'voice')})
            return {'recorded': True, 'physical_speaker_confirmation': False}
        if op == 'interests.list':
            return self.suggestions.interests(session)
        if op == 'interests.update':
            return self.suggestions.interests(session, data['id'], data['action'])
        raise ValueError('INVALID_REQUEST')


class CoreServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True
    block_on_close = False


def serve(root, path, *, application=None, proactive=True):
    os.umask(0o077)
    root = private_directory(root)
    path = Path(path)
    private_directory(path.parent)
    lock_fd = os.open(root/'service.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    store = application.store if application else Store(root)
    app = application or Application(store)
    store.interrupted()
    if path.exists() or path.is_symlink():
        info = path.lstat()
        if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ValueError('SOCKET_ALREADY_EXISTS')
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as test:
            try:
                test.connect(str(path))
            except ConnectionRefusedError:
                path.unlink()
            else:
                raise ValueError('SOCKET_ALREADY_EXISTS')

    class Handler(socketserver.StreamRequestHandler):
        def handle(self):
            self.request.settimeout(8)
            try:
                if hasattr(socket, 'SO_PEERCRED'):
                    _, uid, _ = struct.unpack('3i', self.request.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
                    if uid != os.getuid():
                        raise ValueError('LOCAL_OWNER_REQUIRED')
                raw = self.rfile.readline(65537)
                if len(raw) > 65536 or not raw.endswith(b'\n'):
                    raise ValueError('REQUEST_LIMIT')
                result = app.dispatch(json.loads(raw))
                response = {'ok': True, 'result': result}
                if len(encoded(response).encode()) > 524288:
                    raise ValueError('RESPONSE_LIMIT')
            except Exception as error:
                code = str(error) if isinstance(error, ValueError) and re.fullmatch('[A-Z_]{1,80}', str(error)) else 'REQUEST_FAILED'
                response = {'ok': False, 'code': code}
            try:
                self.wfile.write((encoded(response) + '\n').encode())
            except OSError:
                pass

    server = CoreServer(str(path), Handler)
    path.chmod(0o600)
    worker = threading.Thread(target=app.task.loop, daemon=True)
    worker.start()
    def watch():
        while not app.task.stopping.wait(1):
            # Foreground work wins. No model or search for unchanged state.
            if app.store.row("SELECT id FROM tasks WHERE state IN ('RUNNING','QUEUED') LIMIT 1"):
                continue
            try:
                app.suggestions.tick()
            except Exception:
                pass
    watcher = threading.Thread(target=watch, daemon=True) if proactive else None
    if watcher:
        watcher.start()
    def stop(signum, frame):
        app.task.stopping.set()
        app.task.wake.set()
        threading.Thread(target=server.shutdown, daemon=True).start()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, stop)
    try:
        server.serve_forever(poll_interval=0.2)
    finally:
        app.task.stopping.set()
        server.server_close()
        path.unlink(missing_ok=True)
        # Do not close a connection out from under an uncertain in-flight call.
        worker.join(timeout=2)
        if not worker.is_alive() and (watcher is None or not watcher.is_alive()):
            store.close()
        os.close(lock_fd)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--state', type=Path, default=Path.home()/'.local/state/rio/core')
    parser.add_argument('--socket', type=Path)
    parser.add_argument('--no-proactive', action='store_true')
    args = parser.parse_args()
    serve(args.state, args.socket or args.state/'service.sock', proactive=not args.no_proactive)


if __name__ == '__main__':
    main()
