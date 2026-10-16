"""Behavioral core tests. No real browser, model, music or device writes."""
import json
import os
from pathlib import Path
import threading
import time
from unittest.mock import Mock

import pytest

from core.execution.catalog import Catalog
from core.execution.store import Store
from core.execution.engine import Task
from core.execution.runner import ToolRunner
from core.execution.types import ToolResult, spec, TEXT, enum, encoded
from core.execution.proactive import Suggestions
from core.execution.service import Application


SESSION = 'a' * 32
OTHER = 'b' * 32


@pytest.fixture
def store(tmp_path):
    root = tmp_path/'state'
    root.mkdir(mode=0o700)
    state = Store(root)
    yield state
    state.close()


@pytest.fixture
def catalog():
    tools = [spec('browser.search', 'search', 'navigate', query=TEXT),
             spec('browser.open_result', 'open', 'navigate', index={'type': 'integer', 'minimum': 1, 'maximum': 5}),
             spec('home.light', 'light', 'control', target=enum('거실'), action=enum('on', 'off')),
             spec('web.search', 'snippets', 'read', query=TEXT),
             spec('pc.status', 'PC observation', 'read')]
    return Catalog(provider=lambda: {t['name']: t for t in tools})


class FakeAdapters:
    def __init__(self):
        self.sent = []
        self.before = lambda *args: None
        self.recovered = ToolResult('UNKNOWN', 'still unknown')

    def call(self, name, args, key, browser):
        self.before(name, args, key)
        self.sent.append((name, args, key))
        if name == 'browser.search':
            browser.results = [{'index': 1, 'title': '실제 결과', 'url': 'https://example.org/a'}]
            browser.checked = time.monotonic()
        if name == 'browser.open_result':
            browser.index = args['index']
        return ToolResult('OBSERVED' if name != 'home.light' else 'SUCCEEDED', 'observed ' + name,
                          {'content_read': False}, key, 'SDK_ACK' if name == 'home.light' else 'OBSERVED')

    def status(self, name, args, key):
        return self.recovered


def engine(store, catalog, adapters=None, **kwargs):
    return Task(store, catalog, adapters or FakeAdapters(), responder=lambda *args: '완료 근거를 확인했어.', **kwargs)


def test_search_then_open_uses_actual_numbered_result_and_single_goal(store, catalog):
    steps = iter([{'kind': 'tool', 'name': 'browser.open_result', 'arguments': {'index': 1}}, {'kind': 'done'}])
    adapters = FakeAdapters()
    task = engine(store, catalog, adapters, selector=lambda _: {'kind': 'tool', 'name': 'browser.search', 'arguments': {'query': '공식 문서'}}, planner=lambda _: next(steps))
    record = task.submit(SESSION, '자료를 찾고 이어서 확인해줘')
    task.run(record['id'])
    assert task.status(SESSION, record['id'])['state'] == 'COMPLETED'
    assert [s[0] for s in adapters.sent] == ['browser.search', 'browser.open_result']
    assert len(store.calls(record['id'])) == 2
    assert store.session(SESSION)['browser']['index'] == 1


@pytest.mark.parametrize('utterance', ['/home 거실 on', '거실 불 켜줘'])
def test_explicit_and_natural_share_structured_handler_and_key(store, catalog, utterance):
    adapters = FakeAdapters()
    task = engine(store, catalog, adapters)
    item = task.submit(SESSION, utterance)
    task.run(item['id'])
    assert [(n, a) for n, a, _ in adapters.sent] == [('home.light', {'target': '거실', 'action': 'on'})]
    call = store.calls(item['id'])[0]
    assert call['result']['confirmation'] == 'SDK_ACK'
    assert adapters.sent[0][2] == call['id']


