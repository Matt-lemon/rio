import time
import pytest
from rio_tools.builder import Builder
from rio_tools.registry import Registry
from rio_tools.srt import SOURCE
from core.tools.contracts import ToolError
from rio_tools.builder import SharedModel

IMAGE='sha256:'+'a'*64

def test_shared_model_requires_actual_batch_gateway_contract(monkeypatch):
    model=SharedModel('http://127.0.0.1:8080/v1','existing-model')
    def read(path):
        if path=='/health':return {'service':'rio-llm-gateway','ok':True}
        assert path=='/v1/dubbing/capabilities'
        return {'protocol_version':1,'single_resident':True,'batch_healthy':True,
                'priority_policy':'realtime-before-batch','realtime_slot':0,'batch_slot':1,
                'batch_concurrency':1,'context_per_slot':8192}
    monkeypatch.setattr(model,'_read',read)
    assert model.available()
    monkeypatch.setattr(model,'_read',lambda path:{'single_resident':True,'batch_healthy':True})
    assert not model.available()


def test_batch_config_never_falls_back_to_nas_chat_url(monkeypatch):
    monkeypatch.setenv('LLM_BASE_URL','http://127.0.0.1:8080/v1')
    monkeypatch.delenv('RIO_TOOLS_LLM_BASE_URL',raising=False)
    monkeypatch.delenv('RIO_TOOLS_LLM_MODEL',raising=False)
    with pytest.raises(ToolError,match='EXPLICIT_BATCH_CONFIG_REQUIRED'):SharedModel.from_environment()


def test_plain_llama_server_rejected_before_batch_request(monkeypatch):
    model=SharedModel('http://127.0.0.1:8080/v1','existing-model');paths=[]
    def read(path):paths.append(path);return {'status':'ok'}
    monkeypatch.setattr(model,'_read',read)
    assert model.inspect()['status']=='WRONG_GATEWAY_ROLE'
    assert paths==['/health']


def test_http404_diagnostic_does_not_expose_response_or_credentials(monkeypatch):
    import urllib.error
    model=SharedModel('http://127.0.0.1:18080/v1','existing-model')
    def read(path):
        if path=='/health':return {'service':'rio-llm-gateway','ok':True}
        raise urllib.error.HTTPError('private-url',404,'private-body',{},None)
    monkeypatch.setattr(model,'_read',read)
    assert model.inspect()=={'status':'BATCH_ENDPOINT_UNAVAILABLE','stage':'capabilities','http_status':404}


def test_generation_records_real_response_hash_and_repairs_previous_code(monkeypatch):
    import json,hashlib
    from core.tools.contracts import digest
    model=SharedModel('http://127.0.0.1:18080/v1','existing-model');payloads=[]
    response={'choices':[{'message':{'content':json.dumps({'kind':'python','code':SOURCE})}}]}
    def read(path,payload):
        assert path=='/v1/dubbing/chat/completions'
        assert payload['stream'] is False
        payloads.append(payload);return response
    monkeypatch.setattr(model,'_read',read)
    model.generate()
    assert model.last_generation['response_hash']==digest(response)
    assert model.last_generation['code_hash']==hashlib.sha256(SOURCE.encode()).hexdigest()
    model.generate('FIXED_TEST_FAILED')
    assert SOURCE in payloads[1]['messages'][0]['content']
    assert 'FIXED_TEST_FAILED' in payloads[1]['messages'][0]['content']


def test_import_repair_uses_validator_allowlist_without_expanding_permissions(monkeypatch):
    import json
    from rio_tools.validator import IMPORTS
    model=SharedModel('http://127.0.0.1:18080/v1','existing-model');payloads=[]
    def read(path,payload):
        payloads.append(payload)
        return {'choices':[{'message':{'content':json.dumps({'kind':'python','code':SOURCE})}}]}
    monkeypatch.setattr(model,'_read',read)
    model.generate('IMPORT_NOT_ALLOWED')
    prompt=payloads[0]['messages'][0]['content']
    assert 'complete import allowlist is: '+', '.join(sorted(IMPORTS))+'.' in prompt
    assert 'Do not import typing' in prompt
    assert 'Remove all imports outside the complete allowlist' in prompt
    assert 'typing' not in IMPORTS
    assert model.last_generation['prompt_version']=='srt-builder-v5'
    assert 'program must NEVER wrap its results in kind/code' in prompt
    assert 'minutes or seconds are >=60' in prompt


