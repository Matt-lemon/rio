"""Fixed-command client for Rio's dedicated public browser."""
import json
import os
import re
from pathlib import Path
import socket
import stat
import time
import unicodedata
from urllib.parse import urlparse


# Site identities, never article addresses or IDs. The operator's expected URL
# is not available to the resolver or model.
RESEARCH_SITES={'geeknews':'news.hada.io','긱뉴스':'news.hada.io'}


def topic_scope(query):
    match=re.match(r'^(GeekNews|긱뉴스)(?:에서|의)?\s+(.+)$',query,re.I)
    if not match:return query,None
    topic=re.sub(r'([A-Za-z0-9]+)(?:라는|이란|인)(?=\s)',r'\1',match.group(2))
    topic=re.sub(r'\s+(?:소개\s*)?(?:글|페이지|기사)$','',topic).strip()
    return topic,RESEARCH_SITES[match.group(1).lower()]


def selected_result(command):
    """Recognize only explicit search-result selection, never device commands."""
    text = command.strip().rstrip('.!。')
    text = re.sub(r'^리오(?:야)?[,，]?\s*', '', text)
    match = re.fullmatch(r'(?:검색\s*결과|결과)\s*([1-5])\s*번(?:\s*페이지)?(?:을|를)?\s*(?:열어\s*줘|열어주세요|열어|보기)', text)
    return int(match.group(1)) if match else None


def request(command):
    text=command.strip()
    if text.startswith('/browse '):text=text[8:].strip()
    if text in ('status','상태'):payload={'op':'status'}
    elif text=='proof':payload={'op':'proof'}
    elif text=='back':payload={'op':'back_results'}
    elif text.startswith('search '):payload={'op':'search','query':text[7:].strip()}
    elif text.startswith('open ') and text[5:].isdigit():payload={'op':'open_result','index':int(text[5:])}
    elif selected_result(text) is not None:payload={'op':'open_result','index':selected_result(text)}
    else:return {'ok':False,'status':'USE_STATUS_SEARCH_OR_OPEN'}
    try:
        from core.browser_transport import selected_mode,endpoint as transport_endpoint
        endpoint=transport_endpoint(selected_mode())
        info=endpoint.lstat()
        if not stat.S_ISSOCK(info.st_mode) or info.st_uid!=os.getuid() or stat.S_IMODE(info.st_mode)!=0o600:
            return {'ok':False,'status':'PRIVATE_BROWSER_SOCKET_REQUIRED'}
        with socket.socket(socket.AF_UNIX,socket.SOCK_STREAM) as connection:
            connection.settimeout(40);connection.connect(str(endpoint))
            connection.sendall((json.dumps(payload,ensure_ascii=False)+'\n').encode())
            raw=b''
            while b'\n' not in raw and len(raw)<=65536:
                part=connection.recv(4096)
                if not part:break
                raw+=part
            if len(raw)>65536:return {'ok':False,'status':'BROWSER_RESPONSE_TOO_LARGE'}
            return json.loads(raw)
    except (OSError,ValueError):return {'ok':False,'status':'BROWSER_CONNECTION_UNVERIFIED'}


def _line(value):
    return ' '.join(str(value).split())