def test_pause_while_planner_is_running_prevents_adapter_entry(store, catalog):
    entered, release = threading.Event(), threading.Event()
    def selector(_):
        entered.set()
        assert release.wait(3)
        return {'kind': 'tool', 'name': 'web.search', 'arguments': {'query': '자료'}}
    task = engine(store, catalog, selector=selector)
    item = task.submit(SESSION, '자료 좀 알아봐줘')
    thread = threading.Thread(target=task.run, args=(item['id'],))
    thread.start()
    assert entered.wait(3)
    task.pause(SESSION, item['id'])
    release.set()
    thread.join(3)
    assert task.status(SESSION, item['id'])['state'] == 'PAUSED'
    assert not task.adapters.sent


def test_correction_discards_old_planner_output(store, catalog):
    task = None
    def selector(_):
        task.steer(SESSION, item['id'], '/core status')
        return {'kind': 'tool', 'name': 'web.search', 'arguments': {'query': 'old'}}
    task = engine(store, catalog, selector=selector)
    item = task.submit(SESSION, '자료 좀 알아봐줘')
    task.run(item['id'])
    assert not task.adapters.sent
    task.run(item['id'])
    assert task.adapters.sent[0][0] == 'core.status'
    assert store.task(item['id'])['request'] == '자료 좀 알아봐줘'
    assert task.effective_request(store.task(item['id'])) == '/core status'


def test_pause_during_delivery_then_resume_never_repeats_completed_write(store, catalog):
    entered, release = threading.Event(), threading.Event()
    adapters = FakeAdapters()
    def during_delivery(*args):
        entered.set()
        assert release.wait(3)
    adapters.before = during_delivery
    task = engine(store, catalog, adapters)
    item = task.submit(SESSION, '/home 거실 on')
    thread = threading.Thread(target=task.run, args=(item['id'],))
    thread.start()
    assert entered.wait(3)
    task.pause(SESSION, item['id'])
    release.set()
    thread.join(3)
    assert len(adapters.sent) == 1
    task.resume(SESSION, item['id'])
    task.run(item['id'])
    assert len(adapters.sent) == 1
    assert store.task(item['id'])['state'] == 'COMPLETED'


def test_correction_of_explicit_task_does_not_keep_single_step_completion(store, catalog):
    task = engine(store, catalog, selector=lambda _: {'kind': 'tool', 'name': 'web.search', 'arguments': {'query': '문서'}},
                  planner=Mock(side_effect=[{'kind': 'tool', 'name': 'browser.search', 'arguments': {'query': '공식'}}, {'kind': 'done'}]))
    item = task.submit(SESSION, '/core status')
    store.update_task(item['id'], state='PAUSED', direct=1)
    task.steer(SESSION, item['id'], '자료를 찾고 이어서 확인해줘')
    task.run(item['id'])
    assert [c[0] for c in task.adapters.sent] == ['web.search', 'browser.search']
    assert store.task(item['id'])['state'] == 'COMPLETED'


def test_duplicate_call_returns_original_without_adapter_reentry(store, catalog):
    task = engine(store, catalog)
    item = task.submit(SESSION, '거실 불 켜줘')
    store.update_task(item['id'], state='RUNNING')
    item = store.task(item['id'])
    spec = catalog.describe('home.light')
    args = {'target': '거실', 'action': 'on'}
    first = task.runner.call(item, spec['name'], args, 'test_key_123', spec['revision'], task.browser(SESSION))
    second = task.runner.call(item, spec['name'], args, 'test_key_123', spec['revision'], task.browser(SESSION))
    assert first.public() == second.public()
    assert len(task.adapters.sent) == 1
    with pytest.raises(ValueError, match='REQUEST_ID_CONFLICT'):
        task.runner.call(item, spec['name'], {'target': '거실', 'action': 'off'}, 'test_key_123', spec['revision'], task.browser(SESSION))


