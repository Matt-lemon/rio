import json
import pytest
from core.tools.main_adapter import PublicTools,handle_tools_command
from core.tools.contracts import ToolError

class Client:
    def __init__(self):self.calls=[]
    def request(self,action,**args):self.calls.append((action,args));return {'ok':True,'result':{'status':'QUEUED'}}

def test_explicit_command_and_json_never_evaluate_shell():
    c=Client();reply=handle_tools_command('/tools call srt_inspect request_001 {"text":"$(touch /tmp/no)"}',c)
    assert json.loads(reply)['result']['status']=='QUEUED'
    assert c.calls==[('call',{'name':'srt_inspect','request_id':'request_001','args':{'text':'$(touch /tmp/no)'}})]

def test_no_model_approval_or_arbitrary_build_scope():
    c=Client();tools=PublicTools(c)
    with pytest.raises(ToolError):tools.call('tools_approve',{'hash':'x'})
    with pytest.raises(ToolError):tools.call('tools_build',{'request_id':'request_001','capability':'system.change'})
    assert c.calls==[]

def test_other_commands_and_unknown_actions_preserved():
    c=Client();assert handle_tools_command('/pc off',c) is None
    assert handle_tools_command('/home room on',c) is None
    assert '도구 명령' in handle_tools_command('/tools approve hash',c)
    assert c.calls==[]
