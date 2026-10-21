"""Small visible excerpts from the already allowlisted public vocabulary pages."""
from html.parser import HTMLParser
import re
from urllib.parse import urljoin,urlparse

import httpx
from web_search import SearchResult

DOMAINS=('careet.net','korean.go.kr','univ20.com')


def allowed(url):
    try:
        parsed=urlparse(url)
        host=(parsed.hostname or '').lower()
        return parsed.scheme=='https' and not parsed.username and parsed.port in (None,443) and any(host==d or host.endswith('.'+d) for d in DOMAINS)
    except ValueError:
        return False


class PublicText(HTMLParser):
    VOID=('br','img','meta','link','input','hr','wbr','source','area','base','embed','param','track')
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack=[];self.parts=[];self.published=None
    def handle_starttag(self,tag,attrs):
        a=dict(attrs)
        if tag=='meta' and a.get('property',a.get('name')) in ('article:published_time','datePublished','pubdate'):
            self.published=a.get('content')
        hidden=bool(self.stack and self.stack[-1][1]) or tag in ('script','style','noscript','svg','nav','footer','header','textarea') or 'hidden' in a or a.get('aria-hidden')=='true' or 'display:none' in a.get('style','').replace(' ','')
        if tag not in self.VOID:
            self.stack.append((tag,hidden))
    def handle_endtag(self,tag):
        if tag in self.VOID:return
        for i in range(len(self.stack)-1,-1,-1):
            if self.stack[i][0]==tag:
                del self.stack[i:];break
    def handle_startendtag(self,tag,attrs):
        self.handle_starttag(tag,attrs)
        if tag not in self.VOID:self.handle_endtag(tag)
    def handle_data(self,data):
        if not self.stack or not self.stack[-1][1]:
            text=' '.join(data.split())
            if text:self.parts.append(text)


def excerpt(html,term):
    parser=PublicText();parser.feed(html)
    text='\n'.join(parser.parts)
    # Use only the public preview; never include the gated continuation.
    for marker in ('캐릿 멤버십을 시작','여기서부터 진짜 필요한 내용','로그인 후 확인할 수'):
        text=text.split(marker,1)[0]
    candidates=[line for line in text.splitlines() if term in line and re.search(r'뜻|의미|가리키|말로|줄임말',line)]
    if not candidates:return None
    line=max(candidates,key=lambda s:sum(word in s for word in ('뜻','의미','가리키','말로','줄임말')))
    index=line.index(term)
    return ('[공개 페이지 발췌] '+line[max(0,index-60):index+360])[:440],parser.published


def enrich(results,term):
    enriched=[]
    with httpx.Client(timeout=5,follow_redirects=False,headers={'User-Agent':'Rio-public-vocabulary/1.0'}) as client:
        for index,result in enumerate(results):
            if index>=2 or not allowed(result.url):
                enriched.append(result);continue
            try:
                url=result.url
                for _ in range(3):
                    if not allowed(url):break
                    with client.stream('GET',url) as response:
                        if response.is_redirect:
                            url=urljoin(url,response.headers.get('location',''));continue
                        if response.status_code!=200 or 'html' not in response.headers.get('content-type',''):break
                        raw=bytearray()
                        for chunk in response.iter_bytes():
                            raw.extend(chunk)
                            if len(raw)>256000:break
                        found=excerpt(bytes(raw[:256000]).decode('utf-8',errors='replace'),term)
                        if found:
                            snippet,published=found
                            result=SearchResult(result.title,result.url,snippet,published or result.published_at)
                        break
            except (httpx.HTTPError,ValueError):
                pass
            enriched.append(result)
    return enriched
