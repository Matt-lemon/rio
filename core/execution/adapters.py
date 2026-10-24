"""Structured wrappers around existing protected implementations; no new control API."""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
import socket
import stat
import time

from .types import ToolResult, encoded, safe_text


def registry_request(action, **arguments):
    if action not in {'capabilities', 'list', 'describe', 'call', 'job'}:
        raise ValueError('REGISTRY_OPERATION_FORBIDDEN')
    path = Path(os.getenv('RIO_TOOLS_SOCKET', str(Path.home()/'.local/state/rio/tools/service.sock')))
    info = path.lstat()
    if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ValueError('PRIVATE_REGISTRY_REQUIRED')
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(5)
        client.connect(str(path))
        client.sendall((encoded({'action': action, **arguments}) + '\n').encode())
        data = bytearray()
        while not data.endswith(b'\n'):
            block = client.recv(65536)
            if not block or len(data) + len(block) > 262144:
                raise ValueError('REGISTRY_RESPONSE_LIMIT')
            data.extend(block)
    result = json.loads(data)
    if result.get('ok') is not True:
        raise ValueError('REGISTRY_UNAVAILABLE')
    return result['result']


def registry_search(query):
    """Search metadata locally; mixed Korean/identifier phrases need not be substrings."""
    rows = registry_request('list', search='')
    terms = re.findall(r'[a-z0-9]+|[가-힣]+', query.casefold())
    terms = [t for t in terms if t not in {'도구', '검사', '검색', '등록된', '찾기', 'tool', 'tools'}] or terms
    if '자막' in terms:
        terms.append('srt')
    ranked = []
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict):
            continue
        contract = row.get('contract', row)
        haystack = ' '.join(str(contract.get(k, '')) for k in ('name', 'description', 'capability')).casefold()
        score = sum(t in haystack for t in terms)
        if score:
            ranked.append((score, row))
    return [row for _, row in sorted(ranked, key=lambda pair: pair[0], reverse=True)]


