import importlib.util
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from rio_browser_research.service import validate,public_url,ResearchError,parse_result,Browser


@pytest.mark.parametrize('payload',[{'op':'set_light','target':'computer','state':'off'},{'op':'status','script':'code'}, {'op':'search','query':'https://example.org'}, {'op':'search','query':'인증번호 123456'}, {'op':'open_result','index':True}, {'op':'open_result','index':6}])
def test_only_scoped_public_research_commands(payload):
    with pytest.raises(ResearchError):validate(payload)


@pytest.mark.parametrize('url',['http://example.org','https://localhost/x','https://127.0.0.1/x','https://192.168.50.1/x','https://home.google.com/','https://accounts.google.com/','https://my.smartthings.com/','https://example.org/logout','https://x@example.org/','https://example.org:bad/x'])
def test_private_and_sensitive_pages_are_blocked(url):assert not public_url(url)


def test_normal_read_only_public_page_is_allowed():
    assert public_url('https://www.korean.go.kr/front/main.do')
    for request in ({'op':'status'},{'op':'proof'},{'op':'search','query':'추구미'},{'op':'open_result','index':1}):validate(request)


def test_parser_does_not_return_private_mcp_metadata():
    response=SimpleNamespace(is_error=False,content=[SimpleNamespace(type='text',text='### Result\n{"rio_research":{"title":"Google"}}\n### Page\nprivate metadata')])
    assert parse_result(response)=={'title':'Google'}
    response.is_error=True
    with pytest.raises(ResearchError):parse_result(response)


def test_no_result_can_be_opened_before_search():
    import asyncio
    browser=Browser(None)
    with pytest.raises(ResearchError,match='NO_CURRENT_SEARCH_RESULT'):
        asyncio.run(browser.handle({'op':'open_result','index':1}))


def test_open_can_switch_only_between_stored_results_and_confirmed_page():
    import asyncio
    from unittest.mock import AsyncMock
    browser=Browser(None)
    browser.search_url='https://www.google.com/search?q=example'
    browser.results=[{'index':1,'title':'A','url':'https://example.org/a'},
                     {'index':2,'title':'B','url':'https://example.org/b'}]
    browser.code=AsyncMock(side_effect=[{'url':'https://example.org/a','title':'A'},
                                       {'url':'https://example.org/b','title':'B'}])
    assert asyncio.run(browser.handle({'op':'open_result','index':1}))['index']==1
    assert asyncio.run(browser.handle({'op':'open_result','index':2}))['index']==2
    second_code=browser.code.call_args_list[1].args[0]
    assert 'https://example.org/a' in second_code
    assert 'includes(page.url())' in second_code
    assert browser.opened_url=='https://example.org/b'


def test_return_recollects_current_results_and_refuses_untracked_navigation():
    import asyncio
    from unittest.mock import AsyncMock
    browser=Browser(None)
    browser.search_url='https://www.google.com/search?q=example'
    browser.query='example';browser.opened_url='https://example.org/a'
    browser.results=[{'index':1,'title':'old','url':'https://example.org/a'}]
    browser.code=AsyncMock(return_value={'url':browser.search_url,
        'rows':[{'title':'new','url':'https://example.org/new'}]})
    reply=asyncio.run(browser.handle({'op':'back_results'}))
    assert reply['results'][0]['title']=='new'
    assert 'includes(page.url())' in browser.code.call_args.args[0]
    browser.code=AsyncMock(side_effect=ResearchError('SEARCH_CONTEXT_CHANGED'))
    with pytest.raises(ResearchError,match='SEARCH_CONTEXT_CHANGED'):
        asyncio.run(browser.handle({'op':'open_result','index':1}))


def test_only_exact_confirmed_public_page_is_added_to_scope():
    browser=Browser(None)
    browser.opened_url='https://example.org/a'
    scope=browser.read_scope()
    assert 'page.url()==="https://example.org/a"' in scope
    assert 'other_host' in scope


def test_failed_new_search_does_not_leave_previous_results_openable():
    import asyncio
    from unittest.mock import AsyncMock
    browser=Browser(None)
    browser.results=[{'index':1,'title':'old','url':'https://example.org/old'}]
    browser.search_url='https://www.google.com/search?q=old'
    browser.code=AsyncMock(side_effect=[{'url':browser.search_url,'origin':'https://www.google.com'},
                                       ResearchError('SEARCH_TAB_REQUIRED')])
    with pytest.raises(ResearchError):
        asyncio.run(browser.handle({'op':'search','query':'new'}))
    with pytest.raises(ResearchError,match='NO_CURRENT_SEARCH_RESULT'):
        asyncio.run(browser.handle({'op':'open_result','index':1}))
