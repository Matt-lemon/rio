import json
import threading
import time
import pytest
from core.tools.contracts import ToolError
from rio_tools.registry import Registry
from rio_tools.srt import package
from rio_tools.service import ToolService
from rio_tools.executor import DockerExecutor

IMAGE='sha256:'+'a'*64


def wait_job(service,key):
    for _ in range(100):
        result=service.job(key)
        if result['status'] not in {'RUNNING','QUEUED'}:return result
        time.sleep(.01)
    raise AssertionError('job did not finish')


class MockExecutor:
    image_id=IMAGE
    def __init__(self):self.count=0;self.delay=threading.Event();self.delay.set()
    def run_package(self,p,args,**kwargs):
        self.count+=1;self.delay.wait(2)
        return {'result':{'segments':0,'valid_segments':0,'total_duration_ms':0,
                         'timeline_duration_ms':0,'invalid_times':[],'overlap_count':0}}


@pytest.fixture
def fixture(tmp_path):
    r=Registry(tmp_path/'state');key=r.candidate(package(IMAGE))
    r.validated(key,{'static_pass':True,'fixed_tests_pass':True,'isolation_pass':True});r.activate(key)
    executor=MockExecutor();service=ToolService(r,executor)
    yield r,executor,service,key
    r.close()


def test_input_validation_precedes_dispatch(fixture):
    r,e,s,k=fixture
    with pytest.raises(ToolError):s.submit('srt_inspect',{'text':'','code':'bad'},'request_001')
    assert e.count==0


def test_duplicate_request_reuses_execution(fixture):
    r,e,s,k=fixture;s.submit('srt_inspect',{'text':''},'request_001')
    assert wait_job(s,'request_001')['status']=='SUCCEEDED'
    assert s.submit('srt_inspect',{'text':''},'request_001')['status']=='SUCCEEDED'
    assert e.count==1
    with pytest.raises(ToolError,match='REQUEST_ID_CONFLICT'):s.submit('srt_inspect',{'text':'different'},'request_001')


def test_job_keeps_original_version(fixture):
    r,e,s,k=fixture;e.delay.clear();s.submit('srt_inspect',{'text':''},'request_001')
    newer=r.candidate(package(IMAGE,version='1.0.1'))
    r.validated(newer,{'static_pass':True,'fixed_tests_pass':True,'isolation_pass':True});r.activate(newer)
    e.delay.set();assert wait_job(s,'request_001')['package_hash']==k


def test_expected_version_rejects_stale_discovery_before_any_execution(fixture):
    r,e,s,k=fixture
    newer=r.candidate(package(IMAGE,version='1.0.1'))
    r.validated(newer,{'static_pass':True,'fixed_tests_pass':True,'isolation_pass':True});r.activate(newer)
    with pytest.raises(ToolError,match='TOOL_VERSION_CHANGED'):
        s.dispatch({'action':'call','name':'srt_inspect','args':{'text':''},'request_id':'version_001','expected_hash':k})
    assert e.count==0
    result=s.dispatch({'action':'call','name':'srt_inspect','args':{'text':''},'request_id':'version_001','expected_hash':newer})
    assert wait_job(s,'version_001')['package_hash']==newer
    with pytest.raises(ToolError,match='REQUEST_VERSION_CONFLICT'):
        s.submit('srt_inspect',{'text':''},'version_001',k)


def test_public_api_cannot_approve_or_set_profile(fixture):
    r,e,s,k=fixture
    for data in [{'action':'activate','hash':k},
                 {'action':'call','name':'srt_inspect','args':{'text':''},'request_id':'request_001','image':'evil'}]:
        with pytest.raises(ToolError,match='PUBLIC_ACTION_FORBIDDEN'):s.dispatch(data)


def test_unknown_tool_not_reported_as_completed(fixture):
    r,e,s,k=fixture
    with pytest.raises(ToolError,match='TOOL_NOT_REGISTERED'):s.submit('imaginary_tool',{},'request_001')
    assert e.count==0


def test_restart_unknown_does_not_reexecute(tmp_path):
    r=Registry(tmp_path/'state');e=MockExecutor()
    r.db.execute('INSERT INTO jobs VALUES(?,?,?,?,?,?,?,?,?)',('request_001','adapter','h','p','RUNNING','{}',None,0,0))
    s=ToolService(r,e);assert s.job('request_001')['status']=='UNKNOWN';assert e.count==0;r.close()


def test_fixed_container_policy(tmp_path):
    e=DockerExecutor(tmp_path/'executor',IMAGE);command=e.command('rio-tools-'+'a'*32,'runtime')
    for option in ['--network=none','--read-only','--cap-drop=ALL','--security-opt=no-new-privileges',
                   '--user=65532:65532','--memory=512m','--memory-swap=512m','--cpus=1','--pids-limit=32']:
        assert option in command
    assert all(option not in command for option in ['--privileged','-v','--volume','--mount','sh','-c'])


@pytest.mark.parametrize('steps',[[{'command':'sh','args':['-c','touch /tmp/evil']}],
                                [{'command':'sort','args':['/etc/passwd']}],
                                [{'command':'wc','args':['-l;touch /tmp/evil']}],
                                [{'command':'/usr/bin/wc','args':[]} ]])
def test_shell_injection_not_dispatched(tmp_path,steps):
    e=DockerExecutor(tmp_path/'executor',IMAGE)
    with pytest.raises(ToolError):e.pipeline(steps,'example')
