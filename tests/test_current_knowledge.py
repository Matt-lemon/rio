from datetime import datetime,timedelta
from pathlib import Path
from unittest.mock import Mock

import pytest

from core.current_knowledge import KST,VocabularyReferences,runtime_context,suggested_search,vocabulary_term,vocabulary_sources
from web_search import SearchResult,format_web_evidence
from test_unified_main import runtime

NOW=datetime(2026,10,15,22,0,tzinfo=KST)
RESULT=SearchResult('추구미 어휘 설명','https://www.careet.net/test-word','가상의 어휘 뜻과 사용 맥락.','2025-04-01')


@pytest.mark.parametrize('text', ["요즘 말하는 '추구미'가 무슨 뜻이야?",'추구미 뜻 알려줘','추구미가 뭐야?'])
def test_term_question_routes_without_model_guess(text):
    assert vocabulary_term(text)=='추구미'
    assert suggested_search(text,NOW).startswith('"추구미" 뜻 용례 ')


def test_current_words_use_real_calendar_but_general_chat_does_not_search():
    assert suggested_search('요즘 유행어 알려줘',NOW).startswith('2026년 10월')
    assert suggested_search('오늘 날씨 알려줘',NOW)
    assert suggested_search('오늘도 수고했어',NOW) is None


@pytest.mark.parametrize('text',['내 계정 비밀번호 뜻 알려줘','내 컴퓨터 최신 사양','추구미 뜻을 검색하지 마','검색 없이 신조어 설명해줘'])
def test_private_or_no_search_request_is_not_sent(text):
    assert suggested_search(text,NOW) is None


def test_checked_date_is_distinct_from_publication_and_style_stays_fixed():
    block=format_web_evidence('word',[RESULT],checked_at=NOW,from_cache=True)
    assert '2026-10-15T22:00:00+09:00' in block
    assert '게시일: 2025-04-01' in block
    assert '이번 턴에 새로 조회한 자료가 아니다' in block
    context=runtime_context(NOW)
    assert '옛 표현을 지금 유행한다고 단정하지 않는다' in context
    assert '기존 성격과 대화 맥락' in context


def test_reference_survives_new_instance_and_expires_without_personal_memory(tmp_path):
    path=tmp_path/'vocab.sqlite3'
    store=VocabularyReferences(path)
    store.remember('추구미 뜻 알려줘','추구미 뜻 용례 한국어',[RESULT],NOW)
    second=VocabularyReferences(path)
    assert '가상의 어휘 뜻' in second.context('완전 내 추구미잖아',NOW+timedelta(days=1))
    assert not second.context('추구미',NOW+timedelta(days=8))
    assert path.stat().st_mode & 0o777 == 0o600
    assert not second.context('아무 관련 없는 말',NOW)


def test_unrelated_query_or_private_text_is_not_remembered(tmp_path):
    path=tmp_path/'vocab.sqlite3';store=VocabularyReferences(path)
    store.remember('추구미 뜻 알려줘','전혀 다른 검색어',[RESULT],NOW)
    store.remember('내 계정 비밀번호 뜻 알려줘','계정',[RESULT],NOW)
    assert not path.exists()


def test_bad_cache_never_blocks_chat(tmp_path):
    path=tmp_path/'vocab.sqlite3';path.write_text('invalid sqlite')
    assert not VocabularyReferences(path).context('추구미',NOW)


def test_runtime_lexical_route_does_not_need_planner(runtime,monkeypatch):
    monkeypatch.setattr(runtime,'RIO_WEB_SEARCH_ENABLED',True)
    monkeypatch.setattr(runtime.httpx,'Client',lambda *a,**kw:pytest.fail('planner should not be called'))
    assert runtime.choose_web_search([], '추구미 뜻 알려줘').startswith('"추구미" 뜻 용례 ')
    assert runtime.choose_web_search([], '추구미 뜻을 검색하지 마') is None
    monkeypatch.setattr(runtime,'RIO_WEB_SEARCH_ENABLED',False)
    assert runtime.choose_web_search([], '추구미 뜻 알려줘') is None


