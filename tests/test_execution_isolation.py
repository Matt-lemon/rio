"""Account boundary and durable adapter bridge; effects use mocks only."""
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
import json
import os

import pytest

from core.execution.bridge import Bridge
from core.execution.catalog import Catalog
from core.execution.isolated import RemoteAdapters,RemoteCatalog,RemoteSuggestions
from core.execution.peers import validate_policy,load_policy,CORE_SOCKET,BRIDGE_SOCKET
from core.execution.store import Store
from core.execution.types import ToolResult,spec,TEXT

SESSION='a'*32
KEY='bridge_request_123'


@pytest.fixture
def setup(tmp_path):
    store=Store(tmp_path/'private')
    declarations={'web.search':spec('web.search','search','read',query=TEXT)}
    catalog=Catalog(provider=lambda:declarations)
    class Adapters:
        def __init__(self):self.sent=[]
        def call_tracked(self,d,args,key,browser,progress):
            progress.attempted('TEST_ONLY_BOUNDARY');self.sent.append(key)
            progress.accepted('TEST_RECEIPT')
            return ToolResult('OBSERVED','observed data',{'content_read':False},key,'SEARCH_SNIPPETS')
        def status(self,*args):return ToolResult('UNKNOWN','no replay',request_id=args[-1])
    adapters=Adapters();bridge=Bridge(store,catalog=catalog,adapters=adapters)
    yield store,catalog,adapters,bridge,declarations
    store.close()


def request(catalog,**changes):
    return {'op':'call','session':SESSION,'name':'web.search','args':{'query':'public words'},
            'revision':catalog.describe('web.search')['revision'],'request_id':KEY,
            'original_request':'public words 검색해줘',**changes}


def test_duplicate_bridge_call_reuses_one_ledger_and_dispatch(setup):
    store,catalog,adapters,bridge,_=setup
    first=bridge.dispatch(request(catalog));again=bridge.dispatch(request(catalog))
    assert first['result']==again['result'] and adapters.sent==[KEY]
    assert len(store.rows('SELECT * FROM calls'))==1
    assert [r['phase'] for r in first['history']['transitions']]==['PREPARED','ATTEMPTED','ACCEPTED','RESULT_CONFIRMED']
    with pytest.raises(ValueError,match='CONFLICT'):
        bridge.dispatch(request(catalog,args={'query':'other'}))
    with pytest.raises(ValueError,match='REQUEST_NOT_FOUND'):
        bridge.dispatch({'op':'status','session':'b'*32,'request_id':KEY})
    assert adapters.sent==[KEY]


def test_current_binding_revision_is_checked_before_bridge_reservation(setup):
    store,catalog,adapters,bridge,declarations=setup
    old=request(catalog)
    declarations['web.search']['description']='changed binding'
    with pytest.raises(ValueError,match='REVISION_CHANGED'):bridge.dispatch(old)
    assert not adapters.sent and not store.rows('SELECT * FROM bridge_calls')


def test_partial_reservation_after_crash_never_dispatches_again(setup):
    store,catalog,adapters,bridge,_=setup
    r=request(catalog);task=store.create_task(SESSION,'original request')
    with store.transaction() as db:
        db.execute('INSERT INTO bridge_calls VALUES(?,?,?,?,?,?)',(KEY,SESSION,r['name'],json.dumps(r['args'],sort_keys=True,separators=(',',':')),r['revision'],task['id']))
    result=bridge.dispatch(r)
    assert result['result']['status']=='UNKNOWN' and result['history'] is None
    assert adapters.sent==[]


@pytest.mark.parametrize('op',['build','approve','docker','exec','shell','permissions','filesystem'])
def test_bridge_has_no_builder_or_privileged_generic_command(setup,op):
    with pytest.raises(ValueError,match='OPERATION_FORBIDDEN'):setup[3].dispatch({'op':op})


def test_remote_adapter_marks_attempt_before_ipc_and_preserves_backend_confirmation(setup):
    store,catalog,_,_,_=setup
    call_order=[]
    def rpc(op,**args):
        call_order.append('ipc')
        assert args['request_id']==KEY and args['original_request']=='original human request'
        return {'result':ToolResult('SUCCEEDED','SDK acknowledged',{'physical_state_confirmed':False},KEY,'SDK_ACK').public(),
                'browser':{},'history':{'transitions':[{'phase':'ACCEPTED'}]}}
    peer=SimpleNamespace(request=rpc);progress=SimpleNamespace(
        attempted=lambda source:call_order.append('attempted'),accepted=lambda source:call_order.append('accepted'),
        claim={'completion':'original human request'})
    browser=SimpleNamespace(session_id=SESSION,clear=Mock())
    result=RemoteAdapters(peer,store).call_tracked(catalog.describe('web.search'),{'query':'x'},KEY,browser,progress)
    assert call_order==['attempted','ipc','accepted'] and result.confirmation=='SDK_ACK'
    assert result.evidence['physical_state_confirmed'] is False


