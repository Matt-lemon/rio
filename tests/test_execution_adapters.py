import json
import time
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from core.execution.adapters import Adapters
from core.execution.catalog import Catalog
from core.execution.store import Store
from core.execution.types import ToolResult, spec, TEXT
from core.execution.service import Application
from core.execution.engine import Task


@pytest.fixture
def state(tmp_path):
    p = tmp_path/'private'
    p.mkdir(mode=0o700)
    store = Store(p)
    yield store
    store.close()


def generated(**changes):
    return {'state': 'ACTIVE', 'validated': 1, 'hash': 'a'*64,
            'contract': {'name': 'srt_inspect', 'description': 'SRT inspect', 'kind': 'python',
                         'capability': 'srt.readonly.inspect', 'permissions': ['input.read', 'workspace.compute'],
                         'input_schema': {'type': 'object', 'properties': {'text': {'type': 'string'}},
                                          'required': ['text'], 'additionalProperties': False}}, **changes}


def test_registered_tool_becomes_discoverable_without_editing_dispatch():
    catalog = Catalog(provider=lambda: {}, registry_provider=lambda: [generated()])
    descriptor = catalog.describe('registered.srt_inspect')
    assert descriptor['package_hash'] == 'a'*64
    assert descriptor['effect'] == 'compute'
    catalog.check('registered.srt_inspect', {'text': '1\n00:00:00,000 --> 00:00:01,000\nHello'})


@pytest.mark.parametrize('query', ['SRT 검사', '자막 도구'])
def test_registry_search_mixed_language_finds_existing_metadata_once(state, monkeypatch, query):
    lookup = Mock(return_value=[generated(), {'name': 'unrelated', 'description': 'other'}])
    monkeypatch.setattr('core.execution.adapters.registry_request', lookup)
    result = Adapters(state).call('registry.search', {'query': query}, 'search_key_123', None)
    lookup.assert_called_once_with('list', search='')
    assert [r['name'] for r in result.evidence['tools']] == ['srt_inspect']
    assert result.confirmation == 'REGISTRY_METADATA'


@pytest.mark.parametrize('row', [generated(state='QUARANTINED'), generated(validated=0), generated(hash='bad'),
                               {'state': 'ACTIVE', 'validated': 1, 'hash': 'a'*64,
                                'contract': {**generated()['contract'], 'permissions': ['device.control']}},
                               {'state': 'ACTIVE', 'validated': 1, 'hash': 'a'*64,
                                'contract': {**generated()['contract'], 'capability': 'arbitrary.code'}}])
def test_unverified_or_expanded_generated_tools_are_never_exposed(row):
    assert Catalog(provider=lambda: {}, registry_provider=lambda: [row]).describe('registered.srt_inspect') is None


def test_reused_generated_call_pins_version_and_never_builds(state, monkeypatch):
    def reply(action, **args):
        if action == 'capabilities':
            return {'expected_hash': True}
        if action == 'describe':
            return generated()
        assert action == 'call'
        assert args == {'name': 'srt_inspect', 'args': {'text': 'user text'}, 'request_id': 'call_key_123', 'expected_hash': 'a'*64}
        return {'request_id': 'call_key_123', 'package_hash': 'a'*64, 'status': 'SUCCEEDED', 'result': {'result': {'segments': 1}}}
    callback = Mock(side_effect=reply)
    monkeypatch.setattr('core.execution.adapters.registry_request', callback)
    result = Adapters(state).call('registered.srt_inspect', {'text': 'user text'}, 'call_key_123', None)
    assert result.confirmation == 'VALIDATED_OUTPUT'
    assert [c.args[0] for c in callback.call_args_list] == ['capabilities', 'describe', 'call']


def test_generated_version_change_between_final_check_and_adapter_cannot_run(state, monkeypatch):
    lookup = Mock(side_effect=[{'expected_hash': True}, generated(hash='b'*64)])
    monkeypatch.setattr('core.execution.adapters.registry_request', lookup)
    result = Adapters(state).call_checked({'name': 'registered.srt_inspect', 'package_hash': 'a'*64}, {'text': 'x'}, 'call_key_123', None)
    assert result.status == 'REJECTED' and result.code == 'TOOL_REVISION_CHANGED'
    assert [c.args[0] for c in lookup.call_args_list] == ['capabilities', 'describe']


def test_home_control_uses_core_request_id_and_preserves_sdk_not_physical(state, monkeypatch):
    registry = Mock()
    registry.call.return_value = {'status': 'DISPATCHED', 'request_id': 'core_request_01'}
    registry.wait_result.return_value = {'status': 'SUCCEEDED', 'request_id': 'core_request_01', 'physical_state_confirmed': False}
    monkeypatch.setattr('core.home.main_adapter.registry_from_environment', lambda: registry)
    result = Adapters(state).call('home.light', {'target': '거실', 'action': 'off'}, 'core_request_01', None)
    registry.call.assert_called_once_with('home_set_light', {'target': '거실', 'on': False, 'idempotency_key': 'core_request_01'})
    assert result.confirmation == 'SDK_ACK'
    assert result.evidence['physical_state_confirmed'] is False
    registry.close.assert_called_once()