class Adapters:
    def __init__(self, store):
        self.store = store

    def call_checked(self, descriptor, args, key, browser):
        return self.call(descriptor['name'], args, key, browser, expected_hash=descriptor.get('package_hash'))

    def call_tracked(self, descriptor, args, key, browser, progress):
        from .catalog import Catalog
        if Catalog.effect(descriptor, args) == 'read' or descriptor['name'].startswith('browser.'):
            progress.attempted('READ_OR_NAVIGATION_BOUNDARY')
        return self.call(descriptor['name'], args, key, browser, expected_hash=descriptor.get('package_hash'), progress=progress)

    def call(self, name, args, key, browser, *, expected_hash=None, progress=None):
        if name.startswith('registered.'):
            from .catalog import approved_generated
            if registry_request('capabilities').get('expected_hash') is not True:
                return ToolResult('REJECTED', '등록 서비스가 버전 고정 호출을 지원하지 않아 실행하지 않았어.', request_id=key)
            original = name.removeprefix('registered.')
            item = registry_request('describe', name=original)
            if not approved_generated(item):
                return ToolResult('REJECTED', '검증·승인된 읽기 도구가 아니라 실행하지 않았어.', request_id=key)
            if expected_hash is not None and item['hash'] != expected_hash:
                return ToolResult('REJECTED', '등록 도구 버전이 실행 직전에 바뀌어 멈췄어.', request_id=key, code='TOOL_REVISION_CHANGED')
            if progress:
                progress.attempted('PINNED_REGISTRY_CALL')
            result = registry_request('call', name=original, args=args, request_id=key, expected_hash=item['hash'])
            if progress and result.get('request_id') == key and result.get('status') in {'QUEUED', 'RUNNING', 'SUCCEEDED', 'FAILED'}:
                progress.accepted('REGISTRY_REQUEST_RECEIPT')
            until = time.monotonic() + 20
            while result.get('status') in {'QUEUED', 'RUNNING'} and time.monotonic() < until:
                time.sleep(0.2)
                result = registry_request('job', request_id=key)
            return self.registered_result(result, key, item['hash'])
        if name.startswith('browser.'):
            from core.research_browser import format_reply
            op = name.split('.', 1)[1]
            owner = self.store.row("SELECT value FROM meta WHERE key='browser_owner'")
            session = getattr(browser, 'session_id', '')
            if op in {'open_result', 'back_results'} and (not owner or owner['value'] != session):
                return ToolResult('NEED_INPUT', '다른 대화에서 검색 화면이 바뀌었어. 이 대화에서 다시 검색해 줘.', request_id=key)
            if op == 'open_topic':
                raw = browser.open_topic_result(args['query'])
            else:
                command = {'search': lambda: 'search ' + args['query'],
                           'open_url': lambda: 'url ' + args['url'],
                           'open_result': lambda: 'open ' + str(args['index']),
                           'back_results': lambda: 'back', 'status': lambda: 'status'}[op]()
                raw = browser.run_result(command)
            if op in {'search', 'open_topic', 'open_url'}:
                with self.store.transaction(progress.claim if progress else None, running=False, revision=False) as db:
                    db.execute("INSERT INTO meta VALUES('browser_owner',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (session,))
            evidence = {k: raw[k] for k in ('status', 'title', 'url', 'results', 'query', 'index','requested_host','site_verified','expected_host','actual_host','requested_url','redirected') if k in raw}
            # A title and URL are navigation evidence, never page-content evidence.
            evidence['content_read'] = False
            return ToolResult('NEED_INPUT' if raw.get('needs_selection') else 'OBSERVED' if raw.get('ok') else 'UNKNOWN',
                              format_reply(raw), evidence, key, 'NAVIGATION' if raw.get('ok') else 'NONE')
        if name == 'web.search':
            from dataclasses import asdict
            try:
                from web_search import web_search
                rows = [asdict(r) for r in web_search(args['query'], max_results=4, timeout=10)]
            except ImportError:
                return ToolResult('UNAVAILABLE',
                                  '웹 검색 실행 환경의 필수 패키지를 불러오지 못했어. '
                                  '이번 요청의 사실이나 최신 수치는 확인하지 못했어.',
                                  {'results': [], 'content_read': False}, key,
                                  code='SEARCH_DEPENDENCY_UNAVAILABLE')
            except Exception:
                # Raw provider exceptions can contain URLs or credentials.
                # Read failure is not an uncertain physical command receipt.
                return ToolResult('UNAVAILABLE',
                                  '웹 검색 공급자의 응답을 확인하지 못했어. '
                                  '확인된 자료가 없어 뜻이나 최신 수치를 추측해 답하지 않을게.',
                                  {'results': [], 'content_read': False}, key,
                                  code='SEARCH_PROVIDER_UNAVAILABLE')
            message = '검색 요약문 기준 자료야.\n' + '\n'.join(
                r['title'] + '\n' + r['snippet'] + '\n' + r['url'] for r in rows) if rows else '검색 결과를 확인하지 못했어.'
            return ToolResult('OBSERVED' if rows else 'UNAVAILABLE', message,
                              {'results': rows, 'content_read': False}, key, 'SEARCH_SNIPPETS' if rows else 'NONE',
                              None if rows else 'SEARCH_NO_RESULTS')
        if name == 'spotify.control':
            from core.spotify import SpotifyController
            controller = SpotifyController()
            if progress and args['action'] != 'status':
                self.track_method(controller.transport, 'command', progress, 'MPRIS_METHOD', lambda result: True)
            raw = controller.call(args['action'])
            state = raw.get('playback')
            message = {'Playing': 'Spotify가 재생 중이라고 보고했어.', 'Paused': 'Spotify가 일시정지라고 보고했어.',
                       'Stopped': 'Spotify가 정지 상태라고 보고했어.'}.get(state, '음악 요청 결과를 확인하지 못했어. 자동으로 다시 보내지 않을게.')
            return ToolResult(raw['status'], message, raw, key, 'OBSERVED' if state else 'NONE', raw.get('code'))
        if name.startswith('spotify.timer.'):
            return self.timer(name, args, key, progress)
        if name == 'home.smartthings':
            from core.home.smartthings_direct import from_environment
            direct = from_environment(args['target'])
            target, action = args['target'], args['action']
            if action == 'status':
                state = direct.status()
                message = ('SmartThings '+target+' 스위치 보고값: '+
                           ('켜짐' if state['switch'] == 'on' else '꺼짐')+
                           '. 실제 전등 상태는 별도 확인이 필요해.')
                return ToolResult('OBSERVED', message, state, key, 'SWITCH_REPORT')
            raw = direct.set_power(action == 'on', request_key=key,
                                   before_dispatch=(lambda: progress.attempted('ST_CLI_DISPATCH_BOUNDARY')) if progress else None)
            result = raw['result']
            if result == 'OBSERVED_SWITCH':
                return ToolResult('OBSERVED', f'SmartThings {target}가 요청한 스위치 상태를 새 변경 시각으로 보고했어. 실물 상태는 별도 확인이 필요해.',
                                  raw, key, 'SWITCH_REPORT')
            if result == 'ALREADY_REPORTED':
                return ToolResult('OBSERVED',f'SmartThings {target} 스위치가 이미 요청한 상태로 보고됐어. 명령을 보내지 않았어.',
                                  raw, key, 'SWITCH_REPORT')
            if result == 'REJECTED':
                return ToolResult('REJECTED', f'SmartThings {target} 제어가 허용되지 않았어. 명령을 보내지 않았어.', raw, key, 'NONE', raw.get('code'))
            if result == 'PENDING_DO_NOT_RETRY':
                return ToolResult('UNKNOWN',f'SmartThings {target}의 이전 명령이 미확정이야. 새 명령을 보내지 않았어.', raw, key)
            if result == 'REJECTED':
                return ToolResult('REJECTED',f'SmartThings {target}의 현재 정책·대상 검증을 통과하지 못해 명령을 보내지 않았어.',raw,key,'NONE',raw.get('code'))
            if raw.get('post_command_status_checked') is True:
                switch = raw.get('post_command_switch_report')
                reported = ('켜짐' if switch == 'on' else '꺼짐') if switch in {'on','off'} else '미확인'
                message = (f'SmartThings {target} 명령 후 상태를 한 번 확인했어: {reported}. '
                           '단, 기기 변경 시각으로 이 명령의 반영을 입증하지 못했어. 자동 재전송하지 않아.')
            else:
                message = f'SmartThings {target} 명령 후 상태 조회도 실패해 결과가 미확정이야. 자동 재전송하지 않아.'
            return ToolResult('UNKNOWN', message, raw, key, 'NONE', raw.get('code'))
        if name.startswith('home.'):
            return self.home(name, args, key, progress)
        if name in {'pc.status', 'pc.wake', 'pc.shutdown'}:
            from core.home.smartthings_pc import controller_from_environment, result_message
            controller = controller_from_environment()
            action = {'pc.status': 'status', 'pc.wake': 'on', 'pc.shutdown': 'off'}[name]
            if progress and action != 'status':
                self.track_method(controller.transport, 'on' if action == 'on' else 'off', progress, 'PC_POWER_HTTP',
                                  lambda raw: isinstance(raw, dict) and len(raw.get('results', [])) == 1 and raw['results'][0].get('status') == 'ACCEPTED')
            raw = controller.observe() if action == 'status' else controller.wake(key) if action == 'on' else controller.shutdown(key)
            code = raw.get('result')
            state = 'OBSERVED' if action == 'status' or code in {'ALREADY_REACHABLE', 'OFF_NOT_REACHABLE'} else 'DISPATCHED' if code == 'ACCEPTED' else 'UNKNOWN' if code in {'UNKNOWN', 'PENDING_DO_NOT_RETRY'} else 'REJECTED'
            return ToolResult(state, result_message(raw, action), raw, key, 'OBSERVED' if state == 'OBSERVED' else 'NONE', code)
        if name == 'dubbing.health':
            from rio_dubbing_client import DubbingClient
            from rio_dubbing_tools import _format_health
            try:
                raw = DubbingClient().health()
            except Exception:
                from core.dubbing_link import read_status
                try:
                    observed=read_status()
                    state=observed['state'] if not observed['stale'] else 'UNVERIFIED'
                    reason=observed.get('reason','UNVERIFIED') if not observed['stale'] else 'STALE_LINK_OBSERVATION'
                except (OSError,ValueError,KeyError):
                    state,reason='UNVERIFIED','NO_FRESH_LINK_OBSERVATION'
                message={'WAITING_FOR_PEER':'PC 연결을 기다리고 있어. 전원이 꺼졌다고 확정한 것은 아니야.',
                         'WAITING_FOR_API':'SSH 연결은 있으나 더빙 API가 아직 응답하지 않아. 서버를 자동으로 시작하지 않았어.',
                         'CONFIGURATION_ERROR':'더빙 연결 설정이나 인증 확인이 필요해. 자동으로 인증을 반복하지 않아.'}.get(state,'더빙 API의 준비 상태를 확인하지 못했어.')
                return ToolResult('UNAVAILABLE',message,{'link_state':state,'link_reason':reason,'api_ready':False},key,'OBSERVED' if state!='UNVERIFIED' else 'NONE',reason)
            return ToolResult('OBSERVED', _format_health(raw),
                              {'ok': raw.get('ok'), 'worker': {k: raw.get('worker', {}).get(k) for k in ('enabled', 'accepting_jobs', 'state')}}, key, 'OBSERVED')
        if name.startswith('registry.'):
            raw = registry_search(args['query']) if name.endswith('search') else registry_request('describe', name=args['name'])
            rows = raw if isinstance(raw, list) else [raw]
            public = [{k: row.get('contract', row).get(k) for k in
                       ('name', 'description', 'version', 'input_schema', 'output_schema', 'capability', 'permissions')}
                      for row in rows[:20] if isinstance(row, dict)]
            message = '등록 도구의 규격이야. 실행이나 제작은 하지 않았어.\n' + '\n'.join(
                str(row.get('name')) + ': ' + str(row.get('description', ''))[:200] for row in public)
            if name.endswith('describe') and public:
                message += '\n입력 규격: ' + safe_text(encoded(public[0].get('input_schema')), 2000)
            return ToolResult('OBSERVED', message if public else
                              '재사용할 등록 도구를 찾지 못했어. 필요한 기능과 검증 방법을 먼저 제안할게.', {'tools': public}, key, 'REGISTRY_METADATA')
        if name == 'core.status':
            return ToolResult('OBSERVED', '리오 실행 코어가 응답하고 있어.',
                              {'persistent_tasks': True, 'max_calls': 8, 'max_seconds': 300,
                               'automatic_build': False, 'page_content_reader': False}, key, 'OBSERVED')
        if name.startswith('legacy.'):
            family = name.split('.')[1]
            if not args['command'].startswith('/' + family + ' '):
                return ToolResult('REJECTED', '명령 범위가 일치하지 않아 실행하지 않았어.', request_id=key)
            if family == 'home':
                from core.home.main_adapter import handle_home_command as callback
            elif family == 'pc':
                from core.home.smartthings_pc import handle_pc_command as callback
            else:
                from rio_dubbing_tools import handle_dubbing_command as callback
            # Opaque legacy replies are delivered as such, never parsed into an
            # acknowledgement or used to authorize a follow-up physical change.
            if family == 'dubbing' and progress:
                from rio_dubbing_client import DubbingClient
                client = DubbingClient()
                original = client._request
                def request(method, path, *a, **kw):
                    if method != 'GET':
                        progress.attempted('DUBBING_HTTP_BOUNDARY')
                    raw = original(method, path, *a, **kw)
                    # HTTP success plus the server's typed job receipt confirms
                    # acceptance only, not a rendered output or Core-ID lookup.
                    if (method != 'GET' and isinstance(raw, dict)
                            and isinstance(raw.get('id'), str) and re.fullmatch(r'[A-Za-z0-9_-]{1,100}', raw['id'])
                            and raw.get('status') in {'queued', 'running', 'blocked', 'failed', 'completed', 'preview_ready', 'analysis_ready'}):
                        progress.accepted('DUBBING_JOB_RECEIPT')
                    return raw
                client._request = request
                text = callback(args['command'], client=client)
            else:
                if progress:
                    progress.attempted('OPAQUE_LEGACY_BOUNDARY')
                text = callback(args['command'],request_key=key) if family=='home' else callback(args['command'])
            return ToolResult('UNKNOWN', (text or '기존 명령을 확인하지 못했어.') + '\n구조화된 접수·결과를 확인할 수 없어 미확정으로 보존했어. 자동 재전송하지 않을게.',
                              {'structured_outcome': False, 'lookup_supported': False}, key)
        raise ValueError('UNKNOWN_TOOL')

    @staticmethod
    def track_method(transport, method, progress, source, accepted):
        original = getattr(transport, method)
        def tracked(*args, **kwargs):
            progress.attempted(source)
            raw = original(*args, **kwargs)
            if accepted(raw):
                progress.accepted(source + '_RECEIPT')
            return raw
        setattr(transport, method, tracked)

    @staticmethod
    def track_home(transport, progress, key):
        if progress is None:
            return
        original = transport.request
        def tracked(path, data=None):
            mutation = path == '/v1/commands' and data is not None
            if mutation:
                progress.attempted('HOME_COMMAND_HTTP')
            raw = original(path, data) if data is not None else original(path)
            if mutation and isinstance(raw, dict) and raw.get('id') == key and raw.get('status') in {'DISPATCHED', 'SUCCEEDED', 'REJECTED'}:
                progress.accepted('HOME_GATEWAY_RECEIPT')
            return raw
        transport.request = tracked

    def home(self, name, args, key, progress=None):
        from core.home.main_adapter import registry_from_environment
        from core.home.tools import user_message, HomeToolRegistry
        registry = None
        try:
            def before_google_dispatch(target, action):
                # Only a preflight BRIDGE_NOT_READY can trigger a separately
                # approved fallback. An SDK error after a write never can.
                # Existing protected adapter implementations (and old
                # test doubles) must retain their original behavior when
                # optional fallback capabilities are absent.
                if not callable(getattr(registry, 'plan', None)):
                    return None
                if isinstance(getattr(registry, 'configuration', None), dict):
                    enabled = registry.configuration.get('enabled') is True
                elif isinstance(getattr(registry, 'allow_execution', None), bool):
                    enabled = registry.allow_execution is True
                else:
                    return None
                if not enabled:
                    return None
                try:
                    plan = registry.plan(target, action == 'on') if name == 'home.light' else registry.plan(target, action)
                except (OSError, TimeoutError, ValueError):
                    # The independent bridge-health probe below must still
                    # substantiate an outage before selecting SmartThings.
                    plan = {'status': 'REJECTED', 'code': 'BRIDGE_NOT_READY'}
                if plan.get('status') != 'REJECTED' or plan.get('code') != 'BRIDGE_NOT_READY':
                    return None
                from core.home.smartthings_fallback import attempt
                raw = attempt(target,action,key,registry,progress)
                state = raw.get('result')
                if state == 'OBSERVED_SWITCH':
                    return ToolResult('OBSERVED',f'Google Home 브리지가 연결되지 않아 SmartThings가 {target} 스위치의 새 변경 보고를 확인했어. 실물 상태는 별도 확인이 필요해.',
                                      raw,key,'SWITCH_REPORT')
                if state == 'ALREADY_REPORTED':
                    return ToolResult('OBSERVED',f'Google Home 연결 불가. SmartThings에서 {target}이 이미 요청한 상태라고 보고했어. 명령은 보내지 않았어.',
                                      raw,key,'SWITCH_REPORT')
                if state == 'UNKNOWN':
                    return ToolResult('UNKNOWN',f'Google Home 연결 불가. SmartThings {target} 요청은 전송됐으나 결과 미확정이야. 자동 재전송하지 않아.',
                                      raw,key,'NONE',raw.get('code'))
                if state == 'PENDING_DO_NOT_RETRY':
                    return ToolResult('UNKNOWN',f'Google Home 연결 불가. 이전 SmartThings {target} 명령이 미확정이라 새 명령을 보내지 않았어.',raw,key)
                return ToolResult('REJECTED',f'Google Home 연결 불가. SmartThings {target} 폴백은 물리적 동일 기기 검증 또는 명령 이력 조건을 충족하지 못해 실행하지 않았어.',
                                  raw,key,'NONE',state)
            if name.startswith('home.device.'):
                from core.home.device_controls import registry_from_environment as devices
                registry = devices()
                target = name.removeprefix('home.device.')
                # Only power command families have a compatible SmartThings switch fallback.
                if target in {'에어컨','lens'} and args['action'] in {'on','off'}:
                    before = before_google_dispatch(target,args['action'])
                    if before is not None:
                        return before
                self.track_home(registry.transport, progress, key)
                raw = registry.wait_result(registry.execute(target, args['action'], key))
            else:
                registry = registry_from_environment()
                if name == 'home.light':
                    before = before_google_dispatch(args['target'],args['action'])
                    if before is not None:
                        return before
                self.track_home(registry.transport, progress, key)
                if name == 'home.status':
                    raw = registry.call('home_get_status', {})
                    ready = raw.get('available') is True and raw.get('permission') == 'GRANTED' and raw.get('bridge_state') == 'READY'
                    return ToolResult('OBSERVED' if ready else 'UNAVAILABLE',
                                      'Google Home 연결과 권한이 준비돼 있어.' if ready else 'Google Home 연결이나 권한 확인이 필요해.', raw, key, 'OBSERVED')
                raw = registry.wait_result(registry.call('home_set_light', {'target': args['target'], 'on': args['action'] == 'on', 'idempotency_key': key}))
            message = user_message(raw)
            if name.startswith('home.device.'):
                message = message.replace('조명', '기기').replace('실제 불 상태', '실제 기기 상태')
            if raw.get('code') and raw['status'] == 'REJECTED':
                message += ' 확인 코드: ' + safe_text(raw['code'], 80)
            return ToolResult(raw['status'], message, raw, raw.get('request_id', key),
                              'SDK_ACK' if raw['status'] == 'SUCCEEDED' else 'NONE', raw.get('code'))
        finally:
            if registry is not None:
                registry.close()

    def status(self, name, args, key):
        """Known result lookup only; this method never dispatches an operation."""
        if name.startswith('registered.'):
            return self.registered_result(registry_request('job', request_id=key), key)
        if name == 'home.smartthings':
            from core.home.smartthings_fallback import lookup_existing
            outcome=lookup_existing(args['target'],key) or {'result':'UNKNOWN','commands_sent':0}
            if outcome['result'] == 'OBSERVED_SWITCH':
                return ToolResult('OBSERVED', f'SmartThings {args["target"]} 스위치 변경을 새 보고 시각으로 확인했어. 실제 전등 상태는 별도 확인이 필요해.',
                                  outcome, key, 'SWITCH_REPORT')
            return ToolResult('UNKNOWN', 'SmartThings 보고 상태만으로 이전 명령 반영을 입증하지 못했어. 재전송하지 않아.',
                              outcome, key)
        if name == 'home.light' or name.startswith('home.device.'):
            # A prior Google Home outage may have selected SmartThings before
            # any primary write. Its original request must be looked up there.
            from core.home.smartthings_fallback import lookup_existing
            target = args['target'] if name == 'home.light' else name.removeprefix('home.device.')
            try:
                existing = lookup_existing(target,key)
            except Exception:
                existing = None
            if existing is not None:
                if existing['result'] == 'OBSERVED_SWITCH':
                    return ToolResult('OBSERVED',f'SmartThings {target}가 이전 전원 명령 이후 새 스위치 상태를 보고했어. 실물 동작은 별도 확인이 필요해.',
                                      existing,key,'SWITCH_REPORT')
                return ToolResult('UNKNOWN',f'SmartThings {target}의 이전 명령을 새 상태 변화로 확인하지 못했어. 재전송하지 않아.',existing,key)
            from core.home.main_adapter import registry_from_environment
            from core.home.tools import HomeToolRegistry, user_message
            registry = registry_from_environment()
            try:
                if not registry.ledger_path.exists() or not registry.ledger.known(key):
                    return ToolResult('UNKNOWN', '전송 기록과 결과를 확인하지 못했어. 재전송하지 않을게.', request_id=key)
                response = registry.transport.result(key)
                if not isinstance(response, dict) or response.get('http_status') or response.get('status') not in {'SUCCEEDED', 'REJECTED', 'UNKNOWN', 'DISPATCHED'}:
                    return ToolResult('UNKNOWN', '이전 요청 결과를 확인하지 못했어.', request_id=key)
                raw = HomeToolRegistry.answer(response, key)
                registry.ledger.refresh_dispatched(lambda requested: response if requested == key else registry.transport.result(requested))
                return ToolResult(raw['status'], user_message(raw), raw, key,
                                  'SDK_ACK' if raw['status'] == 'SUCCEEDED' else 'NONE', raw.get('code'))
            finally:
                registry.close()
        return ToolResult('UNKNOWN', '이 요청은 자동 재전송하지 않아. 새 상태 확인이나 사용자 확인이 필요해.', request_id=key)

    @staticmethod
    def registered_result(result, key, expected_hash=None):
        if result.get('request_id') != key or (expected_hash is not None and result.get('package_hash') != expected_hash):
            return ToolResult('UNKNOWN', '등록 도구의 결과 ID나 버전을 확인하지 못했어.', request_id=key)
        status = result.get('status')
        if status == 'SUCCEEDED':
            return ToolResult('SUCCEEDED', '검증된 기존 도구의 실행 결과야.\n' + safe_text(encoded(result.get('result')), 2000),
                              {'package_hash': result.get('package_hash'), 'output': result.get('result'),
                               'result_is_data': True}, key, 'VALIDATED_OUTPUT')
        if status in {'FAILED', 'CANCELLED'}:
            return ToolResult(status, '기존 도구 실행이 완료되지 않았어. 결과를 보존하고 멈췄어.', request_id=key)
        return ToolResult('DISPATCHED' if status in {'QUEUED', 'RUNNING'} else 'UNKNOWN',
                          '기존 도구 결과를 확인 중이야. 같은 요청을 재전송하지 않아.', request_id=key)

    def timer(self, name, args, key, progress=None):
        from core.spotify_timer import TimerStore, format_deadline
        from core.spotify import SpotifyController
        timer = TimerStore()
        if name.endswith('status'):
            raw = timer.status()
            message = '음악 정지 예약 상태: ' + raw['state']
            if raw['state'] == 'ARMED':
                message = '음악을 ' + format_deadline(raw['due']) + '에 멈출 예정이야.'
                if not timer.active():
                    message += ' 다만 예약 실행 서비스는 현재 연결되지 않았어.'
            return ToolResult('OBSERVED', message, raw, key, 'OBSERVED')
        if name.endswith('cancel'):
            if progress:
                progress.attempted('LOCAL_TIMER_CANCEL')
            raw = timer.cancel()
            return ToolResult('REJECTED' if raw['state'] == 'EXECUTING' else 'SUCCEEDED', '음악 정지 예약 상태: ' + raw['state'], raw, key)
        outcome = {}
        def arm():
            if progress:
                progress.attempted('LOCAL_TIMER_SCHEDULE')
            outcome.update(timer.schedule(args['due'], minimum_delay=10 if args['play'] else 0))
            if progress and outcome.get('status') == 'SCHEDULED':
                progress.accepted('LOCAL_TIMER_STORED')
            return outcome
        controller = SpotifyController()
        if progress and args['play']:
            self.track_method(controller.transport, 'command', progress, 'MPRIS_METHOD', lambda result: True)
        playback = controller.call('play', before_write=arm) if args['play'] else None
        if playback is None:
            arm()
        if outcome.get('status') != 'SCHEDULED':
            return ToolResult('REJECTED', '예약을 등록하지 못했어. 재생도 새로 시작하지 않았어.', outcome, key)
        return ToolResult('UNKNOWN' if playback and playback.get('status') != 'SUCCEEDED' else 'SCHEDULED',
                          '음악을 ' + format_deadline(outcome['due']) + '에 멈추도록 예약했어.' +
                          (' 재생 결과는 미확정이야.' if playback and playback.get('status') != 'SUCCEEDED' else ''),
                          {'timer': outcome, 'playback': playback}, key, 'SCHEDULE_STORED')