def test_remote_disconnect_cannot_invoke_a_local_adapter(setup):
    store,catalog,_,_,_=setup
    peer=SimpleNamespace(request=Mock(side_effect=TimeoutError()))
    progress=SimpleNamespace(attempted=Mock(),claim={'completion':'original request'})
    browser=SimpleNamespace(session_id=SESSION)
    with pytest.raises(TimeoutError):RemoteAdapters(peer,store).call_tracked(catalog.describe('web.search'),{'query':'x'},KEY,browser,progress)
    progress.attempted.assert_called_once()
    peer.request.assert_called_once()


def test_account_policy_rejects_same_uid_untrusted_endpoints_and_root_uid():
    value={'version':1,'core_uid':123,'operator_uid':1000,'core_socket':CORE_SOCKET,
           'bridge_socket':BRIDGE_SOCKET,'operator_project':'/home/operator/project','release':'/opt/rio-core/releases/abcd'}
    assert validate_policy(value)==value
    for change in ({'core_uid':1000},{'core_uid':0},{'core_socket':'/tmp/other.sock'},{'release':'/home/operator/source'}):
        with pytest.raises(ValueError):validate_policy({**value,**change})


def test_operator_owned_policy_cannot_authorize_a_privileged_peer(tmp_path):
    path=tmp_path/'peers.json';path.write_text('{}')
    with pytest.raises(ValueError,match='ROOT_OWNED'):load_policy(path)


def test_suggestion_inbox_transport_only_ingests_and_never_submits_tasks(setup):
    store,_,_,_,_=setup
    peer=SimpleNamespace(request=Mock(return_value=[{'id':'test_event_123','kind':'note','text':'참고 자료가 있어요'}]))
    suggestions=RemoteSuggestions(store,peer);suggestions.import_legacy_events()
    assert len(store.rows('SELECT * FROM suggestions'))==1
    assert store.rows('SELECT * FROM tasks')==[]
    peer.request.assert_called_once_with('legacy.events')


PROCESS_BRIDGE=r'''
import sys,time,os
from pathlib import Path
import core.execution.bridge as module
from core.execution.catalog import Catalog
from core.execution.types import ToolResult,spec,enum
root=Path(sys.argv[1]);marker=root/'sent.txt'
class Adapter:
 def __init__(self,store):pass
 def call_tracked(self,descriptor,args,key,browser,progress):
  progress.attempted('TEST_EFFECT_BOUNDARY')
  with marker.open('a') as f:f.write(key+'\n');f.flush()
  progress.accepted('TEST_RECEIPT')
  while not (root/'release').exists():time.sleep(.02)
  return ToolResult('SUCCEEDED','SDK acknowledged',{'physical_state_confirmed':False},key,'SDK_ACK')
 def status(self,name,args,key):return ToolResult('UNKNOWN','not resent',request_id=key)
module.Adapters=Adapter
module.Catalog=lambda:Catalog(provider=lambda:{'home.light':spec('home.light','test effect','control',target=enum('거실'),action=enum('on','off'))})
module.grant_socket=lambda path,uid:None
module.serve(root,root/'bridge.sock',os.getuid(),root)
'''

PROCESS_CORE=r'''
import sys,os
from pathlib import Path
from core.execution.store import Store
from core.execution.engine import Task
from core.execution.isolated import RemoteCatalog,RemoteAdapters
from core.execution.peers import PeerClient
from core.execution.service import Application,serve
root=Path(sys.argv[1]);peer=PeerClient(sys.argv[2],os.getuid());store=Store(root)
task=Task(store,RemoteCatalog(peer),RemoteAdapters(peer,store),planner=lambda p:{'kind':'done'},responder=lambda *a:'observed')
serve(root,root/'core.sock',application=Application(store,task),proactive=False)
'''


def eventually(check,seconds=5):
    import time
    deadline=time.monotonic()+seconds
    while time.monotonic()<deadline:
        try:
            result=check()
            if result:return result
        except (OSError,ValueError):pass
        time.sleep(.02)
    raise AssertionError('private IPC process did not become ready')