def format_reply(result):
    status = result.get('status')
    if str(status).startswith('SEARCH_TAB_REQUIRED'):
        return '선택된 탭이 검색 결과 화면인지 확인해 줘. 다른 페이지에 있다면 뒤로 가기로 마지막 검색 결과에 돌아온 뒤 번호를 골라 줘.'
    if status in ('SEARCH_RESULTS_READY', 'SEARCH_RESULTS_RETURNED') and result.get('ok'):
        lines = [f'“{_line(result.get("query", ""))}” 검색 결과야.']
        for row in result.get('results', []):
            lines.append(f'{row["index"]}번. {_line(row["title"])}\n    {_line(row["url"])}')
        lines.append('“첫 번째 거 열어줘”처럼 골라 줘. “검색 결과 1번 열어줘”나 /browse open 1도 가능해.')
        return '\n'.join(lines)
    if status == 'RESULT_PAGE_OPENED' and result.get('ok'):
        return f'페이지를 열었어: {_line(result.get("title", ""))}\n{_line(result.get("url", ""))}'
    messages = {
        'NO_CURRENT_SEARCH_RESULT': '열 수 있는 검색 결과가 없어. 먼저 /browse search 검색어로 검색해 줘.',
        'NO_VERIFIED_RESULTS': '열 수 있는 검색 결과를 확인하지 못했어. 검색어를 바꿔서 다시 검색해 줘.',
        'CONNECTED_WAITING_FOR_QUERY': '리오 전용 브라우저가 연결돼 있어. 검색어를 기다리는 중이야.',
        'CONNECTED_RESEARCH_PAGE': '리오 전용 브라우저에서 검색한 페이지를 보고 있어.',
        'SEARCH_CONTEXT_CHANGED': '탭이 다른 페이지로 바뀌어서 이전 결과를 열지 않았어. 검색 결과 화면으로 돌아오거나 다시 검색해 줘.',
        'AWAITING_TAB_APPROVAL': '리오 전용 검색 탭의 연결 승인을 기다리고 있어.',
        'STARTING_DEDICATED_BROWSER': '리오 전용 브라우저 연결을 준비하고 있어.',
        'CONNECTION_PROOF_SAVED': '브라우저 확인 화면을 저장했어.',
        'BROWSER_CONNECTION_UNVERIFIED': '브라우저 연결을 확인하지 못했어. 페이지가 열렸는지도 미확인이야.',
        'REQUESTED_SITE_MISMATCH': '요청한 사이트와 실제 도착한 사이트가 달라서 정확한 페이지를 찾았다고 보고하지 않을게.',
        'USE_STATUS_SEARCH_OR_OPEN': '검색은 /browse search 검색어, 페이지 열기는 “검색 결과 1번 열어줘”로 입력해 줘.',
    }
    return messages.get(status, f'브라우저 작업을 완료했는지 확인하지 못했어. 상태: {_line(status)}')


def handle(command):
    text=command.removeprefix('/browse ').strip()
    if not (text in ('status','상태','proof','back') or text.startswith(('search ','open ')) or selected_result(text) is not None):
        reply=BrowserConversation().handle(text)
        if reply is not None:return reply
    return format_reply(request(command))


def handle_selection(command):
    if selected_result(command) is None:
        return None
    return handle(command)


