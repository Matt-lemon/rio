from unittest.mock import Mock

import pytest

from core import research_browser as browser


@pytest.mark.parametrize('text', [
    '검색 결과 1번 열어줘', '검색결과 1번을 열어 줘',
    '리오, 검색 결과 1번 페이지 열어주세요', '결과 1번 보기',
])
def test_explicit_selection_recognized(text):
    assert browser.selected_result(text) == 1


@pytest.mark.parametrize('text', [
    '컴퓨터 꺼줘', '1번 켜줘', '1번 열어줘', '검색 결과 6번 열어줘',
    '검색 결과 1번 열어줘 그리고 컴퓨터 꺼줘', '검색 결과 1번 열어줘라는 말의 뜻',
])
def test_unrelated_or_ambiguous_commands_do_not_contact_browser(monkeypatch, text):
    request = Mock(side_effect=AssertionError('unexpected browser request'))
    monkeypatch.setattr(browser, 'request', request)
    assert browser.handle_selection(text) is None
    request.assert_not_called()


def test_numbered_results_include_title_url_and_exact_followup():
    reply = browser.format_reply({'ok': True, 'status': 'SEARCH_RESULTS_READY',
        'query': '예시', 'results': [{'index': 1, 'title': '실제 문서 제목',
                                   'url': 'https://example.org/docs'}]})
    assert '1번. 실제 문서 제목' in reply
    assert 'https://example.org/docs' in reply
    assert '검색 결과 1번 열어줘' in reply


def test_open_failure_does_not_claim_page_opened(monkeypatch):
    monkeypatch.setattr(browser, 'request', lambda _: {'ok': False, 'status': 'NO_CURRENT_SEARCH_RESULT'})
    assert '먼저 /browse search' in browser.handle_selection('검색 결과 1번 열어줘')


def test_open_success_uses_actual_page_evidence(monkeypatch):
    monkeypatch.setattr(browser, 'request', lambda _: {'ok': True,
        'status': 'RESULT_PAGE_OPENED', 'title': '도착한 문서', 'url': 'https://example.org/final'})
    reply = browser.handle_selection('검색 결과 1번 열어줘')
    assert '도착한 문서' in reply and 'https://example.org/final' in reply


def test_changed_page_explains_how_to_return_without_claiming_success():
    reply = browser.format_reply({'ok': False, 'status': 'SEARCH_TAB_REQUIRED_OTHER_HOST'})
    assert '뒤로 가기' in reply and '마지막 검색 결과' in reply
    assert '페이지를 열었어' not in reply


def test_natural_search_open_next_and_return_use_confirmed_context(monkeypatch):
    rows=[{'index': 1, 'title': '공식 문서', 'url': 'https://example.org/a'},
          {'index': 2, 'title': '다른 문서', 'url': 'https://example.org/b'}]
    replies=iter([
        {'ok': True, 'status': 'SEARCH_RESULTS_READY', 'query': '예시', 'results': rows},
        {'ok': True, 'status': 'RESULT_PAGE_OPENED', 'index': 1, 'title': '공식 문서', 'url': rows[0]['url']},
        {'ok': True, 'status': 'RESULT_PAGE_OPENED', 'index': 2, 'title': '다른 문서', 'url': rows[1]['url']},
        {'ok': True, 'status': 'SEARCH_RESULTS_RETURNED', 'query': '예시', 'results': rows},
    ])
    request=Mock(side_effect=lambda _: next(replies))
    monkeypatch.setattr(browser, 'request', request)
    chat=browser.BrowserConversation()
    assert '검색 결과야' in chat.handle('리오, 구글에서 예시 찾아줘')
    assert '공식 문서' in chat.handle('첫 번째 거 열어줘')
    assert '다른 문서' in chat.handle('다음 거 열어줘')
    assert '검색 결과야' in chat.handle('검색 결과로 돌아가줘')
    assert [c.args[0] for c in request.call_args_list]==['search 예시','open 1','open 2','back']


def test_ambiguous_reference_asks_without_opening_and_context_is_per_chat(monkeypatch):
    request=Mock()
    monkeypatch.setattr(browser, 'request', request)
    chat=browser.BrowserConversation()
    chat.results=[{'index': 1}, {'index': 2}];chat.checked=browser.time.monotonic()
    assert '어느 결과' in chat.handle('그거 열어줘')
    assert '어느 결과 다음' in chat.handle('다음 거 열어줘')
    assert browser.BrowserConversation().handle('첫 번째 거 열어줘') is None
    chat.checked-=601
    assert chat.handle('첫 번째 거 열어줘') is None
    request.assert_not_called()