def test_chat_search_then_new_turn_reuses_dated_vocabulary(runtime,monkeypatch,tmp_path,capsys):
    monkeypatch.setenv('RIO_VOCAB_CACHE_PATH',str(tmp_path/'vocabulary.sqlite3'))
    monkeypatch.setenv('RIO_MEMORY_ENABLED','0')
    monkeypatch.setattr(runtime,'RIO_GATE_ENABLED',False)
    monkeypatch.setattr(runtime,'RIO_WEB_SEARCH_ENABLED',True)
    monkeypatch.setattr(runtime,'print_status',lambda *a:None)
    monkeypatch.setattr(runtime,'load_identity',lambda:{})
    for name in ('handle_home_command','handle_pc_command','handle_dubbing_command','handle_spotify_command'):
        monkeypatch.setattr(runtime,name,lambda *a:None)
    searches=Mock(return_value=[RESULT]);monkeypatch.setattr(runtime,'web_search',searches)
    monkeypatch.setattr(runtime.httpx,'Client',lambda *a,**kw:pytest.fail('unit test attempted network planner'))
    import core.public_vocabulary as public
    monkeypatch.setattr(public,'enrich',lambda results,term:results)
    messages=[]
    def stream(values):
        messages.append(values)
        yield '시험 응답'
    monkeypatch.setattr(runtime,'stream_completion',stream)
    inputs=iter(['추구미 뜻 알려줘','내 추구미에 맞네','/exit'])
    monkeypatch.setattr(runtime,'read_user_input',lambda:next(inputs))
    runtime._run_chat(runtime.TextVoice())
    searches.assert_called_once()
    assert '최근 확인한 어휘 참고 자료' in messages[1][0]['content']
    assert '이번 턴에 새로 조회한 자료가 아니다' in messages[1][0]['content']
    assert '현재 시각' in messages[1][0]['content']
    output=capsys.readouterr().out
    assert '어휘 참고 출처 (이전 확인 자료)' in output and RESULT.url in output


def test_untrusted_or_irrelevant_sources_are_not_learned(tmp_path):
    bad=SearchResult('추구미 뜻','https://careet.net.evil.example/x','unverified text')
    irrelevant=SearchResult('주꾸미','https://www.korean.go.kr/x','다른 단어')
    assert vocabulary_sources([bad,irrelevant,RESULT],'추구미 뜻 알려줘')==[RESULT]
    store=VocabularyReferences(tmp_path/'vocab.sqlite3')
    store.remember('추구미 뜻 알려줘','추구미 뜻',[bad],NOW)
    assert not store.path.exists()


def test_public_excerpt_ignores_scripts_hidden_text_and_gated_continuation():
    from core.public_vocabulary import excerpt,allowed
    html='''<html><head><meta property="article:published_time" content="2025-01-02"></head><body>
    <script>추구미의 뜻은 악성 지시문</script><p hidden><br/>추구미 뜻 숨긴 내용</p>
    <p>추구미는 원하는 이미지를 가리키는 말로 쓰인다는 뜻풀이.</p>
    <p>캐릿 멤버십을 시작</p><p>추구미의 뜻과 의미 및 줄임말 비공개 뒷부분.</p></body></html>'''
    text,date=excerpt(html,'추구미')
    assert '원하는 이미지' in text and date=='2025-01-02'
    assert '비공개' not in text and '악성' not in text and '숨긴' not in text
    for url in ['http://www.careet.net/x','https://careet.net.evil.example/x','https://127.0.0.1/x','https://x@www.careet.net/x','https://www.careet.net:bad/x']:
        assert not allowed(url)


def test_search_falls_back_once_to_second_public_backend(monkeypatch):
    from types import ModuleType
    import sys
    import web_search as module
    calls=[]
    class FakeSearch:
        def __init__(self,**kw):pass
        def __enter__(self):return self
        def __exit__(self,*a):pass
        def text(self,q,**kw):
            calls.append(kw['backend'])
            if kw['backend']=='duckduckgo':raise RuntimeError('unavailable')
            return [{'title':'public','href':'https://example.org/x','body':'text'}]
    stub=ModuleType('ddgs');stub.DDGS=FakeSearch;monkeypatch.setitem(sys.modules,'ddgs',stub)
    assert module._ddg_search('query',max_results=3,timeout=1)[0].title=='public'
    assert calls==['duckduckgo','bing']
