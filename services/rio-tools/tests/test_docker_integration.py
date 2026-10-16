"""Opt-in platform denial probes: never run on the host or operate home devices."""
import json
import os
import subprocess
import threading
import time
from pathlib import Path
import pytest
from core.tools.contracts import ToolError
from rio_tools.executor import DockerExecutor
from core.tools.client import ToolClient

IMAGE=os.environ.get('RIO_TOOLS_TEST_IMAGE')
pytestmark=pytest.mark.skipif(not IMAGE,reason='Pinned local runtime image required')


@pytest.fixture
def executor(tmp_path):
    return DockerExecutor(tmp_path/'executor',IMAGE)


def probe(executor,source,**options):
    return executor.execute({'mode':'package','kind':'python','source':source,'input':{}},**options)


def test_actual_resource_profile_and_host_boundaries(executor):
    source='''import json, pathlib, os, socket
r=pathlib.Path
blocked=False
try:
 socket.create_connection(('192.0.2.1',443),timeout=.2)
except OSError: blocked=True
print(json.dumps({'cpu':r('/sys/fs/cgroup/cpu.max').read_text().strip(),
 'memory':r('/sys/fs/cgroup/memory.max').read_text().strip(),
 'network_blocked':blocked,'uid':os.getuid(),
 'socket_absent':not r('/var/run/docker.sock').exists(),
 'host_absent':not r('/home/example').exists()}))
'''
    result=probe(executor,source)['result']
    quota,period=map(int,result['cpu'].split())
    assert quota==period
    assert int(result['memory'])==512*1024*1024
    assert result['uid']==65532
    assert result['network_blocked'] and result['socket_absent'] and result['host_absent']


@pytest.mark.parametrize('source',[
    "import json; blob=bytearray(768*1024*1024); print(json.dumps(True))",
    "from pathlib import Path; import json; Path('/work/large').write_bytes(b'x'*(65*1024*1024)); print(json.dumps(True))",
    "import json; print(json.dumps('x'*(2*1024*1024)))",
])
def test_memory_disk_output_overflow_fail_closed(executor,source):
    with pytest.raises(ToolError,match='CONTAINER_TOOL_FAILED|OUTPUT_LIMIT'):
        probe(executor,source)


def test_runtime_deadline_and_container_cleanup(executor):
    names=[];original=executor.command
    def command(name,lane):
        names.append(name);return original(name,lane)
    executor.command=command
    with pytest.raises(ToolError,match='TIME_LIMIT'):
        probe(executor,'while True: pass')
    query=subprocess.run(['/usr/bin/docker','--host','unix:///var/run/docker.sock',
                          'inspect',names[0]],capture_output=True)
    assert query.returncode!=0


def test_cancellation_removes_only_own_container(executor):
    cancel=threading.Event();timer=threading.Timer(2,cancel.set);timer.start()
    try:
        with pytest.raises(ToolError,match='CANCELLED'):
            probe(executor,'while True: pass',cancel=cancel)
    finally:timer.cancel()


def test_actual_socket_registry_reuse_and_restart(tmp_path):
    import sys
    repo=Path(__file__).resolve().parents[3]
    state=tmp_path/'state'
    env={**os.environ,'PYTHONPATH':str(repo)+os.pathsep+str(repo/'services/rio-tools')}
    command=[sys.executable,'-m','rio_tools.service','--state',str(state),'--image-id',IMAGE]
    pilot=subprocess.run([*command,'srt-pilot'],capture_output=True,env=env,timeout=30)
    assert pilot.returncode==0
    key=json.loads(pilot.stdout)['package_hash']
    client=ToolClient(state/'service.sock')
    def start():
        process=subprocess.Popen([*command,'serve'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,env=env)
        for _ in range(100):
            if (state/'service.sock').exists():return process
            assert process.poll() is None
            time.sleep(.02)
        process.terminate();process.wait(timeout=10);raise AssertionError('no socket')
    def finish(key):
        for _ in range(200):
            result=client.request('job',request_id=key)['result']
            if result['status'] not in {'QUEUED','RUNNING'}:return result
            time.sleep(.02)
        raise AssertionError('job timeout')
    process=start()
    try:
        assert client.request('describe',name='srt_inspect')['result']['hash']==key
        with pytest.raises(ToolError,match='PUBLIC_ACTION_FORBIDDEN'):
            client.request('activate',hash=key)
    finally:
        process.terminate();process.wait(timeout=15)
    assert not (state/'service.sock').exists()
    for key_id in ['socket_request_001','socket_request_002']:
        process=start()
        try:
            queued=client.request('call',name='srt_inspect',args={'text':''},request_id=key_id)['result']
            assert queued['package_hash']==key
            result=finish(key_id)
            assert result['status']=='SUCCEEDED'
            assert result['result']['usage']['child_max_rss_kib']>0
            assert client.request('call',name='srt_inspect',args={'text':''},request_id=key_id)['result']['status']=='SUCCEEDED'
            assert client.request('call',name='pipeline_run',args={'text':'b\na\na\n',
                'steps':[{'command':'sort','args':[]},{'command':'uniq','args':[]},{'command':'wc','args':['-l']}]},
                request_id='pipeline_socket_'+key_id[-3:])['ok']
            assert finish('pipeline_socket_'+key_id[-3:])['result']['result']['text']=='2\n'
            assert client.request('job',request_id='socket_request_001')['result']['status']=='SUCCEEDED'
        finally:
            process.terminate();process.wait(timeout=15)
        assert process.returncode==0
        assert not (state/'service.sock').exists()