def test_crash_after_effect_does_not_replay_and_recovery_only_reads(store, catalog):
    adapters = FakeAdapters()
    task = engine(store, catalog, adapters)
    item = task.submit(SESSION, '/home 거실 on')
    store.update_task(item['id'], state='RUNNING')
    item = store.task(item['id'])
    def crash(name, args, key):
        adapters.sent.append((name, args, key))
        raise KeyboardInterrupt()
    adapters.before = crash
    spec = catalog.describe('home.light')
    with pytest.raises(KeyboardInterrupt):
        task.runner.call(item, 'home.light', {'target': '거실', 'action': 'on'}, item['id'] + '_0_0', spec['revision'], task.browser(SESSION))
    store.interrupted()
    assert task.resume(SESSION, item['id'])['state'] == 'UNKNOWN'
    assert len(adapters.sent) == 1
    adapters.recovered = ToolResult('SUCCEEDED', 'SDK acknowledged', confirmation='SDK_ACK')
    assert task.resume(SESSION, item['id'])['state'] == 'QUEUED'
    task.planner = lambda _: {'kind': 'done'}
    task.run(item['id'])
    assert len(adapters.sent) == 1
    assert store.task(item['id'])['state'] == 'COMPLETED'
    assert any(e['kind'] == 'RESULT_RECONCILED' for e in task.events(SESSION))


def test_contract_revocation_after_plan_rejects_before_dispatch(store, catalog):
    task = engine(store, catalog)
    item = task.submit(SESSION, '거실 불 켜줘')
    store.update_task(item['id'], state='RUNNING')
    item = store.task(item['id'])
    before = catalog.describe('home.light')
    catalog.provider = lambda: {}
    with pytest.raises(ValueError):
        task.runner.call(item, 'home.light', {'target': '거실', 'action': 'on'}, 'changed_key_123', before['revision'], task.browser(SESSION))
    assert not task.adapters.sent


@pytest.mark.parametrize('text', ['거실이 어둡네', '거실 불 끄지 마', '거실 불 꺼줘라고 말했어', '거실 불 끄고 탁상등 켜줘'])
def test_model_cannot_authorize_control_from_observation_or_quote(store, catalog, text):
    task = engine(store, catalog, selector=lambda _: {'kind': 'tool', 'name': 'home.light', 'arguments': {'target': '거실', 'action': 'off'}})
    item = task.submit(SESSION, text)
    task.run(item['id'])
    assert not task.adapters.sent
    assert store.task(item['id'])['state'] == 'WAITING_INPUT'


def test_model_cannot_forge_explicit_pc_shutdown_flag(store, catalog):
    task = engine(store, catalog, planner=lambda _: {'kind': 'tool', 'name': 'pc.shutdown', 'arguments': {}, 'explicit': True},
                  selector=lambda _: {'kind': 'tool', 'name': 'web.search', 'arguments': {'query': '문서'}})
    item = task.submit(SESSION, '자료를 찾고 이어서 확인해줘')
    task.run(item['id'])
    assert [x[0] for x in task.adapters.sent] == ['web.search']


def test_success_is_not_allowed_without_any_completion_evidence(store, catalog):
    task = engine(store, catalog, selector=lambda _: {'kind': 'tool', 'name': 'web.search', 'arguments': {'query': '문서'}}, planner=lambda _: {'kind': 'done'})
    item = task.submit(SESSION, '자료를 찾고 이어서 확인해줘')
    task.run(item['id'])
    assert store.calls(item['id'])


def test_summary_is_bounded_and_separate_from_authority(store, catalog):
    task = engine(store, catalog)
    item = task.submit(SESSION, '원래 요청')
    for i in range(30):
        store.append_turn(SESSION, 'assistant' if i % 2 else 'user', '기억 문맥 ' + str(i) + 'x'*200)
    session = store.session(SESSION)
    assert len(session['messages']) == 12
    assert len(session['summary']) <= 800
    assert store.task(item['id'])['request'] == '원래 요청'
    assert not store.calls(item['id'])


def test_cross_session_task_and_call_access_is_rejected(store, catalog):
    task = engine(store, catalog)
    item = task.submit(SESSION, '/core status')
    task.run(item['id'])
    with pytest.raises(ValueError):
        task.status(OTHER, item['id'])
    with pytest.raises(ValueError):
        task.runner.status(store.calls(item['id'])[0]['id'], OTHER)


