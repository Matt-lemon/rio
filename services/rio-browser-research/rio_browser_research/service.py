from __future__ import annotations

import asyncio
import ipaddress
import json
import os
from pathlib import Path
import re
import socket
import signal
import struct
from urllib.parse import quote,urlparse
from core.browser_transport import mode_name,profile,profile_label,endpoint as transport_endpoint,private_directory,mcp_arguments,cdp_identity,profile_in_use

ROOT=Path.home()/'.local/state/rio/browser-research'
PROFILE=Path.home()/'.local/share/rio-browser/edge-profile'
ENGINES={'www.google.com','google.com','www.google.co.kr','google.co.kr','www.bing.com','bing.com'}


class ResearchError(Exception):pass


def public_url(value):
    try:
        p=urlparse(value);host=(p.hostname or '').lower()
        if p.scheme!='https' or p.username or p.password or p.port not in (None,443):return False
        if '.' not in host or host.endswith(('.local','.localhost','.internal')):return False
        if host in ('home.google.com','accounts.google.com','my.smartthings.com','account.samsung.com'):return False
        if re.search(r'/(?:login|signin|logout|checkout|payment|accounts)(?:/|$)',p.path,re.I):return False
        try:return ipaddress.ip_address(host).is_global
        except ValueError:return True
    except (ValueError,TypeError):return False


def validate(req):
    if not isinstance(req,dict):raise ResearchError('INVALID_REQUEST')
    if req in ({'op':'status'}, {'op':'proof'}, {'op':'back_results'}):return
    if set(req)=={'op','query'} and req['op']=='search':
        q=req['query']
        if not isinstance(q,str) or not 1<=len(q.strip())<=160 or re.search(r'[\x00-\x1f]|https?://|[\\/]|비밀번호|인증번호|access.?token|sk-[A-Za-z0-9]|\b\d{6,}\b',q,re.I):
            raise ResearchError('PUBLIC_SEARCH_TERM_REQUIRED')
        return
    if set(req)=={'op','index'} and req['op']=='open_result' and type(req['index']) is int and 1<=req['index']<=5:return
    raise ResearchError('OPERATION_NOT_ALLOWED')


def parse_result(result):
    if result.is_error:
        # Only fixed diagnostic categories cross IPC; never return browser stderr.
        text='\n'.join(getattr(x,'text','') for x in result.content if getattr(x,'type',None)=='text')
        categories=(('Missing X server','DESKTOP_DISPLAY_UNAVAILABLE'),
                    ('Authorization required','DESKTOP_AUTHORIZATION_MISSING'),
                    ('cannot open display','DESKTOP_DISPLAY_UNAVAILABLE'),
                    ('sandbox','BROWSER_SANDBOX_START_FAILED'),
                    ('Executable doesn\'t exist','BROWSER_EXECUTABLE_MISSING'),
                    ('Unknown tool','MCP_TOOL_UNAVAILABLE'),
                    ('not found','MCP_RESOURCE_NOT_FOUND'),
                    ('Target page, context or browser has been closed','BROWSER_CLOSED'),
                    ('Timeout','BROWSER_OPERATION_TIMEOUT'),
                    ('ERR_','BROWSER_NETWORK_ERROR'))
        kind=next((code for marker,code in categories if marker.lower() in text.lower()),'OFFICIAL_BROWSER_CONNECTION_NOT_READY')
        print(json.dumps({'status':kind,'error_lines':len(text.splitlines())}),flush=True)
        raise ResearchError(kind)
    for item in result.content:
        if getattr(item,'type',None)!='text':continue
        m=re.search(r'^### Result\n(.*?)(?=^### |\Z)',item.text,re.M|re.S)
        if not m:continue
        try:d=json.loads(m.group(1).strip())
        except ValueError:continue
        if isinstance(d,dict) and set(d)=={'rio_research'} and isinstance(d['rio_research'],dict):
            if d['rio_research'].get('error'):
                if d['rio_research']['error']=='SEARCH_CHANGED':raise ResearchError('SEARCH_CONTEXT_CHANGED')
                kind=d['rio_research'].get('kind')
                raise ResearchError('SEARCH_TAB_REQUIRED_'+kind.upper() if kind in ('google_other','bing_other','other_host','internal_or_blank') else 'SEARCH_TAB_REQUIRED')
            return d['rio_research']
    raise ResearchError('BROWSER_RESULT_UNVERIFIED')


