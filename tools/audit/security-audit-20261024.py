"""Offline audit reproduction, not a passing security gate or a production fix.

Uses URL parsers and a mocked browser boundary only. No browser is started,
no network request is sent, and no operator configuration/state is opened.
Run with the preview Python dependencies and Node installed.
"""
import asyncio
import importlib.util
import json
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from core.browser_url import public_url


def parser_cases():
    cases = [
        'https://127.1/', 'https://0x7f.1/', 'https://0177.0.0.1/',
        'https://10.1/', 'https://192.168.1/',
        'https://%31%32%37.0.0.1/', 'https://accounts%2egoogle.com/',
        'https://accounts\u3002google.com/',
    ]
    # WHATWG URL parsing is performed without opening a browser or URL.
    code = ('for (const input of JSON.parse(process.argv[1])) '
            'console.log(JSON.stringify({input,canonical:new URL(input).href}));')
    raw = subprocess.check_output(['node', '-e', code, json.dumps(cases)], text=True)
    return [{**json.loads(line), 'accepted': public_url(json.loads(line)['input'])}
            for line in raw.splitlines()]


async def browser_boundary():
    source = ROOT / 'services/rio-browser-research/rio_browser_research/service.py'
    spec = importlib.util.spec_from_file_location('audit_browser_service', source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    class MockBrowser(module.Browser):
        def __init__(self):
            super().__init__(None)
            self.generated = []

        async def status(self):
            return {'url': 'https://www.google.com/',
                    'origin': 'https://www.google.com'}

        async def code(self, body, timeout=25):
            self.generated.append(body)
            return {'url': 'https://127.0.0.1/', 'title': 'mock title'}

    browser = MockBrowser()
    rejection = None
    try:
        await browser.handle({'op': 'open_url', 'url': 'https://127.1/'})
    except module.ResearchError as error:
        rejection = str(error)
    return {
        'mock_goto_generated': any('page.goto("https://127.1/"' in body
                                   for body in browser.generated),
        'rejection_after_mock_navigation': rejection,
        'actual_network_requests': 0,
    }


if __name__ == '__main__':
    print(json.dumps({'cases': parser_cases(),
                      'boundary': asyncio.run(browser_boundary())},
                     ensure_ascii=False, indent=2))