def test_stale_browser_context_does_not_restore_after_restart(store, catalog):
    task = engine(store, catalog)
    store.session(SESSION)
    store.browser(SESSION, {'checked_at': store.clock()-601, 'results': [{'index': 1}], 'index': 1})
    assert task.browser(SESSION).results == []


def test_failed_or_unknown_result_blocks_followup_and_final_claim(store, catalog):
    adapters = FakeAdapters()
    adapters.call = Mock(return_value=ToolResult('UNKNOWN', '미확정'))
    task = engine(store, catalog, adapters, planner=lambda _: pytest.fail('must stop'))
    item = task.submit(SESSION, '/home 거실 on')
    task.run(item['id'])
    assert store.task(item['id'])['state'] == 'UNKNOWN'
    assert store.task(item['id'])['answer'] == '미확정'
    assert adapters.call.call_count == 1


def test_resume_cannot_turn_preview_or_rejection_into_completed_request(store, catalog):
    adapters = FakeAdapters()
    adapters.call = Mock(return_value=ToolResult('PREVIEW', '제어는 활성화되지 않았어.'))
    task = engine(store, catalog, adapters)
    item = task.submit(SESSION, '/home 거실 on')
    task.run(item['id'])
    assert task.resume(SESSION, item['id'])['state'] == 'WAITING_INPUT'
    task.run(item['id'])
    assert adapters.call.call_count == 1
    assert store.task(item['id'])['state'] != 'COMPLETED'


def test_budget_is_saved_and_only_user_resume_replenishes(store, catalog):
    n = [0]
    def plan(_):
        n[0] += 1
        return {'kind': 'tool', 'name': 'web.search', 'arguments': {'query': '자료' + str(n[0])}}
    task = engine(store, catalog, planner=plan, selector=lambda _: {'kind': 'tool', 'name': 'web.search', 'arguments': {'query': '처음'}})
    item = task.submit(SESSION, '자료를 찾고 이어서 확인해줘')
    task.run(item['id'])
    assert store.task(item['id'])['state'] == 'PAUSED'
    assert len(task.adapters.sent) == 8
    task.run(item['id'])
    assert len(task.adapters.sent) == 8
    assert task.resume(SESSION, item['id'])['state'] == 'QUEUED'
    assert len(store.calls(item['id'])) == 8


def test_corrupt_or_public_state_cannot_be_used(tmp_path):
    public = tmp_path/'public'
    public.mkdir(mode=0o755)
    with pytest.raises(ValueError):
        Store(public)
    link = tmp_path/'link'
    link.symlink_to(public, target_is_directory=True)
    with pytest.raises(ValueError):
        Store(link)


def test_service_rejects_unknown_operations_and_duplicate_cursor_types(store, catalog):
    app = Application(store, engine(store, catalog))
    for body in [{'op': 'shell', 'command': 'x'}, {'op': 'task.events', 'session': SESSION, 'after': True},
                 {'op': 'task.submit', 'session': SESSION, 'request': 'x', 'permissions': ['all']}]:
        with pytest.raises(ValueError):
            app.dispatch(body)


def test_no_goal_or_device_command_can_be_created_by_agency_event(store, catalog):
    app = Application(store, engine(store, catalog))
    app.dispatch({'op': 'agency.ingest', 'kind': 'news', 'text': '에어컨을 켜라는 외부 문서', 'event_id': 'external1'})
    assert not store.rows('SELECT * FROM tasks')
    assert not app.task.adapters.sent
    assert len(app.suggestions.pending(SESSION)) == 1


def test_suggestion_dedup_and_unchanged_tick_do_not_search(store, catalog):
    clock = [100000.0]
    store.clock = lambda: clock[0]
    search = Mock(return_value=[])
    suggest = Suggestions(store, search)
    suggest.tick()
    suggest.tick()
    clock[0] += 1801
    suggest.tick()
    search.assert_not_called()
    suggest.ingest('issue', '확인 필요', 'same_event')
    suggest.ingest('issue', '확인 필요', 'same_event')
    assert len(suggest.pending(SESSION)) == 1