def run_process(script,*args):
    import subprocess,sys
    root=Path(__file__).resolve().parents[1]
    return subprocess.Popen([sys.executable,'-c',script,*map(str,args)],cwd=root,
        env={**os.environ,'RIO_EXECUTION_CORE':'0'},stdout=subprocess.DEVNULL,stderr=subprocess.PIPE)


def finish_process(child):
    import subprocess
    if child.poll() is None:child.terminate()
    try:child.wait(3)
    except subprocess.TimeoutExpired:child.kill();child.wait(3)
    child.stderr.close()


def test_real_core_death_leaves_live_bridge_result_queryable_without_resend(tmp_path):
    from core.execution.client import CoreClient
    from core.execution.peers import PeerClient
    broker=tmp_path/'broker';broker.mkdir(mode=0o700)
    state=tmp_path/'core';state.mkdir(mode=0o700)
    b=run_process(PROCESS_BRIDGE,broker);c=None
    try:
        eventually(lambda:PeerClient(broker/'bridge.sock',os.getuid()).request('health'))
        c=run_process(PROCESS_CORE,state,broker/'bridge.sock');client=CoreClient(state/'core.sock')
        eventually(lambda:client.request('health'))
        session=client.request('session.open')['session']
        task=client.request('task.submit',session=session,request='/home 거실 on')
        eventually(lambda:(broker/'sent.txt').exists())
        key=(broker/'sent.txt').read_text().strip()
        c.kill();c.wait(3);finish_process(c)
        (broker/'release').touch()
        eventually(lambda:PeerClient(broker/'bridge.sock',os.getuid()).request('status',session=session,request_id=key)['result']['status']=='SUCCEEDED')
        c=run_process(PROCESS_CORE,state,broker/'bridge.sock');eventually(lambda:client.request('health'))
        assert client.request('session.open',session=session)['pending'][0]['state']=='PAUSED'
        assert client.request('task.resume',session=session,id=task['id'])['state']=='QUEUED'
        eventually(lambda:client.request('task.status',session=session,id=task['id'])['state']=='COMPLETED')
        assert (broker/'sent.txt').read_text().splitlines()==[key]
        assert client.request('tool.status',session=session,request_id=key)['confirmation']=='SDK_ACK'
    finally:
        if c is not None:finish_process(c)
        finish_process(b)


def test_real_bridge_death_preserves_attempt_and_unknown_without_resend(tmp_path):
    from core.execution.client import CoreClient
    from core.execution.peers import PeerClient
    broker=tmp_path/'broker';broker.mkdir(mode=0o700)
    state=tmp_path/'core';state.mkdir(mode=0o700)
    b=run_process(PROCESS_BRIDGE,broker);c=None
    try:
        eventually(lambda:PeerClient(broker/'bridge.sock',os.getuid()).request('health'))
        c=run_process(PROCESS_CORE,state,broker/'bridge.sock');client=CoreClient(state/'core.sock')
        eventually(lambda:client.request('health'));session=client.request('session.open')['session']
        task=client.request('task.submit',session=session,request='/home 거실 on')
        eventually(lambda:(broker/'sent.txt').exists());key=(broker/'sent.txt').read_text().strip()
        b.kill();b.wait(3);finish_process(b)
        eventually(lambda:client.request('task.status',session=session,id=task['id'])['state']=='UNKNOWN')
        b=run_process(PROCESS_BRIDGE,broker);eventually(lambda:PeerClient(broker/'bridge.sock',os.getuid()).request('health'))
        assert client.request('task.resume',session=session,id=task['id'])['state']=='UNKNOWN'
        assert (broker/'sent.txt').read_text().splitlines()==[key]
        observed=PeerClient(broker/'bridge.sock',os.getuid()).request('status',session=session,request_id=key)
        assert observed['history']['phase']=='ACCEPTED' and observed['result']['status']=='UNKNOWN'
    finally:
        if c is not None:finish_process(c)
        finish_process(b)


def account_installer():
    import importlib.util
    path=Path(__file__).resolve().parents[1]/'tools/rio-runtime/nas/rio-core-accounts.py'
    spec=importlib.util.spec_from_file_location('rio_accounts_installer_test',path)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    return module


