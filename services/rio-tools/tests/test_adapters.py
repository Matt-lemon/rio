import pytest
from core.tools.contracts import ToolError
from rio_tools.adapters import AdapterCatalog
from rio_tools.registry import Registry


class Home:
    def __init__(self):self.calls=[]
    def schemas(self):return [{'name':'home_get_status','parameters':{'type':'object','properties':{},'additionalProperties':False}},
                            {'name':'home_set_light','parameters':{'type':'object','properties':{'target':{'type':'string'},'on':{'type':'boolean'}},'required':['target','on'],'additionalProperties':False}}]
    def call(self,name,args):
        self.calls.append((name,args))
        return {'status':'REJECTED','code':'CONTROLS_LOCKED'}

class Dubbing:
    def __init__(self):self.calls=[]
    def dispatch(self,name,args):
        self.calls.append((name,args));return {'status':'queued','expected_revision':args.get('expected_revision')}

class PC:
    def __init__(self):self.actions=[]
    def observe(self):self.actions.append('observe');return {'switch':'on'}
    def wake(self):self.actions.append('wake');return {'result':'PENDING_DO_NOT_RETRY'}
    def shutdown(self):self.actions.append('shutdown');return {'result':'SHUTDOWN_BUSY','commands_sent':0}

def test_dubbing_dispatch_preserves_revision_and_no_unapproved_write(tmp_path):
    r=Registry(tmp_path/'state');a=AdapterCatalog(r);client=Dubbing()
    a.dubbing(client,[{'function':{'name':'dubbing_start','parameters':{'type':'object',
        'properties':{'job_id':{'type':'string'},'expected_revision':{'type':'integer'}},
        'required':['job_id','expected_revision'],'additionalProperties':False}}}])
    args={'job_id':'job-001','expected_revision':4}
    with pytest.raises(ToolError,match='ADAPTER_NOT_ACTIVE'):a.call('dubbing_start',args,'request_001')
    assert client.calls==[]
    key=a.bindings['dubbing_start'][2];a.approve(key,['external.send'])
    assert a.call('dubbing_start',args,'request_001')['expected_revision']==4
    assert client.calls==[('dubbing_start',args)]
    r.close()

def test_pc_shutdown_retains_underlying_busy_guard(tmp_path):
    r=Registry(tmp_path/'state');a=AdapterCatalog(r);pc=PC();a.pc(pc)
    assert a.call('pc_status',{},'request_001')['switch']=='on'
    with pytest.raises(ToolError,match='ADAPTER_NOT_ACTIVE'):a.call('pc_shutdown',{},'request_002')
    assert pc.actions==['observe']
    key=a.bindings['pc_shutdown'][2];a.approve(key,['device.control'])
    assert a.call('pc_shutdown',{},'request_002')['commands_sent']==0
    r.close()

def test_pinned_adapter_job_cannot_invoke_different_version(tmp_path):
    r=Registry(tmp_path/'state');a=AdapterCatalog(r);h=Home();a.home(h)
    with pytest.raises(ToolError,match='ADAPTER_VERSION_CHANGED'):
        a.call('home_get_status',{},'request_001',expected_hash='0'*64)
    assert h.calls==[];r.close()


def test_adapter_mutations_wait_for_hash_bound_approval(tmp_path):
    r=Registry(tmp_path/'state');a=AdapterCatalog(r);h=Home();a.home(h)
    with pytest.raises(ToolError,match='ADAPTER_NOT_ACTIVE'):a.call('home_set_light',{'target':'room','on':True},'request_001')
    assert h.calls==[]
    key=a.bindings['home_set_light'][2]
    with pytest.raises(ToolError):a.approve(key,['external.send'])
    a.approve(key,['device.control'])
    result=a.call('home_set_light',{'target':'room','on':True},'request_001')
    assert result['code']=='CONTROLS_LOCKED'
    assert h.calls[-1][1]['idempotency_key']=='request_001'
    r.close()


def test_existing_read_adapter_and_argument_validation(tmp_path):
    r=Registry(tmp_path/'state');a=AdapterCatalog(r);h=Home();a.home(h)
    with pytest.raises(ToolError):a.call('home_get_status',{'override':True},'request_001')
    assert h.calls==[]
    assert a.call('home_get_status',{},'request_001')['status']=='REJECTED'
    r.close()
