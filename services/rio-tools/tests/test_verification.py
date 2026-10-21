import json
import pytest
from core.tools.verification import SrtVerification,EXPECTED
from core.tools.contracts import ToolError

class Client:
    def __init__(self):self.calls=[];self.executions={};self.generated=0;self.provenance=True;self.wrong_result=False
    def request(self,action,**args):
        self.calls.append((action,args))
        package='a'*64
        if action=='list':value=[]
        elif action=='build':self.generated+=1;value={'status':'QUEUED'}
        elif action=='describe':value={'name':'srt_inspect','hash':package,'state':'ACTIVE',
            'contract':{'permissions':['input.read','workspace.compute','workspace.output'],'image_id':'sha256:'+'b'*64},
            'approval':json.dumps({'package_hash':package,'permissions':['input.read','workspace.compute','workspace.output']})}
        elif action=='validation':value=[{'static_pass':True,'fixed_tests_pass':True,'isolation_pass':True}]
        elif action=='call':
            self.executions.setdefault(args['request_id'],1);value={'status':'QUEUED'}
        elif action=='job' and '_call_' not in args['request_id']:
            value={'status':'ACTIVE','package_hash':package,'attempts':
                   [{'package_hash':package,'response_hash':'c'*64}] if self.provenance else []}
        elif action=='job':value={'status':'SUCCEEDED','package_hash':package,
            'request_id':args['request_id'],'result':{'result':{} if self.wrong_result else EXPECTED}}
        else:raise AssertionError(action)
        return {'ok':True,'result':value}

def test_user_triggered_request_searches_first_then_reuses_same_two_calls():
    c=Client();flow=SrtVerification('human_request_001',c)
    assert c.calls==[] and c.generated==0
    assert flow.start()['status']=='QUEUED'
    assert [a for a,_ in c.calls]==['list','build']
    assert flow.poll()['status']=='SRT_VALIDATION_COMPLETED'
    assert flow.poll()['status']=='SRT_VALIDATION_COMPLETED'
    assert c.generated==1 and len(c.executions)==2 and all(v==1 for v in c.executions.values())

def test_template_result_does_not_prove_actual_model_generation():
    c=Client();c.provenance=False;flow=SrtVerification('human_request_001',c)
    assert flow.poll()['status']=='UNVERIFIED'
    assert c.executions=={}

def test_post_registration_result_mismatch_is_not_success():
    c=Client();c.wrong_result=True
    with pytest.raises(ToolError,match='POST_REGISTRATION_TEST_FAILED'):
        SrtVerification('human_request_001',c).poll()

def test_existing_tool_does_not_get_replaced_for_validation():
    c=Client();original=c.request
    c.request=lambda action,**args:{'ok':True,'result':[{'name':'srt_inspect'}]} if action=='list' else original(action,**args)
    assert SrtVerification('human_request_001',c).start()['status']=='EXISTING_TOOL_FOUND'
    assert c.generated==0

def test_session_start_and_exit_do_not_request_generation(monkeypatch):
    import core.tools.session as session
    monkeypatch.setattr(session,'SrtVerification',lambda *a:(_ for _ in ()).throw(AssertionError('must not build on startup')))
    output=[];session.run_session(reader=lambda prompt:'/exit',writer=output.append)
    assert len(output)==1

def test_waiting_status_explains_no_generation_and_keeps_original_id():
    from core.tools.session import status_message
    message=status_message({'status':'WAITING_MODEL'},'human_request_001')
    assert '아직 생성하지 않았어' in message and 'human_request_001' in message
    assert '완료' not in message

def test_resume_preserves_request_id_and_does_not_retry_terminal_failure():
    c=Client();original=c.request
    for state in ('WAITING_MODEL','FAILED'):
        c.request=lambda action,**args:({'ok':True,'result':{'status':state}}
            if action=='job' else original(action,**args))
        value=SrtVerification('human_request_001',c).resume()
        assert value['status']==('QUEUED' if state=='WAITING_MODEL' else 'FAILED')
    assert c.generated==1
    assert [args['request_id'] for action,args in c.calls if action=='build']==['human_request_001']

def test_session_can_query_result_before_any_submission(monkeypatch):
    import core.tools.session as session
    class Flow:
        def __init__(self,key):self.key=key
        def poll(self):return {'status':'GENERATING'}
    monkeypatch.setattr(session,'SrtVerification',Flow)
    commands=iter(('/tools result human_request_001','/exit'));output=[]
    session.run_session(reader=lambda prompt:next(commands),writer=output.append)
    assert '실제 모델로 코드를 생성 중' in output[1]
    assert 'human_request_001' in output[1]
