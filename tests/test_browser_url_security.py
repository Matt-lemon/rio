"""Offline URL parser and navigation guard regressions (no real browsing)."""
import asyncio
import importlib.util
import json
from pathlib import Path
import subprocess

import pytest

from core.browser_url import public_url


ROOT = Path(__file__).resolve().parents[1]
SERVICE = ROOT / 'services/rio-browser-research/rio_browser_research/service.py'
spec = importlib.util.spec_from_file_location('rio_research_guard_test', SERVICE)
browser_service = importlib.util.module_from_spec(spec)
spec.loader.exec_module(browser_service)


@pytest.mark.parametrize('url', [
    'https://127.1/', 'https://0x7f.1/', 'https://0177.0.0.1/',
    'https://10.1/', 'https://192.168.1/',
    'https://%31%32%37.0.0.1/', 'https://accounts%2egoogle.com/',
    'https://accounts。google.com/', 'https://[::1]/',
    'https://127.0.0.1/', 'https://8.8.8.8/',
])
def test_ambiguous_numeric_and_encoded_authority_is_rejected(url):
    assert not public_url(url)
    with pytest.raises(browser_service.ResearchError, match='PUBLIC_PAGE_REQUIRED'):
        browser_service.guarded_navigation(url)


@pytest.mark.parametrize('url', [
    'https://example.com/', 'https://docs.python.org/3/',
    'https://www.google.com/search?q=memory',
])
def test_public_https_dns_names_remain_accepted(url):
    assert public_url(url)
    code = browser_service.guarded_navigation(url)
    assert code.index("await page.route('**/*',rioRoute)") < code.index('await page.goto(')
    assert 'request.redirectedFrom()' in code
    assert "await page.unroute('**/*',rioRoute)" in code


def test_browser_guard_blocks_redirect_before_network_with_node_fake():
    """Exercise generated JavaScript routing against a fake Playwright page."""
    body = browser_service.guarded_navigation('https://example.com/')
    code = r'''
    const ops=[];
    const page={
      route:async (_mask,fn)=>{ops.push('route');page.handler=fn;},
      unroute:async ()=>ops.push('unroute'),
      goto:async target=>{
        ops.push('goto');
        const request={url:()=>target,isNavigationRequest:()=>true,redirectedFrom:()=>null};
        await page.handler({request:()=>request,abort:async()=>ops.push('abort'),continue:async()=>ops.push('continue')});
        const privateRequest={url:()=> 'https://127.0.0.1/admin',isNavigationRequest:()=>true,redirectedFrom:()=>request};
        await page.handler({request:()=>privateRequest,abort:async()=>ops.push('abort_redirect'),continue:async()=>ops.push('continue_redirect')});
      }
    };
    (async()=>{await new Function('page','return (async()=>{' + process.argv[1] + '})()')(page);
      process.stdout.write(JSON.stringify(ops));})().catch(e=>{console.error(e);process.exitCode=1;});
    '''
    result = subprocess.check_output(['node', '-e', code, body], text=True)
    assert json.loads(result) == ['route', 'goto', 'continue', 'abort_redirect', 'unroute']


def test_browser_handler_rejects_numeric_before_any_mcp_call():
    class BrowserFake(browser_service.Browser):
        def __init__(self):
            super().__init__(None)
            self.generated = []

        async def code(self, body, timeout=25):
            self.generated.append(body)
            return {'url': 'https://www.google.com/'}

    browser = BrowserFake()
    with pytest.raises(browser_service.ResearchError, match='PUBLIC_PAGE_REQUIRED'):
        asyncio.run(browser.handle({'op':'open_url','url':'https://127.1/'}))
    assert not browser.generated