READ=r"""
const u=new URL(page.url());
const engines=['www.google.com','google.com','www.google.co.kr','google.co.kr','www.bing.com','bing.com'];
if(u.protocol!=='https:')return {rio_research:{error:'SEARCH_TAB_REQUIRED',kind:'internal_or_blank'}};
if(!engines.includes(u.hostname))return {rio_research:{error:'SEARCH_TAB_REQUIRED',kind:'other_host'}};
if(!['/','/webhp','/search'].includes(u.pathname))
  return {rio_research:{error:'SEARCH_TAB_REQUIRED',kind:u.hostname.includes('google')?'google_other':'bing_other'}};
return {rio_research:{url:page.url(),origin:u.origin,path:u.pathname,title:(await page.title()).slice(0,120),
  search_box_count:await page.locator('input[name=q],textarea[name=q]').count()}};
"""

COLLECT_RESULTS=r"""
const final=new URL(page.url());
if(!['www.google.com','google.com','www.google.co.kr','google.co.kr','www.bing.com','bing.com'].includes(final.hostname)||final.pathname!='/search')return {rio_research:{error:'SEARCH_TAB_REQUIRED'}};
const rows=await page.locator('a:has(h3),li.b_algo h2 a').evaluateAll(as=>as.slice(0,20).map(a=>({title:(a.querySelector('h3')?.innerText||a.innerText||'').slice(0,180),url:a.href})));
return {rio_research:{url:page.url(),rows}};
"""


def result_rows(data):
    rows=[];seen=set()
    for row in data.get('rows',[]):
        if isinstance(row,dict) and public_url(row.get('url')) and row['url'] not in seen and row.get('title'):
            seen.add(row['url']);rows.append({'index':len(rows)+1,'title':str(row['title'])[:180],'url':row['url']})
        if len(rows)==5:break
    return rows