def test_failed_open_drops_short_followup_context(monkeypatch):
    request=Mock(return_value={'ok':False,'status':'SEARCH_CONTEXT_CHANGED'})
    monkeypatch.setattr(browser, 'request', request)
    chat=browser.BrowserConversation()
    chat.results=[{'index':1}];chat.checked=browser.time.monotonic()
    assert '열지 않았어' in chat.handle('첫 번째 거 열어줘')
    assert chat.handle('다음 거 열어줘') is None
    request.assert_called_once_with('open 1')


def test_sensitive_search_not_forwarded_and_controls_remain_unrelated(monkeypatch):
    request=Mock()
    monkeypatch.setattr(browser, 'request', request)
    chat=browser.BrowserConversation()
    assert '검색하지 않을게' in chat.handle('인증번호 123456 검색해줘')
    assert chat.handle('거실불 켜줘') is None
    assert chat.handle('컴퓨터 꺼줘') is None
    assert chat.handle('음악 멈춰줘') is None
    request.assert_not_called()


def test_one_sentence_topic_news_searches_and_opens_matching_result(monkeypatch):
    rows=[{'index':1,'title':'다른 에픽 소식','url':'https://example.org/unrelated'},
          {'index':2,'title':'AMD 에픽 베니스 새 소식','url':'https://example.org/article'}]
    request=Mock(side_effect=[
        {'ok':True,'status':'SEARCH_RESULTS_READY','query':'에픽 베니스 뉴스','results':rows},
        {'ok':True,'status':'RESULT_PAGE_OPENED','index':2,'title':rows[1]['title'],'url':rows[1]['url']},
    ])
    monkeypatch.setattr(browser,'request',request)
    reply=browser.handle('에픽 베니스 뉴스 페이지 열어줘')
    assert '페이지를 열었어' in reply
    assert [c.args[0] for c in request.call_args_list]==['search 에픽 베니스 뉴스','open 2']


def test_one_sentence_exact_page_does_not_choose_between_multiple_matches(monkeypatch):
    rows=[{'index':1,'title':'동일 이름 문서 A','url':'https://example.org/a'},
          {'index':2,'title':'동일 이름 문서 B','url':'https://example.org/b'}]
    request=Mock(return_value={'ok':True,'status':'SEARCH_RESULTS_READY','query':'동일 이름','results':rows})
    monkeypatch.setattr(browser,'request',request)
    reply=browser.handle('동일 이름 페이지 열어줘')
    assert '하나로 확정하지 못했어' in reply
    request.assert_called_once_with('search 동일 이름')


def test_named_site_and_topic_use_search_without_an_article_address(monkeypatch):
    calls=[]
    def reply(command):
        calls.append(command)
        if command.startswith('search '):
            return {'ok':True,'status':'SEARCH_RESULTS_READY','query':'site:news.hada.io Example 오픈소스 Canva',
                    'results':[{'index':1,'title':'Example - 오픈소스 Canva','url':'https://www.google.com/goto?url=public'}]}
        return {'ok':True,'status':'RESULT_PAGE_OPENED','index':1,'title':'Example - 오픈소스 Canva','url':'https://news.hada.io/topic?id=123'}
    monkeypatch.setattr(browser,'request',reply)
    chat=browser.BrowserConversation()
    response=chat.handle('긱뉴스에서 Example라는 오픈소스 Canva 소개 글을 찾아서 열어줘')
    assert calls==['search site:news.hada.io Example 오픈소스 Canva','open 1']
    assert '페이지를 열었어' in response and chat.required_host=='news.hada.io'


def test_same_topic_on_another_site_is_never_reported_as_the_requested_page(monkeypatch):
    replies=iter([
        {'ok':True,'status':'SEARCH_RESULTS_READY','results':[{'index':1,'title':'Example - 오픈소스 Canva','url':'https://www.google.com/goto?url=public'}]},
        {'ok':True,'status':'RESULT_PAGE_OPENED','index':1,'title':'Example - Open Source Canva','url':'https://news.ycombinator.com/item?id=123'}])
    monkeypatch.setattr(browser,'request',lambda _:next(replies))
    result=browser.BrowserConversation().open_topic_result('GeekNews Example 오픈소스 Canva')
    assert result['status']=='REQUESTED_SITE_MISMATCH' and result['ok'] is False
    assert '페이지를 열었어' not in browser.format_reply(result)