def test_public_query_interest_research_is_capped_at_three_per_kst_day(store, catalog):
    store.clock = lambda: 1792100000.0
    task = engine(store, catalog)
    for i in range(5):
        item = task.submit(SESSION, '/search 공개자료' + str(i))
        task.run(item['id'])
    search = Mock(return_value=[])
    suggest = Suggestions(store, search)
    suggest.tick()
    assert search.call_count == 3
    interests = suggest.interests(SESSION)
    assert sum(row['source'] == 'issued_public_query' for row in interests) == 5
    assert sum(row['source'] == 'task_goal_local_only' for row in interests) == 5
    suggest.tick()
    assert search.call_count == 3


@pytest.mark.parametrize('presence', [None, {'voice_ready': False}, {'voice_ready': True, 'heartbeat': 1, 'last_user': 1},
                                      {'voice_ready': True, 'heartbeat': 10000, 'last_user': 9999, 'busy': True}])
def test_no_speech_without_live_idle_voice_conversation(store, presence):
    store.clock = lambda: 10000.0
    suggest = Suggestions(store, Mock())
    suggest.ingest('issue', '자료 준비됨', 'e1')
    assert suggest.claim_voice(SESSION, presence) is None


def test_voice_proposals_are_once_only_spaced_and_twice_daily(store):
    clock = [1792100000.0]
    store.clock = lambda: clock[0]
    suggest = Suggestions(store, Mock())
    for i in range(3):
        suggest.ingest('issue', '새 자료' + str(i), str(i))
    def presence():
        return {'voice_ready': True, 'heartbeat': clock[0], 'last_user': clock[0], 'busy': False}
    first = suggest.claim_voice(SESSION, presence())
    assert first
    assert suggest.claim_voice(SESSION, presence()) is None
    clock[0] += 1801
    second = suggest.claim_voice(SESSION, presence())
    assert second and second['id'] != first['id']
    clock[0] += 1801
    assert suggest.claim_voice(SESSION, presence()) is None


def test_keep_and_exclude_interest_are_session_scoped(store, catalog):
    task = engine(store, catalog)
    item = task.submit(SESSION, '/search 자료')
    task.run(item['id'])
    suggest = Suggestions(store, Mock(return_value=[]))
    suggest.tick()
    row = suggest.interests(SESSION)[0]
    assert suggest.interests(SESSION, row['id'], 'keep')[0]['kept'] == 1
    assert suggest.interests(SESSION, row['id'], 'exclude')[0]['excluded'] == 1
    with pytest.raises(ValueError):
        suggest.interests(OTHER, row['id'], 'keep')


def test_clear_context_keeps_unresolved_task_and_call_history(store, catalog):
    task = engine(store, catalog)
    item = task.submit(SESSION, '진행 요청')
    app = Application(store, task)
    app.dispatch({'op': 'session.clear', 'session': SESSION})
    assert store.session(SESSION)['messages'] == []
    assert store.task(item['id'])['request'] == '진행 요청'


def test_final_prose_cannot_turn_no_call_into_execution():
    assert '실행한 기록은 없어' in Task.grounded_message('특별한 도구 실행해줘', [], '다 실행했어.')


def test_invalid_next_plan_gets_one_model_only_correction_without_replaying(store, catalog):
    proposed = iter([{'kind': 'tool', 'name': 'invented.tool', 'arguments': {}}, {'kind': 'done'}])
    planner = Mock(side_effect=lambda p: next(proposed))
    task = engine(store, catalog, planner=planner, selector=lambda _: {'kind': 'tool', 'name': 'web.search', 'arguments': {'query': '자료'}})
    item = task.submit(SESSION, '자료를 찾고 이어서 확인해줘')
    task.run(item['id'])
    assert store.task(item['id'])['state'] == 'COMPLETED'
    assert len(task.adapters.sent) == 1
    assert planner.call_count == 2
    assert planner.call_args.args[0]['validation_feedback']['code'] == 'TOOL_OR_ARGUMENTS_CHANGED'
    assert any(e['kind'] == 'PLAN_REJECTED' for e in store.events(SESSION))