class Browser:
    def __init__(self,client,mode='extension'):
        self.client=client;self.lock=asyncio.Lock();self.phase='AWAITING_TAB_APPROVAL'
        self.mode=mode_name(mode);self.proof=ROOT/'proof'/mode if mode!='extension' else ROOT/'proof'
        if mode!='extension':self.phase='STARTING_DEDICATED_BROWSER'
        self.results=[];self.search_url=None;self.opened_url=None;self.prime_task=None
        self.opened_index=None;self.query=None
        self.start_error=None
        self.failed_navigation_url=None

    def read_scope(self):
        # Only the exact page reached by a confirmed open may supplement search pages.
        if not self.opened_url:return READ
        return ('if(page.url()==='+json.dumps(self.opened_url)+') {const u=new URL(page.url());'
                'return {rio_research:{url:page.url(),origin:u.origin,path:u.pathname,'
                'title:(await page.title()).slice(0,120),search_box_count:0}};}\n'+READ)

    async def code(self,body,timeout=25):
        # Only code owned by this application reaches the private MCP child.
        result=await self.client.call_tool('browser_run_code_unsafe',{'code':'async(page)=>{\n'+body+'\n}'},read_timeout_seconds=timeout)
        return parse_result(result)

    async def prime(self):
        try:
            if self.mode=='managed':
                await self.code('await page.goto("https://www.google.com/",{waitUntil:"domcontentloaded",timeout:15000});'+READ,timeout=30)
            await self.code(READ,timeout=3600)
            self.phase='CONNECTED_WAITING_FOR_QUERY'
        except Exception as error:
            self.start_error=str(error) if isinstance(error,ResearchError) else 'BROWSER_START_UNVERIFIED'
            self.phase='CONNECTION_UNVERIFIED'

    async def status(self):
        if self.prime_task and not self.prime_task.done():
            return {'ok':False,'status':self.phase,'connected':False}
        if self.start_error:
            return {'ok':False,'status':self.start_error,'connected':False}
        info=await self.code(self.read_scope())
        self.phase='CONNECTED_RESEARCH_PAGE' if info.get('url')==self.opened_url else 'CONNECTED_WAITING_FOR_QUERY'
        return {'ok':True,'connected':True,'status':self.phase,'profile':profile_label(self.mode),'transport':self.mode,**info}

    async def handle(self,req):
        validate(req)
        async with self.lock:
            if req['op']=='status':return await self.status()
            if self.prime_task and not self.prime_task.done():raise ResearchError('AWAITING_TAB_APPROVAL')
            if req['op']=='proof':
                await self.status()
                p=self.proof/'connection-ready.png'
                await self.code('await page.screenshot({path:'+json.dumps(str(p))+',fullPage:false,mask:[page.locator("input[type=password],input[autocomplete=one-time-code]")]});return {rio_research:{saved:true}};')
                p.chmod(0o600)
                return {'ok':True,'status':'CONNECTION_PROOF_SAVED','proof':str(p)}
            if req['op']=='search':
                if self.failed_navigation_url:
                    # A new explicit search may leave our own rejected redirect.
                    # Match its exact URL before navigation; never read that page.
                    info={'url':self.failed_navigation_url,'origin':urlparse(self.search_url).scheme+'://'+urlparse(self.search_url).netloc}
                else:info=await self.status()
                query=req['query'].strip()
                base='https://www.bing.com' if urlparse(self.search_url or info['origin']).hostname in ('bing.com','www.bing.com') else 'https://www.google.com'
                url=base+'/search?q='+quote(query)+'&hl=ko'
                body='if(page.url()!=='+json.dumps(info['url'])+')return {rio_research:{error:"SEARCH_CHANGED"}};'
                self.results=[];self.search_url=None;self.opened_url=None;self.opened_index=None;self.query=None
                body+='await page.goto('+json.dumps(url)+',{waitUntil:"domcontentloaded",timeout:15000});'
                body+=COLLECT_RESULTS
                data=await self.code(body)
                self.failed_navigation_url=None
                rows=result_rows(data)
                self.results=rows;self.search_url=data['url'];self.query=query
                return {'ok':bool(rows),'status':'SEARCH_RESULTS_READY' if rows else 'NO_VERIFIED_RESULTS','query':query,'results':rows}
            if req['op']=='back_results':
                if not self.search_url or not self.results:raise ResearchError('NO_CURRENT_SEARCH_RESULT')
                allowed=[u for u in (self.search_url,self.opened_url) if u]
                body='if(!'+json.dumps(allowed)+'.includes(page.url()))return {rio_research:{error:"SEARCH_CHANGED"}};'
                body+='await page.goto('+json.dumps(self.search_url)+',{waitUntil:"domcontentloaded",timeout:15000});'+COLLECT_RESULTS
                data=await self.code(body)
                if data.get('url')!=self.search_url:raise ResearchError('SEARCH_CONTEXT_CHANGED')
                self.results=result_rows(data);self.opened_index=None
                return {'ok':True,'status':'SEARCH_RESULTS_RETURNED','query':self.query,'results':self.results}
            index=req['index']-1
            if index>=len(self.results) or not self.search_url:raise ResearchError('NO_CURRENT_SEARCH_RESULT')
            chosen=self.results[index]
            if not public_url(chosen['url']):raise ResearchError('PUBLIC_PAGE_REQUIRED')
            allowed=[u for u in (self.search_url,self.opened_url) if u]
            body='if(!'+json.dumps(allowed)+'.includes(page.url()))return {rio_research:{error:"SEARCH_CHANGED"}};await page.goto('+json.dumps(chosen['url'])+',{waitUntil:"domcontentloaded",timeout:15000});return {rio_research:{url:page.url(),title:(await page.title()).slice(0,180)}};'
            data=await self.code(body)
            # Search engines can redirect an HTTPS result to an old HTTP article.
            # Try its identical public URL over TLS once, without reading HTTP
            # content or disabling certificate checks. Never try other variants.
            final=data.get('url','')
            secure='https:'+final[5:] if final.startswith('http:') else None
            if secure and public_url(secure):
                data=await self.code('if(page.url()!=='+json.dumps(final)+')return {rio_research:{error:"SEARCH_CHANGED"}};'
                    'await page.goto('+json.dumps(secure)+',{waitUntil:"domcontentloaded",timeout:15000});'
                    'return {rio_research:{url:page.url(),title:(await page.title()).slice(0,180)}};')
            if not public_url(data.get('url')):
                self.failed_navigation_url=data.get('url')
                scheme=urlparse(data.get('url','')).scheme
                raise ResearchError('FINAL_PAGE_NOT_PUBLIC_RESEARCH_'+({'http':'HTTP','chrome-error':'NETWORK_ERROR'}.get(scheme,'REJECTED')))
            self.opened_url=data['url']
            self.opened_index=index+1
            return {'ok':True,'status':'RESULT_PAGE_OPENED','index':self.opened_index,**data}