def test_home_status_does_not_claim_health_on_partial_permission(state, monkeypatch):
    registry = Mock()
    registry.call.return_value = {'available': True, 'permission': 'REVOKED', 'bridge_state': 'READY'}
    monkeypatch.setattr('core.home.main_adapter.registry_from_environment', lambda: registry)
    result = Adapters(state).call('home.status', {}, 'read_key_123', None)
    assert result.status == 'UNAVAILABLE'


def test_another_conversation_cannot_open_shared_browsers_number(state):
    with state.transaction() as db:
        db.execute("INSERT INTO meta VALUES('browser_owner','other-session')")
    browser = SimpleNamespace(session_id='this-session', run_result=Mock())
    result = Adapters(state).call('browser.open_result', {'index': 1}, 'read_key_123', browser)
    assert result.status == 'NEED_INPUT'
    browser.run_result.assert_not_called()


def test_browser_results_keep_content_read_false(state):
    browser = SimpleNamespace(session_id='this-session', run_result=lambda _: {'ok': True, 'status': 'RESULT_PAGE_OPENED', 'url': 'https://example.org', 'title': 'real title'})
    with state.transaction() as db:
        db.execute("INSERT INTO meta VALUES('browser_owner','this-session')")
    result = Adapters(state).call('browser.open_result', {'index': 1}, 'read_key_123', browser)
    assert result.confirmation == 'NAVIGATION'
    assert result.evidence['content_read'] is False


def test_context_clear_drops_implicit_targets_but_keeps_audit(state):
    from core.execution.types import encoded
    state.session('a'*32)
    task = state.create_task('a'*32, '거실 불 켜줘')
    result = ToolResult('SUCCEEDED', 'SDK acknowledged', confirmation='SDK_ACK').public()
    with state.transaction() as db:
        now = state.clock()
        db.execute('INSERT INTO calls(id,task_id,name,args,revision,effect,state,result,created,updated) VALUES(?,?,?,?,?,?,?,?,?,?)',
                   ('old_key_123', task['id'], 'home.light', encoded({'target': '거실', 'action': 'on'}), 'v', 'control', 'SUCCEEDED', encoded(result), now, now))
    engine = Task(state)
    assert engine.router('a'*32).events
    Application(state, engine).dispatch({'op': 'session.clear', 'session': 'a'*32})
    assert engine.router('a'*32).events == []
    assert len(state.calls(task['id'])) == 1


def test_voice_failure_is_recorded_without_repeating_reserved_suggestion(state):
    app = Application(state)
    session = 'a'*32
    client = 'b'*32
    app.suggestions.ingest('notice', '자료 준비됨', 'event_123')
    app.dispatch({'op': 'session.presence', 'session': session, 'client': client,
                  'busy': False, 'voice_ready': True, 'user_input': True})
    first = app.dispatch({'op': 'suggestions.claim', 'session': session, 'client': client})
    assert first
    app.dispatch({'op': 'suggestions.delivered', 'session': session, 'id': first['id'], 'audio_ok': False})
    assert app.dispatch({'op': 'suggestions.claim', 'session': session, 'client': client}) is None
    assert state.events(session)[0]['payload']['audio_ok'] is False


def test_proactive_does_not_search_private_task_labels(state):
    from core.execution.proactive import Suggestions
    session = 'a'*32
    state.session(session)
    task = state.create_task(session, '개인 집 이름이나 내부 경로가 포함된 작업')
    with state.transaction() as db:
        now = state.clock()
        db.execute('INSERT INTO calls(id,task_id,name,args,revision,effect,state,result,created,updated) VALUES(?,?,?,?,?,?,?,?,?,?)',
                   ('old_key_123', task['id'], 'core.status', '{}', 'v', 'read', 'OBSERVED', '{}', now, now))
    state.update_task(task['id'], state='COMPLETED')
    search = Mock()
    suggestions = Suggestions(state, search)
    suggestions.tick()
    search.assert_not_called()
    assert suggestions.interests(session)[0]['source'] == 'task_goal_local_only'


@pytest.mark.parametrize('link_state,stale,expected',[
    ('WAITING_FOR_PEER',False,'WAITING_FOR_PEER'),
    ('WAITING_FOR_API',False,'WAITING_FOR_API'),
    ('CONFIGURATION_ERROR',False,'CONFIGURATION_ERROR'),
    ('WAITING_FOR_API',True,'UNVERIFIED')])
def test_dubbing_failure_uses_only_fresh_supervisor_evidence(state,monkeypatch,link_state,stale,expected):
    monkeypatch.setattr('rio_dubbing_client.DubbingClient.health',Mock(side_effect=RuntimeError('not public')))
    monkeypatch.setattr('core.dubbing_link.read_status',lambda:{'state':link_state,'reason':'API_UNAVAILABLE','stale':stale})
    result=Adapters(state).call('dubbing.health',{},'read_link_123',None)
    assert result.status=='UNAVAILABLE' and result.evidence['link_state']==expected
    assert result.evidence['api_ready'] is False and 'not public' not in result.message
