import json
from unittest.mock import Mock

from core.execution import console


def test_reconnect_shows_offline_completion_once_as_text_and_saves_cursor(tmp_path, monkeypatch, capsys):
    session = 'a'*32
    path = tmp_path/'session.json'
    path.write_text(json.dumps({'session': session, 'cursor': 5}))
    path.chmod(0o600)
    monkeypatch.setattr(console, 'saved_session', lambda: (session, path))
    observed = []
    class Client:
        def request(self, op, **args):
            observed.append((op, args))
            if op == 'session.open':
                return {'session': session, 'cursor': 9, 'pending': []}
            if op == 'task.events':
                assert args['after'] == 5
                return [{'seq': 9, 'task_id': 'completed-while-away', 'kind': 'TASK_FINISHED',
                         'payload': {'state': 'COMPLETED', 'message': '접속을 끊은 동안 완료된 결과'}}]
            if op == 'suggestions.claim':
                return None
            return {'ready': True}
    voice = Mock()
    commands = iter(['/status', '/exit'])
    assert console.run(voice, client=Client(), read_input=lambda: next(commands))
    assert '접속을 끊은 동안 완료된 결과' in capsys.readouterr().out
    assert json.loads(path.read_text())['cursor'] == 9
    voice.feed.assert_not_called()
    assert not any(op == 'task.submit' for op, _ in observed)


def test_cursor_file_is_private_and_written_atomically(tmp_path):
    path = tmp_path/'session.json'
    console.save_session(path, 'a'*32, 100)
    assert json.loads(path.read_text()) == {'session': 'a'*32, 'cursor': 100}
    assert path.stat().st_mode & 0o077 == 0
    assert not list(tmp_path.glob('.rio-session-*'))
