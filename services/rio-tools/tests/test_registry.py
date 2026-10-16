import copy
import json
from pathlib import Path
import pytest
from core.tools.contracts import ToolError, digest, validate_package, schema_check
from rio_tools.registry import Registry
from rio_tools.srt import package
from rio_tools.validator import static_check

IMAGE='sha256:'+'a'*64
REPORT={'static_pass':True,'fixed_tests_pass':True,'isolation_pass':True}


@pytest.fixture
def registry(tmp_path):
    r=Registry(tmp_path/'state')
    yield r
    r.close()


def activate(r,p):
    key=r.candidate(p);r.validated(key,REPORT);r.activate(key);return key


def test_candidate_not_callable_before_validation(registry):
    key=registry.candidate(package(IMAGE))
    with pytest.raises(ToolError,match='TOOL_NOT_REGISTERED'):registry.describe('srt_inspect')
    with pytest.raises(ToolError,match='VALIDATION_REQUIRED'):registry.activate(key)


def test_duplicate_and_version_conflict(registry):
    p=package(IMAGE);key=registry.candidate(p)
    assert registry.candidate(copy.deepcopy(p))==key
    p['files']['tool.py']+='\n# changed\n'
    with pytest.raises(ToolError,match='VERSION_ALREADY_EXISTS'):registry.candidate(p)


def test_permissions_not_granted_by_test_success(registry):
    p=package(IMAGE);p['contract']['permissions'].append('device.control')
    key=registry.candidate(p);registry.validated(key,REPORT)
    with pytest.raises(ToolError,match='APPROVAL_REQUIRED'):registry.activate(key)
    assert registry.describe('srt_inspect',active_only=False)['state']=='PENDING_APPROVAL'


def test_modified_package_rejected(registry):
    key=activate(registry,package(IMAGE));p=registry.packages/(key+'.json')
    p.chmod(0o600);data=json.loads(p.read_text());data['files']['tool.py']+='\n# tampered'
    p.write_text(json.dumps(data))
    with pytest.raises(ToolError,match='PACKAGE_HASH_MISMATCH'):registry.load_package(key)


def test_symlink_and_path_escape_rejected(registry,tmp_path):
    p=package(IMAGE);p['files']['../escape.py']=p['files'].pop('tool.py')
    with pytest.raises(ToolError,match='PACKAGE_PATH'):registry.candidate(p)
    key=registry.candidate(package(IMAGE));path=registry.packages/(key+'.json')
    raw=path.read_bytes();other=tmp_path/'external';other.write_bytes(raw)
    path.unlink();path.symlink_to(other)
    with pytest.raises(ToolError,match='PACKAGE_READ_FAILED'):registry.load_package(key)


def test_failed_revalidation_removes_active_pointer(registry):
    key=activate(registry,package(IMAGE));registry.validated(key,{'static_pass':True})
    with pytest.raises(ToolError,match='TOOL_NOT_REGISTERED'):registry.describe('srt_inspect')


def test_rollbacks_and_disable(registry):
    a=activate(registry,package(IMAGE));b=activate(registry,package(IMAGE,version='1.0.1'))
    assert registry.describe('srt_inspect')['hash']==b
    registry.rollback('srt_inspect',a)
    assert registry.describe('srt_inspect')['hash']==a
    registry.suspend(a)
    with pytest.raises(ToolError):registry.describe('srt_inspect')


def test_repeat_errors_quarantine(registry):
    key=activate(registry,package(IMAGE))
    for _ in range(3):registry.record_error(key)
    with pytest.raises(ToolError):registry.describe('srt_inspect')
    assert registry.describe('srt_inspect',active_only=False)['state']=='QUARANTINED'


def test_restart_keeps_tools_without_model_identity(tmp_path):
    root=tmp_path/'state';r=Registry(root);key=activate(r,package(IMAGE));r.close()
    r=Registry(root)
    assert r.describe('srt_inspect')['hash']==key
    assert 'model' not in r.describe('srt_inspect')['contract']
    r.close()


@pytest.mark.parametrize('key',['$ref','$dynamicRef','pattern','patternProperties'])
def test_schema_never_fetches_remote_or_evaluates_regex(key):
    with pytest.raises(ToolError):schema_check({'type':'string',key:'https://example.com/no-fetch'})


@pytest.mark.parametrize('source',['import os\n','import subprocess\n','eval("1")\n','x.__class__\n'])
def test_static_exec_rejected(source):
    with pytest.raises(ToolError):static_check(package(IMAGE,source))


def test_bash_eval_rejected():
    with pytest.raises(ToolError):static_check(package(IMAGE,'eval "$input"',kind='bash'))


def test_template_static_passes():
    static_check(package(IMAGE))


def test_syntax_feedback_is_bounded_source_context_and_does_not_execute(registry):
    from rio_tools.validator import Validator
    source='if True:\n    x = 1\n  y = 2\n'+'#'+'x'*1000+'\n'
    key=registry.candidate(package(IMAGE,source))
    class NoExecution:
        def run_package(self,*args,**kwargs):raise AssertionError('invalid source must not run')
        def execute(self,*args,**kwargs):raise AssertionError('invalid source must not run')
    report=Validator(registry,NoExecution()).validate(key)
    assert report['error_code']=='PYTHON_SYNTAX' and report['test_count']==0
    diagnostic=report['diagnostic']
    assert diagnostic['line']==3 and isinstance(diagnostic['column'],int)
    assert diagnostic['message']=='unindent does not match any outer indentation level'
    assert len(diagnostic['context'])<=5
    assert all(len(v['text'])<=240 for v in diagnostic['context'])
    assert registry.load_package(key)['files']['tool.py']==source
    with pytest.raises(ToolError,match='VALIDATION_REQUIRED'):registry.activate(key)


@pytest.mark.parametrize('value,code',[
    ({'kind':'python','code':'not metrics'},'INVALID_OUTPUT'),
    ({'segments':1,'valid_segments':0,'total_duration_ms':0,'timeline_duration_ms':0,
      'invalid_times':[],'overlap_count':0},'FIXED_TEST_FAILED')])
def test_function_feedback_exposes_fields_without_oracle_inputs_or_answers(registry,value,code):
    from rio_tools.validator import Validator
    class Executor:
        def run_package(self,*args,**kwargs):return {'result':value}
        def execute(self,*args,**kwargs):raise AssertionError('failed metrics must not activate')
    key=registry.candidate(package(IMAGE))
    report=Validator(registry,Executor()).validate(key)
    assert report['error_code']==code and report['test_count']==0
    diagnostic=report['diagnostic']
    assert not {'input','expected','actual','result','text'} & set(diagnostic)
    if code=='INVALID_OUTPUT':
        assert diagnostic['unexpected_fields']==['kind','code']
        assert 'segments' in diagnostic['missing_fields']
    else:assert diagnostic['mismatched_fields']==['segments']
    with pytest.raises(ToolError,match='VALIDATION_REQUIRED'):registry.activate(key)