def test_system_core_template_has_separate_identity_and_no_network_or_docker(tmp_path):
    installer=account_installer()
    texts=installer.units(tmp_path,SimpleNamespace(pw_dir='/home/operator',pw_uid=1000,pw_gid=1000),SimpleNamespace(pw_dir='/var/lib/rio-core',pw_gid=991))
    core=texts[installer.CORE_UNIT];broker=texts[installer.BRIDGE_UNIT]
    assert 'User=rio-core' in core and 'Group=991' in core
    assert 'RestrictAddressFamilies=AF_UNIX' in core and 'ProtectHome=yes' in core
    assert 'InaccessiblePaths=-/var/run/docker.sock -/run/docker.sock' in core
    assert 'NoNewPrivileges=yes' in core and 'CapabilityBoundingSet=\n' in core
    assert 'User=example' in broker
    assert 'docker' not in broker.lower() and '--use-shared-model' not in broker


def test_root_installer_never_runs_privileged_steps_without_root(monkeypatch):
    import sys
    installer=account_installer();monkeypatch.setattr(installer.os,'getuid',lambda:1000)
    monkeypatch.setattr(sys,'argv',['rio-core-accounts.py','--install','--commit','a'*40])
    invoked=Mock();monkeypatch.setattr(installer,'install',invoked)
    with pytest.raises(SystemExit,match='관리자 인증'):installer.main()
    invoked.assert_not_called()


def test_rollback_preserves_newest_rows_and_uncertain_history(tmp_path,monkeypatch):
    import sqlite3,subprocess,sys
    installer=account_installer();op=tmp_path/'operator';(op/'.local/state/rio/core').mkdir(parents=True)
    (op/'.config/rio-execution').mkdir(parents=True)
    old=op/'.local/state/rio/core/core.sqlite3';live=tmp_path/'live.sqlite3'
    for path,rows in ((old,[('old','OBSERVED')]),(live,[('old','OBSERVED'),('latest','UNKNOWN')])):
        db=sqlite3.connect(path);db.execute('CREATE TABLE calls(id TEXT,state TEXT)');db.executemany('INSERT INTO calls VALUES(?,?)',rows);db.commit();db.close();path.chmod(0o600)
    backup=tmp_path/'backup';backup.mkdir()
    (backup/'selection-before.json').write_text(json.dumps({'enabled':True,'version':1}))
    (op/'.config/rio-execution/selection.json').write_text(json.dumps({'enabled':True,'transport':'isolated'}))
    state=tmp_path/'root-state';state.mkdir();monkeypatch.setattr(installer,'ROOT_STATE',state)
    monkeypatch.setattr(installer.pwd,'getpwnam',lambda _:SimpleNamespace(pw_dir=str(op)))
    monkeypatch.setattr(installer.os,'chown',lambda *a:None)
    system=Mock();user=Mock();report=Mock()
    monkeypatch.setattr(installer,'system_unit',system);monkeypatch.setattr(installer,'user_unit',user);monkeypatch.setattr(installer,'save_operator_report',report)
    def operator(code,*,release=None,data=None):
        return subprocess.run([sys.executable,'-c',code],input=data,env={**os.environ,'HOME':str(op)},check=True,capture_output=True)
    monkeypatch.setattr(installer,'operator_python',operator)
    installer.rollback({'backup':str(backup),'release':str(tmp_path),'user_core_enabled':True},live=live)
    db=sqlite3.connect(old);assert db.execute('SELECT * FROM calls ORDER BY id').fetchall()==[('latest','UNKNOWN'),('old','OBSERVED')];db.close()
    assert (backup/'rollback-latest.sqlite3').exists()
    assert json.loads((state/'current.json').read_text())['status']=='ROLLED_BACK_HISTORY_PRESERVED'
    assert json.loads((op/'.config/rio-execution/selection.json').read_text())=={'enabled':True,'version':1}
    user.assert_any_call('start','rio-execution-core.service')


def test_bridge_rejects_wrong_actual_process_uid_before_reading_request(tmp_path):
    import socket,sqlite3
    root=tmp_path/'broker';root.mkdir(mode=0o700)
    script=PROCESS_BRIDGE.replace("root/'bridge.sock',os.getuid(),root","root/'bridge.sock',os.getuid()+1,root")
    child=run_process(script,root)
    try:
        eventually(lambda:(root/'bridge.sock').exists())
        with socket.socket(socket.AF_UNIX,socket.SOCK_STREAM) as connection:
            connection.settimeout(2);connection.connect(str(root/'bridge.sock'))
            try:
                connection.sendall(b'{"op":"call","name":"shell"}\n');response=connection.recv(1024)
            except (ConnectionResetError,BrokenPipeError):response=b''
        assert response==b'' and child.poll() is None
        db=sqlite3.connect('file:'+str(root/'core.sqlite3')+'?mode=ro',uri=True)
        assert db.execute('SELECT COUNT(*) FROM calls').fetchone()[0]==0;db.close()
    finally:finish_process(child)