async def run():
    from mcp import ClientSession,StdioServerParameters
    from mcp.client.stdio import stdio_client
    from mcp.types import Implementation
    os.umask(0o077)
    owner=asyncio.current_task()
    asyncio.get_running_loop().add_signal_handler(signal.SIGTERM,owner.cancel)
    mode=mode_name(os.getenv('RIO_BROWSER_TRANSPORT','extension'))
    private_directory(ROOT)
    if mode=='extension' and not (profile(mode)/'Local State').is_file():raise ResearchError('DEDICATED_PROFILE_REQUIRED')
    if mode=='managed':
        private_directory(profile(mode))
        if profile_in_use(mode):raise ResearchError('DEDICATED_PROFILE_ALREADY_OPEN')
    if mode=='cdp':cdp_identity()
    proof=private_directory(ROOT/'proof'/mode) if mode!='extension' else private_directory(ROOT/'proof')
    endpoint=transport_endpoint(mode)
    if endpoint.exists():raise ResearchError('SOCKET_ALREADY_EXISTS')
    cli=Path.home()/'.local/share/rio-browser/mcp-runtime/node_modules/@playwright/mcp/cli.js'
    env={k:os.environ[k] for k in ('PATH','DISPLAY','XAUTHORITY','WAYLAND_DISPLAY','XDG_RUNTIME_DIR','DBUS_SESSION_BUS_ADDRESS','LANG') if k in os.environ}
    params=StdioServerParameters(command='node',args=[str(cli),*mcp_arguments(mode,proof)],cwd=str(ROOT),env=env)
    with open(os.devnull,'w') as logs:
        async with stdio_client(params,errlog=logs) as (reader,writer):
            async with ClientSession(reader,writer,read_timeout_seconds=30,client_info=Implementation(name='Rio dedicated research',version='0.1.0')) as client:
                await client.initialize()
                browser=Browser(client,mode);browser.prime_task=asyncio.create_task(browser.prime())
                async def connection(reader,writer):
                    try:
                        peer=writer.get_extra_info('socket').getsockopt(socket.SOL_SOCKET,socket.SO_PEERCRED,12)
                        if struct.unpack('3i',peer)[1]!=os.getuid():raise ResearchError('PEER_NOT_ALLOWED')
                        raw=await asyncio.wait_for(reader.readline(),timeout=5)
                        if len(raw)>4096:raise ResearchError('REQUEST_TOO_LARGE')
                        response=await browser.handle(json.loads(raw))
                    except Exception as e:response={'ok':False,'status':str(e) if isinstance(e,ResearchError) else 'BROWSER_OPERATION_UNVERIFIED'}
                    try:
                        writer.write((json.dumps(response,ensure_ascii=False)+'\n').encode());await writer.drain()
                    except (OSError,ConnectionError):pass
                    finally:writer.close();await writer.wait_closed()
                server=await asyncio.start_unix_server(connection,path=str(endpoint),limit=8192);endpoint.chmod(0o600)
                try:
                    async with server:await server.serve_forever()
                finally:
                    endpoint.unlink(missing_ok=True);browser.prime_task.cancel()


if __name__=='__main__':
    try:asyncio.run(run())
    except (KeyboardInterrupt,asyncio.CancelledError):pass
    except BaseException as e:
        print(json.dumps({'status':'RESEARCH_START_FAILED','error_type':type(e).__name__}),flush=True)
        raise SystemExit(1)