class Executor:
    image_id=IMAGE


def wait(builder,key):
    for _ in range(100):
        result=builder.status(key)
        if result['status'] not in {'QUEUED','GENERATING','TESTING'}:return result
        time.sleep(.01)
    raise AssertionError('builder stalled')


def test_offline_build_is_persisted_without_loading_model(tmp_path):
    r=Registry(tmp_path/'state');b=Builder(r,Executor())
    b.request('build_request_001','srt.readonly.inspect')
    assert wait(b,'build_request_001')['status']=='WAITING_MODEL'
    r.close();r=Registry(tmp_path/'state')
    assert r.db.execute('SELECT status FROM builds').fetchone()[0]=='WAITING_MODEL'
    r.close()


def test_scope_expansion_rejected(tmp_path):
    r=Registry(tmp_path/'state');b=Builder(r,Executor())
    with pytest.raises(ToolError,match='BUILD_SCOPE_NOT_PREAPPROVED'):b.request('build_request_001','home.control')
    r.close()


def test_repairs_stop_after_two_and_preserve_candidates(tmp_path,monkeypatch):
    class Model:
        def __init__(self):self.calls=0
        def available(self):return True
        def generate(self,error,repair_details=None):self.calls+=1;return {'kind':'python','code':SOURCE}
    class RejectValidator:
        def __init__(self,*a):pass
        def validate(self,key):return {'static_pass':True,'fixed_tests_pass':False,'isolation_pass':False,'error_code':'FIXED_TEST_FAILED'}
    monkeypatch.setattr('rio_tools.builder.Validator',RejectValidator)
    r=Registry(tmp_path/'state');model=Model();b=Builder(r,Executor(),model)
    b.request('build_request_001','srt.readonly.inspect')
    b.tasks.join()
    assert b.status('build_request_001')['status']=='FAILED'
    assert model.calls==3 and b.status('build_request_001')['repairs']==2
    assert r.db.execute('SELECT count(*) FROM versions').fetchone()[0]==3
    b.request('build_request_001','srt.readonly.inspect');assert model.calls==3
    r.close()


def test_actual_syntax_diagnostic_reaches_bounded_model_repair_without_execution(tmp_path,monkeypatch):
    import json
    source='if True:\n    x = 1\n  y = 2\n'
    model=SharedModel('http://127.0.0.1:18080/v1','existing-model');payloads=[]
    monkeypatch.setattr(model,'available',lambda:True)
    def read(path,payload):
        payloads.append(payload)
        return {'choices':[{'message':{'content':json.dumps({'kind':'python','code':source})}}]}
    monkeypatch.setattr(model,'_read',read)
    r=Registry(tmp_path/'state');b=Builder(r,Executor(),model)
    try:
        b.request('build_request_syntax','srt.readonly.inspect');b.tasks.join()
        status=b.status('build_request_syntax')
        assert status['status']=='FAILED' and status['error']=='PYTHON_SYNTAX'
        assert len(payloads)==3 and status['repairs']==2
        for payload in payloads[1:]:
            prompt=payload['messages'][0]['content']
            assert 'unindent does not match any outer indentation level' in prompt
            assert '"line":3' in prompt and '"column":' in prompt
            assert '"text":"  y = 2"' in prompt and source in prompt
        assert 'Validation diagnostics' not in payloads[0]['messages'][0]['content']
        reports=[json.loads(row[0]) for row in r.db.execute('SELECT report FROM validations')]
        assert len(reports)==3 and all(v['test_count']==0 for v in reports)
        assert all(v['diagnostic']['line']==3 for v in reports)
        assert r.db.execute('SELECT count(*) FROM active').fetchone()[0]==0
        assert all(a['prompt_version']=='srt-builder-v5' for a in status['attempts'])
    finally:b.close();r.close()