class BrowserConversation:
    """Per-chat, short-lived follow-up context. No model-generated browser actions."""
    def __init__(self):
        self.clear()

    def clear(self):
        self.results=[];self.index=None;self.checked=0;self.required_host=None

    def _run(self, command):
        return format_reply(self.run_result(command))

    def run_result(self, command):
        """Same verified browser operation, with its structured result retained."""
        result=request(command)
        operation=command.removeprefix('/browse ').strip().split()[0]
        self.last_operation={'open':'open_result','back':'back_results'}.get(operation,operation)
        status=result.get('status')
        if result.get('ok') and status in ('SEARCH_RESULTS_READY','SEARCH_RESULTS_RETURNED'):
            if operation=='search':
                scoped=re.match(r'search site:([a-z0-9.-]+)\s',command,re.I)
                self.required_host=scoped.group(1).lower() if scoped else None
            self.results=result.get('results',[]);self.index=None;self.checked=time.monotonic()
        elif result.get('ok') and status=='RESULT_PAGE_OPENED':
            if self.required_host and urlparse(result.get('url','')).hostname!=self.required_host:
                return {'ok':False,'status':'REQUESTED_SITE_MISMATCH','expected_host':self.required_host,
                        'actual_host':urlparse(result.get('url','')).hostname}
            if self.required_host:result={**result,'requested_host':self.required_host,'site_verified':True}
            self.index=result.get('index');self.checked=time.monotonic()
        elif not result.get('ok'):
            self.clear()
        return result

    def open_topic_result(self, query):
        topic,host=topic_scope(query)
        result = self.run_result('search ' + (('site:'+host+' ') if host else '') + topic)
        if not self.results:
            return result
        tokens=[t for t in re.findall(r'[\w]+',unicodedata.normalize('NFKC',topic).lower())
                if t not in {'뉴스','기사','관련','페이지','사이트','대한','관한'}]
        matches=[r for r in self.results if tokens and all(t in unicodedata.normalize('NFKC',r['title']).lower() for t in tokens)]
        if matches and ('뉴스' in query or '기사' in query or len(matches)==1):
            opened=self.run_result('open ' + str(matches[0]['index']))
            if opened.get('ok') and host and urlparse(opened.get('url','')).hostname!=host:
                return {'ok':False,'status':'REQUESTED_SITE_MISMATCH','expected_host':host,'actual_host':urlparse(opened.get('url','')).hostname}
            if opened.get('ok') and host:opened['requested_host']=host;opened['site_verified']=True
            return opened
        return {**result, 'needs_selection': True}

    def open_topic(self, query):
        """One sentence may request any relevant article, not an unspecified exact site."""
        result=self.open_topic_result(query)
        reply=format_reply(result)
        return reply+'\n요청한 페이지를 하나로 확정하지 못했어. 제목이나 번호를 골라 줘.' if result.get('needs_selection') else reply

    def handle(self, text):
        text=text.strip()
        if text.startswith('/browse '):return self._run(text)
        clean=re.sub(r'^리오(?:야)?[,，]?\s*','',text).rstrip('.!。')
        scoped_open=re.fullmatch(r'((?:GeekNews|긱뉴스)(?:에서|의)?\s+.+?)\s*(?:소개\s*)?(?:글|페이지|기사)(?:를|을)?\s*(?:찾아서\s*)?열어\s*(?:줘|주세요)',clean,re.I)
        if scoped_open:
            query=scoped_open.group(1).strip()
            if re.search(r'비밀번호|인증번호|토큰|password|access.?token|\b\d{6,}\b',query,re.I):
                return '인증 정보는 검색하지 않을게. 공개 자료의 검색어만 알려줘.'
            self.clear();return self.open_topic(query)
        direct=re.fullmatch(r'(.+?)\s*(?:페이지|기사)(?:를|을)?\s*열어\s*(?:줘|주세요)',clean)
        if direct and clean not in ('그 페이지 열어줘','검색 결과 페이지 열어줘') and not re.match(r'(?:검색\s*결과|결과|[1-5]\s*번|첫\s*번째|두\s*번째|세\s*번째|네\s*번째|다섯\s*번째)',clean):
            query=direct.group(1).strip(' “\"”\'')
            if clean.endswith(('기사를 열어줘','기사 열어줘')):query+=' 기사'
            if re.search(r'비밀번호|인증번호|토큰|password|access.?token|\b\d{6,}\b',query,re.I):
                return '인증 정보는 검색하지 않을게. 공개 자료의 검색어만 알려줘.'
            self.clear()
            return self.open_topic(query)
        search=re.fullmatch(r'(?:(?:브라우저|구글|인터넷)(?:에서|로)\s*)?(.+?)\s*(?:검색해\s*줘|검색해\s*주세요|검색해서\s*보여\s*줘|찾아서\s*보여\s*줘)',clean)
        if not search:
            search=re.fullmatch(r'(?:브라우저|구글|인터넷)(?:에서|로)\s*(.+?)\s*찾아\s*줘',clean)
        if search:
            query=search.group(1).strip(' “”"\'')
            if re.search(r'비밀번호|인증번호|토큰|password|access.?token|\b\d{6,}\b',query,re.I):
                return '인증 정보는 검색하지 않을게. 공개 자료의 검색어만 알려줘.'
            self.clear()
            return self._run('search '+query)
        explicit=selected_result(text)
        if explicit is not None:return self._run('open '+str(explicit))
        if not self.results or time.monotonic()-self.checked>600:
            self.clear()
            return None
        if clean in ('검색 결과로 돌아가줘','검색결과로 돌아가줘','검색 결과로 돌아가','결과 목록 보여줘'):
            return self._run('back')
        if clean in ('다음 거 열어줘','다음 결과 열어줘','다른 거 열어줘'):
            if self.index is None:return '어느 결과 다음인지 알려줘. 예를 들어 “첫 번째 거 열어줘”라고 해 줘.'
            index=self.index+1
        else:
            match=re.fullmatch(r'([1-5]\s*번|첫\s*번째|두\s*번째|세\s*번째|네\s*번째|다섯\s*번째)(?:\s*(?:거|것|페이지))?(?:을|를)?\s*(?:열어\s*줘|열어\s*주세요|열어)',clean)
            if match:
                number=re.sub(r'\s','',match.group(1))
                index=int(number[0]) if number[0].isdigit() else {'첫번째':1,'두번째':2,'세번째':3,'네번째':4,'다섯번째':5}[number]
            elif clean in ('그거 열어줘','그 페이지 열어줘','이거 열어줘'):
                if len(self.results)!=1:return '어느 결과를 열까? “첫 번째 거 열어줘”처럼 번호를 알려줘.'
                index=1
            else:
                # Exact result titles can identify a unique page; never fuzzy-pick one.
                title=re.fullmatch(r'(.+?)(?:을|를)?\s+열어\s*줘',clean)
                if not title:return None
                matches=[r['index'] for r in self.results if _line(r['title'])==_line(title.group(1).strip('“”"\''))]
                if len(matches)!=1:return None
                index=matches[0]
        if index>len(self.results):return '그 번호의 검색 결과가 없어. 표시된 번호 중에서 골라 줘.'
        return self._run('open '+str(index))