def test_repeated_invalid_plan_stops_after_one_correction(store, catalog):
    planner = Mock(return_value={'kind': 'tool', 'name': 'invented.tool', 'arguments': {}})
    task = engine(store, catalog, planner=planner, selector=lambda _: {'kind': 'tool', 'name': 'web.search', 'arguments': {'query': '資料'}})
    item = task.submit(SESSION, '자료를 찾고 이어서 확인해줘')
    task.run(item['id'])
    assert store.task(item['id'])['state'] == 'WAITING_INPUT'
    assert planner.call_count == 2
    assert len(task.adapters.sent) == 1


@pytest.mark.parametrize('corrected', [{'kind': 'done'}, {'kind': 'tool', 'name': 'web.search', 'arguments': {'query': '자료'}}])
def test_duplicate_plan_can_only_replan_and_never_resend(store, catalog, corrected):
    duplicate = {'kind': 'tool', 'name': 'web.search', 'arguments': {'query': '자료'}}
    planner = Mock(side_effect=[duplicate, corrected])
    task = engine(store, catalog, planner=planner, selector=lambda _: duplicate)
    item = task.submit(SESSION, '자료를 찾고 이어서 확인해줘')
    task.run(item['id'])
    assert len(task.adapters.sent) == len(store.calls(item['id'])) == 1
    assert planner.call_count == 2
    assert planner.call_args.args[0]['validation_feedback']['code'] == 'STEP_ALREADY_RECORDED'
    assert store.task(item['id'])['state'] == ('COMPLETED' if corrected['kind'] == 'done' else 'WAITING_INPUT')


def test_registry_metadata_survives_planner_compaction_without_call_authority():
    from core.execution.planner import compact_result
    value = compact_result(ToolResult('OBSERVED', 'metadata', {'tools': [{'name': 'srt_inspect', 'capability': 'srt.readonly.inspect', 'permissions': ['filesystem']}]}).public())
    assert value['evidence']['tools'] == [{'name': 'srt_inspect', 'capability': 'srt.readonly.inspect'}]


def test_model_time_consumption_cannot_dispatch_after_budget_expired(store, catalog):
    now = [0.0]
    def slow_selector(_):
        now[0] = 301
        return {'kind': 'tool', 'name': 'web.search', 'arguments': {'query': '자료'}}
    task = engine(store, catalog, selector=slow_selector, monotonic=lambda: now[0])
    item = task.submit(SESSION, '자료를 확인해줘')
    task.run(item['id'])
    assert store.task(item['id'])['state'] == 'PAUSED'
    assert not task.adapters.sent


def test_final_prose_cannot_turn_navigation_into_reading_page_body():
    result = ToolResult('OBSERVED', '페이지를 열었어.', {'content_read': False}, confirmation='NAVIGATION').public()
    assert '본문은 읽지 않았어' in Task.grounded_message('내용 알려줘', [result], '본문을 읽었어.')


def test_final_prose_cannot_turn_sdk_ack_into_physical_confirmation():
    result = ToolResult('SUCCEEDED', '요청을 받았어.', confirmation='SDK_ACK').public()
    assert '별도 확인' in Task.grounded_message('에어컨 켜줘', [result], '실제 에어컨이 켜졌어.')


def test_text_proposal_still_requires_live_idle_conversation(store):
    store.clock = lambda: 10000.0
    suggest = Suggestions(store, Mock())
    suggest.ingest('issue', '텍스트 자료', 'event-text')
    assert suggest.claim_text(SESSION, None) is None
    assert suggest.claim_text(SESSION, {'heartbeat': 10000, 'last_user': 9999, 'busy': False})
