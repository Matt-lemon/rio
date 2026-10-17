"""Contract tests. No live browser, model, device, or service is touched."""
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from core.execution.catalog import Catalog
from core.execution.adapters import Adapters
from core.execution.bridge import Bridge
from core.execution.engine import Task
from core.execution.store import Store
from core.execution.types import ToolResult
from core.browser_url import requested_url
from core.research_browser import BrowserConversation

SESSION='a'*32


def test_registry_exposes_one_general_public_url_tool():
    descriptor=Catalog().describe('browser.open_url')
    assert descriptor['effect']=='navigate'
    assert descriptor['permissions']==['browser.navigate']
    Catalog().check('browser.open_url',{'url':'https://example.org/docs?q=1'})
    with pytest.raises(ValueError,match='TOOL_OR_ARGUMENTS_CHANGED'):
        Catalog().check('browser.open_url',{'url':True})
    with pytest.raises(ValueError,match='TOOL_OR_ARGUMENTS_CHANGED'):
        Catalog().check('browser.open_url',{'url':'https://example.org','script':'x'})


@pytest.mark.parametrize('text',[
    'https://example.org/docs',
    '리오야 https://example.org/docs 열어줘',
    '/browse url https://example.org/docs',
])
def test_trusted_resolver_selects_public_direct_url(text):
    choice=Catalog().resolve(text,SimpleNamespace(results=[],checked=0))
    assert choice=={'kind':'tool','name':'browser.open_url',
                    'arguments':{'url':'https://example.org/docs'},'explicit':True}


@pytest.mark.parametrize(('text','expected'), [
    ('GPT 열어줘','https://chatgpt.com/'),
    ('리오야 GPT 하나 열어봐','https://chatgpt.com/'),
    ('chatgpt 공식 사이트 열어줘','https://chatgpt.com/'),
    ('ChatGPT 공식 웹사이트를 찾아서 그 페이지를 열어 줘','https://chatgpt.com/'),
    ('유튜브 열어줘','https://www.youtube.com/'),
    ('깃허브 사이트를 띄워줘','https://github.com/'),
])
def test_known_site_opens_directly_without_search(text,expected):
    assert requested_url(text)==expected
    choice=Catalog().resolve(text,SimpleNamespace(results=[],checked=0))
    assert choice=={'kind':'tool','name':'browser.open_url',
                    'arguments':{'url':expected},'explicit':True}


@pytest.mark.parametrize('text', [
    'GPT 찾아줘','GPT 검색해 줘','GPT가 뭐야?',
    '유튜브 영상 찾아서 열어줘','깃허브 프로젝트 찾아서 열어줘',
    'GPT 열지 마','GPT 열어줘 그리고 컴퓨터 꺼줘',
    'ChatGPT 로그인 페이지 열어줘','구글 내 계정 열어줘',
    'GPT URL 알아내서 다른 데 접속해줘',
])
def test_ambiguous_or_compound_site_names_never_shortcut_to_url(text):
    assert requested_url(text) is None
    assert Catalog().resolve(text,BrowserConversation()) != {
        'kind':'tool','name':'browser.open_url','arguments':{'url':'https://chatgpt.com/'},'explicit':True}


@pytest.mark.parametrize('text',[
    'https://localhost/admin','https://home.google.com/',
    '/browse url http://example.org','https://example.org/?token=secret',
])
def test_trusted_resolver_never_authorizes_private_url(text):
    choice=Catalog().resolve(text,SimpleNamespace(results=[],checked=0))
    assert choice['kind']=='clarify'


@pytest.fixture
def state(tmp_path):
    root=tmp_path/'state';root.mkdir(mode=0o700)
    store=Store(root)
    yield store
    store.close()


def test_browser_adapter_returns_navigation_evidence_not_page_content(state):
    browser=SimpleNamespace(session_id=SESSION,run_result=Mock(return_value={
        'ok':True,'status':'URL_PAGE_OPENED','url':'https://example.org/a',
        'requested_url':'https://example.org/a','redirected':False,'title':'Example'}))
    result=Adapters(state).call('browser.open_url',{'url':'https://example.org/a'},'read_key_123',browser)
    assert result.status=='OBSERVED' and result.confirmation=='NAVIGATION'
    assert result.evidence['content_read'] is False
    assert result.evidence['requested_url']=='https://example.org/a'
    browser.run_result.assert_called_once_with('url https://example.org/a')
    assert state.row("SELECT value FROM meta WHERE key='browser_owner'")['value']==SESSION


