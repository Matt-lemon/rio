import json
import queue
import threading
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


def test_exit_received_while_speaking_cannot_start_a_suggestion(tmp_path,monkeypatch):
    session='c'*32;path=tmp_path/'session.json'
    monkeypatch.setattr(console,'saved_session',lambda:(None,path))
    exit_queued=threading.Event();speaking=threading.Event();observed=[]
    base_queue=queue.Queue
    class InputQueue(base_queue):
        def put(self,item,*args,**kwargs):
            super().put(item,*args,**kwargs)
            if item=='/exit':exit_queued.set()
    monkeypatch.setattr(console.queue,'Queue',InputQueue)
    class Client:
        submitted=False
        def request(self,op,**args):
            observed.append((op,args))
            if op=='session.open':return {'session':session,'cursor':0,'pending':[]}
            if op=='task.submit':self.submitted=True;return {'id':'test_task'}
            if op=='task.events':
                return [{'seq':1,'task_id':'test_task','kind':'TASK_FINISHED','payload':{'state':'COMPLETED','message':'요청한 응답'}}] if self.submitted and args['after']==0 else []
            if op=='suggestions.claim':return {'id':'test_suggestion','text':'먼저 하는 제안','evidence':{}}
            return {}
    first=[True]
    def read():
        if first[0]:first[0]=False;return '짧게 대답해줘'
        assert speaking.wait(3);return '/exit'
    voice=Mock(audio_succeeded=True)
    def wait():speaking.set();assert exit_queued.wait(3)
    voice.wait.side_effect=wait
    assert console.run(voice,client=Client(),read_input=read)
    voice.feed.assert_called_once_with('요청한 응답')
    assert not any(op=='suggestions.claim' for op,_ in observed)
    assert observed[-1][0]=='session.detach'


def test_speaker_renews_busy_presence_until_audio_completes():
    pulses=[];renewed=threading.Event();voice=Mock(audio_succeeded=True)
    def presence():
        pulses.append(True)
        if len(pulses)>1:renewed.set()
    voice.wait.side_effect=lambda:renewed.wait(3)
    assert console.speak(voice,'모의 음성',presence=presence,incoming=queue.Queue(),
                         exit_requested=threading.Event(),input_gate=threading.RLock(),interval=.01)
    assert len(pulses)>=2
    count=len(pulses)
    assert not threading.Event().wait(.03)
    assert len(pulses)==count


def test_pending_correction_and_closed_session_skip_new_speech():
    incoming=queue.Queue();incoming.put('수정할게');voice=Mock(audio_succeeded=True)
    exit_requested=threading.Event()
    assert not console.speak(voice,'뒤늦은 제안',presence=lambda:None,incoming=incoming,
                             exit_requested=exit_requested,input_gate=threading.RLock())
    incoming.get();exit_requested.set()
    assert not console.speak(voice,'종료 뒤 제안',presence=lambda:None,incoming=incoming,
                             exit_requested=exit_requested,input_gate=threading.RLock())
    voice.feed.assert_not_called()
