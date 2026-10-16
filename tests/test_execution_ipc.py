"""Real-process IPC/crash recovery. Only a private file simulates an effect."""
import os
from pathlib import Path
import subprocess
import sys
import time

from core.execution.client import CoreClient


FAKE_SERVER = '''
import sys,time
from pathlib import Path
from core.execution.store import Store
from core.execution.engine import Task
from core.execution.catalog import Catalog
from core.execution.service import Application,serve
from core.execution.types import ToolResult,spec,enum
root=Path(sys.argv[1]); marker=root/'sent.txt'
class Adapter:
 def call(self,name,args,key,browser):
  if name=='home.light':
   with marker.open('a') as f:f.write(key+'\\n');f.flush()
   time.sleep(60)
  return ToolResult('OBSERVED','read result',request_id=key)
 def status(self,name,args,key):
  return ToolResult('SUCCEEDED','SDK acknowledged',request_id=key,confirmation='SDK_ACK')
catalog=Catalog(provider=lambda:{'home.light':spec('home.light','simulated effect','control',target=enum('거실'),action=enum('on','off'))})
store=Store(root)
task=Task(store,catalog,Adapter(),planner=lambda p:{'kind':'done'},responder=lambda *a:'observed result')
serve(root,root/'s.sock',application=Application(store,task),proactive=False)
'''


def wait_for(check, seconds=5):
    until = time.monotonic() + seconds
    while time.monotonic() < until:
        try:
            value = check()
            if value:
                return value
        except (OSError, ValueError):
            pass
        time.sleep(0.02)
    raise AssertionError('bounded process check did not complete')


def test_actual_process_death_retains_same_key_and_never_resends(tmp_path):
    root = tmp_path/'p'
    root.mkdir(mode=0o700)
    script = root/'fake_service.py'
    script.write_text(FAKE_SERVER)
    project = Path(__file__).resolve().parents[1]
    env = {**os.environ, 'PYTHONPATH': str(project), 'RIO_EXECUTION_CORE': '0'}
    client = CoreClient(root/'s.sock')
    def start():
        return subprocess.Popen([sys.executable, str(script), str(root)], cwd=project, env=env,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    child = start()
    try:
        wait_for(lambda: client.request('health'))
        session = client.request('session.open')['session']
        task = client.request('task.submit', session=session, request='/home 거실 on')
        marker = root/'sent.txt'
        wait_for(lambda: marker.exists() and marker.read_text().strip())
        sent_key = marker.read_text().strip()
        child.kill()
        child.wait(3)
        child = start()
        wait_for(lambda: client.request('health'))
        reopened = client.request('session.open', session=session)
        assert reopened['pending'][0]['state'] == 'PAUSED'
        assert marker.read_text().splitlines() == [sent_key]
        result = client.request('task.resume', session=session, id=task['id'])
        assert result['state'] == 'QUEUED'
        state = wait_for(lambda: (r if (r := client.request('task.status', session=session, id=task['id']))['state'] == 'COMPLETED' else None))
        assert state['answer'] == 'SDK acknowledged'
        assert marker.read_text().splitlines() == [sent_key]
        assert client.request('tool.status', session=session, request_id=sent_key)['confirmation'] == 'SDK_ACK'
    finally:
        child.terminate()
        try:
            child.wait(3)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait(3)


def test_same_session_reconnect_reads_completed_result_without_new_call(tmp_path):
    root = tmp_path/'r'
    root.mkdir(mode=0o700)
    project = Path(__file__).resolve().parents[1]
    env = {**os.environ, 'RIO_EXECUTION_CORE': '0'}
    child = subprocess.Popen([sys.executable, '-m', 'core.execution.service', '--state', str(root), '--no-proactive'],
                             cwd=project, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    client = CoreClient(root/'service.sock')
    try:
        wait_for(lambda: client.request('health'))
        session = client.request('session.open')['session']
        task = client.request('task.submit', session=session, request='/core status')
        wait_for(lambda: client.request('task.status', session=session, id=task['id'])['state'] == 'COMPLETED')
        second = CoreClient(root/'service.sock')
        assert second.request('session.open', session=session)['pending'] == []
        assert len(second.request('task.status', session=session)) == 1
        events = second.request('task.events', session=session)
        assert sum(e['kind'] == 'TOOL_START' for e in events) == 1
        assert (root/'core.sqlite3').stat().st_mode & 0o077 == 0
    finally:
        child.terminate()
        child.wait(3)
