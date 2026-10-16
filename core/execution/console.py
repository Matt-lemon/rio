"""Responsive terminal presentation; all execution lives in the service."""
from __future__ import annotations

import json
import os
from pathlib import Path
import queue
import threading
import tempfile
import time
import uuid

from .client import CoreClient
from .store import private_directory


def saved_session():
    root = private_directory(Path.home()/'.local/state/rio/core-client')
    path = root/'session.json'
    if not path.exists():
        return None, path
    info = path.lstat()
    if path.is_symlink() or info.st_uid != os.getuid() or info.st_mode & 0o077 or info.st_size > 1024:
        raise ValueError('PRIVATE_SESSION_REQUIRED')
    return json.loads(path.read_text()).get('session'), path


def save_session(path, session, cursor):
    fd, temporary = tempfile.mkstemp(prefix='.rio-session-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump({'session': session, 'cursor': cursor}, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def run(voice, *, client=None, read_input=None, one_shot=None):
    client = client or CoreClient()
    ident, path = saved_session()
    opened = client.request('session.open', session=ident)
    session, cursor = opened['session'], opened['cursor']
    if path.exists():
        saved = json.loads(path.read_text())
        if saved.get('session') == session and type(saved.get('cursor', 0)) is int:
            cursor = max(0, min(cursor, saved.get('cursor', 0)))
    save_session(path, session, cursor)
    connection = uuid.uuid4().hex
    incoming = queue.Queue()
    closed = threading.Event()
    active, busy, voice_ready = None, False, False
    last_heartbeat = 0
    if one_shot is None:
        print('리오 실행 코어에 연결했어. /tasks · /pause · /resume · /cancel · /suggestions · /interests · /exit')
        for pending in opened['pending']:
            print('저장된 작업:', pending['id'], pending['state'], pending['request'][:80])
        def reader():
            while not closed.is_set():
                try:
                    incoming.put((read_input() if read_input else input('You: ')).strip())
                except (EOFError, KeyboardInterrupt, StopIteration):
                    incoming.put('/exit')
                    return
        threading.Thread(target=reader, daemon=True).start()
    else:
        incoming.put(one_shot)
    try:
        while not closed.is_set():
            try:
                text = incoming.get(timeout=0.2)
            except queue.Empty:
                text = None
            if text:
                if busy and text in {'잠깐만', '그만', '그만해', '중지해줘', '작업 중지해줘', '멈춰'}:
                    text = '/pause'
                elif busy and text in {'취소해줘', '작업 취소해줘'}:
                    text = '/cancel'
                if text in {'/exit', '/quit'}:
                    if busy:
                        print('Rio: 연결만 닫고 요청한 작업은 계속 진행해. 다음 접속에서 /tasks로 확인할 수 있어.')
                    break
                parts = text.split(None, 1)
                op, tail = parts[0], parts[1] if len(parts) > 1 else None
                try:
                    if op in {'/pause', '/resume', '/cancel'}:
                        result = client.request('task.' + op[1:], session=session, **({'id': tail} if tail else {}))
                        print('Rio:', result.get('answer') or result['state'], result['id'])
                        if op == '/resume' and result['state'] == 'QUEUED':
                            active, busy = result['id'], True
                        elif result['id'] == active:
                            busy = False
                    elif op == '/tasks':
                        rows = client.request('task.status', session=session, **({'id': tail} if tail else {}))
                        for task in [rows] if isinstance(rows, dict) else rows:
                            print(task['id'], task['state'], task['request'][:100])
                            if task.get('answer'):
                                print('Rio:', task['answer'])
                    elif op == '/clear':
                        client.request('session.clear', session=session)
                        print('Rio: 대화 문맥은 비웠어. 실행·미확정 기록은 보존했어.')
                    elif op == '/suggestions':
                        for item in client.request('suggestions.list', session=session):
                            print('Rio:', item['text'])
                            for row in item['evidence'].get('results', []):
                                print(row['title'], row['url'])
                    elif op == '/interests':
                        if tail:
                            action, item = tail.split(None, 1)
                            rows = client.request('interests.update', session=session, id=item, action=action)
                        else:
                            rows = client.request('interests.list', session=session)
                        for item in rows:
                            print(item['id'], item['topic'], '제외' if item['excluded'] else '만료' if item.get('expired') else '유지' if item['kept'] else '작업 참고')
                    elif op in {'/status', '/system', '/memory'}:
                        if op == '/status':
                            print(client.request('health'))
                        elif op == '/system':
                            from rio_core import load_identity, build_system_prompt
                            print(build_system_prompt(load_identity()))
                        else:
                            from core.memory.nas_client import NasMemoryClient
                            print('기억 API:', NasMemoryClient().health())
                    elif active and busy:
                        client.request('task.steer', session=session, id=active, request=text)
                        print('Rio: 수정 요청을 저장했어. 다음 도구 실행 전에 반영할게.')
                    else:
                        task = client.request('task.submit', session=session, request=text)
                        active, busy = task['id'], True
                    client.request('session.presence', session=session, client=connection, busy=busy,
                                   voice_ready=voice_ready, user_input=True)
                except (ValueError, OSError):
                    print('Rio: 요청 전달이나 상태를 확인하지 못했어. 기존 실행기로 자동 재전송하지 않을게.')
                    if one_shot is not None:
                        return False
            try:
                before_cursor = cursor
                for event in client.request('task.events', session=session, after=cursor):
                    cursor = event['seq']
                    payload = event['payload']
                    if event['kind'] == 'TASK_FINISHED':
                        print('Rio:', payload['message'])
                        if event['task_id'] == active:
                            try:
                                if hasattr(voice, 'begin_utterance'):
                                    voice.begin_utterance()
                                voice.feed(payload['message'])
                                voice.flush()
                                voice.wait()
                                voice_ready = bool(getattr(voice, 'audio_succeeded', False))
                            except Exception:
                                voice_ready = False
                            busy = False
                            if one_shot is not None:
                                save_session(path, session, cursor)
                                return payload['state'] == 'COMPLETED'
                    elif event['kind'] == 'TASK_STOPPED':
                        print('Rio:', payload['message'])
                        if event['task_id'] == active:
                            busy = False
                if cursor != before_cursor:
                    save_session(path, session, cursor)
                if time.monotonic() - last_heartbeat >= 1:
                    client.request('session.presence', session=session, client=connection, busy=busy,
                                   voice_ready=voice_ready, user_input=False)
                    last_heartbeat = time.monotonic()
                if not busy:
                    mode = 'voice' if voice_ready else 'text'
                    item = client.request('suggestions.claim', session=session, client=connection, mode=mode)
                    if item:
                        print('Rio:', item['text'])
                        for row in item['evidence'].get('results', []):
                            print(row['title'], row['url'])
                        audio_ok = False
                        if mode == 'voice':
                            try:
                                if hasattr(voice, 'begin_utterance'):
                                    voice.begin_utterance()
                                voice.feed(item['text'])
                                voice.flush()
                                voice.wait()
                                audio_ok = bool(getattr(voice, 'audio_succeeded', False))
                            except Exception:
                                pass
                            voice_ready = audio_ok
                        client.request('suggestions.delivered', session=session, id=item['id'], audio_ok=audio_ok, mode=mode)
            except (ValueError, OSError):
                print('Rio: 코어 연결이 끊겼어. 다음 접속에서 저장된 작업을 확인해 줘. 재전송하지 않을게.')
                return False
    finally:
        closed.set()
        try:
            client.request('session.detach', session=session, client=connection)
        except (ValueError, OSError):
            pass
    return True