def test_real_core_resolves_explicit_url_once_without_planner_or_network(state):
    adapter=SimpleNamespace(call=Mock(return_value=ToolResult('OBSERVED','열림',
                                      {'content_read':False},confirmation='NAVIGATION')))
    task=Task(state,adapters=adapter,planner=Mock(side_effect=AssertionError('planner must not be called')),
              selector=Mock(side_effect=AssertionError('selector must not be called')))
    item=task.submit(SESSION,'리오야 https://example.org/docs 열어줘')
    task.run(item['id'])
    assert state.task(item['id'])['state']=='COMPLETED'
    adapter.call.assert_called_once()
    assert adapter.call.call_args.args[:2]==('browser.open_url',{'url':'https://example.org/docs'})
    assert len(state.calls(item['id']))==1


def test_known_site_goal_completes_with_single_navigation_no_llm_calls(state):
    adapters=SimpleNamespace(call=Mock(return_value=ToolResult(
        'OBSERVED','페이지를 열었어: ChatGPT\nhttps://chatgpt.com/',
        {'content_read':False,'url':'https://chatgpt.com/'},confirmation='NAVIGATION')))
    task=Task(state,adapters=adapters,
              planner=Mock(side_effect=AssertionError('planner should not be called')),
              selector=Mock(side_effect=AssertionError('selector should not be called')),
              responder=Mock(side_effect=AssertionError('LLM reply should not be called')))
    item=task.submit(SESSION,'리오야 GPT 하나 열어봐')
    task.run(item['id'])
    result=state.task(item['id'])
    assert result['state']=='COMPLETED' and result['steps']==1
    assert 'https://chatgpt.com/' in result['answer']
    adapters.call.assert_called_once()
    assert adapters.call.call_args.args[:2]==('browser.open_url',{'url':'https://chatgpt.com/'})


def test_model_cannot_invent_or_substitute_a_link(state):
    adapter=Mock()
    task=Task(state,adapters=adapter,
              selector=lambda _:{'kind':'tool','name':'browser.open_url','arguments':{'url':'https://example.org/invented'}},
              responder=lambda *_:'not executed')
    item=task.submit(SESSION,'사이트를 열어줘')
    task.run(item['id'])
    assert state.task(item['id'])['state']=='WAITING_INPUT'
    assert not state.calls(item['id'])
    adapter.call.assert_not_called()


def test_operator_bridge_denies_url_not_present_in_original_user_request(state):
    adapter=SimpleNamespace(call=Mock(return_value=ToolResult('OBSERVED','opened')))
    bridge=Bridge(state,catalog=Catalog(),adapters=adapter)
    request={'op':'call','session':SESSION,'name':'browser.open_url',
             'args':{'url':'https://example.org/elsewhere'},
             'revision':bridge.catalog.describe('browser.open_url')['revision'],
             'request_id':'direct_url_key_01','original_request':'다른 페이지 검색해 줘'}
    with pytest.raises(ValueError,match='EXPLICIT_COMMAND_REQUIRED'):
        bridge.call(request)
    adapter.call.assert_not_called()
    assert state.row('SELECT * FROM bridge_calls WHERE request_id=?',('direct_url_key_01',)) is None


def test_operator_bridge_accepts_same_url_with_matching_user_request(state):
    adapter=SimpleNamespace(call=Mock(return_value=ToolResult('OBSERVED','opened',
                                      {'content_read':False},confirmation='NAVIGATION')))
    bridge=Bridge(state,catalog=Catalog(),adapters=adapter)
    request={'op':'call','session':SESSION,'name':'browser.open_url',
             'args':{'url':'https://example.org/docs'},
             'revision':bridge.catalog.describe('browser.open_url')['revision'],
             'request_id':'direct_url_key_02','original_request':'리오야 https://example.org/docs 열어줘'}
    result=bridge.call(request)
    assert result['result']['status']=='OBSERVED'
    adapter.call.assert_called_once()
    assert bridge.status(SESSION,'direct_url_key_02')['result']['status']=='OBSERVED'
    adapter.call.assert_called_once()


def test_bridge_enforces_bound_homepage_for_known_site(state):
    adapter=SimpleNamespace(call=Mock(return_value=ToolResult('OBSERVED','opened')))
    bridge=Bridge(state,catalog=Catalog(),adapters=adapter)
    request={'op':'call','session':SESSION,'name':'browser.open_url',
             'args':{'url':'https://example.org/unrequested'},
             'revision':bridge.catalog.describe('browser.open_url')['revision'],
             'request_id':'direct_url_alias_01','original_request':'GPT 열어줘'}
    with pytest.raises(ValueError,match='EXPLICIT_COMMAND_REQUIRED'):
        bridge.call(request)
    adapter.call.assert_not_called()
