"""One catalog for conversation planning, explicit commands and dispatch checks."""
from __future__ import annotations

import copy
import os
import re
from pathlib import Path

from .builtins import builtin_contracts
from .types import TEXT, enum, fingerprint, spec, validate


def approved_generated(item):
    if not isinstance(item, dict):
        return False
    contract = item.get('contract', {})
    if not isinstance(contract, dict):
        return False
    if (not isinstance(contract.get('name'), str) or not re.fullmatch('[a-z][a-z0-9_]{2,63}', contract['name'])
            or not isinstance(contract.get('description'), str) or len(contract['description']) > 1000
            or not isinstance(contract.get('input_schema'), dict)):
        return False
    return (item.get('state') == 'ACTIVE' and item.get('validated') == 1
            and isinstance(item.get('hash'), str) and re.fullmatch('[a-f0-9]{64}', item['hash']) is not None
            and contract.get('kind') in {'python', 'bash'} and contract.get('capability') == 'srt.readonly.inspect'
            and isinstance(contract.get('permissions'), list)
            and all(isinstance(p, str) for p in contract['permissions'])
            and set(contract['permissions']) <= {'input.read', 'workspace.compute', 'workspace.output'})


def registry_tools():
    from .adapters import registry_request
    try:
        capabilities = registry_request('capabilities')
        if not isinstance(capabilities, dict) or capabilities.get('expected_hash') is not True:
            return []
        rows = registry_request('list')
        return rows if isinstance(rows, list) else []
    except (OSError, ValueError):
        return []


class Catalog:
    def __init__(self, provider=builtin_contracts, registry_provider=None):
        self.provider = provider
        self.registry_provider = registry_provider or (registry_tools if provider is builtin_contracts else lambda: [])

    def legacy(self):
        return self.provider()

    def list(self, search='', *, planner=False):
        tools = list(self.legacy().values())
        for item in self.registry_provider()[:50]:
            if approved_generated(item):
                contract = item['contract']
                tools.append({'name': 'registered.' + contract['name'], 'description': contract['description'],
                              'effect': 'compute', 'parameters': contract['input_schema'],
                              'package_hash': item['hash']})
        tools += [
            spec('spotify.timer.status', '음악 정지 예약 상태를 읽는다.', 'read'),
            spec('spotify.timer.cancel', '사용자가 요청한 음악 정지 예약을 취소한다.', 'control'),
            spec('spotify.timer.schedule', '검증된 현실 시각에 음악을 멈추도록 예약한다.', 'control',
                 due={'type': 'number', 'minimum': 0}, play={'type': 'boolean'}),
            spec('registry.search', '검증 영역에 이미 등록된 재사용 도구를 검색한다. 제작하지 않는다.', 'read', query=TEXT),
            spec('registry.describe', '등록 도구의 검증 범위와 규격을 확인한다.', 'read', name=TEXT),
            spec('core.status', '현재 리오 실행 코어의 기능과 상태를 읽는다.', 'read'),
        ]
        # Unsupported legacy command families remain explicit-only and make no
        # structured-success claims. They cannot enter a model-generated plan.
        if not planner:
            tools += [spec('pc.wake', '기존 보호 절차로 PC 켜기. 사용자 명시 명령만.', 'control'),
                      spec('pc.shutdown', '기존 보호 절차로 PC 종료. 사용자 명시 명령만.', 'control')]
            tools += [spec('legacy.' + family, '기존 명시적 명령 호환 경로.', 'control', command={'type': 'string', 'maxLength': 2000})
                      for family in ('home', 'pc', 'dubbing')]
        if planner:
            tools = [t for t in tools if not t['name'].startswith('spotify.timer.') or t['effect'] == 'read']
        result = []
        for declaration in tools:
            tool = copy.deepcopy(declaration)
            tool['version'] = '1.0.0'
            tool['permissions'] = ['workspace.compute' if tool['effect'] == 'compute' else 'device.control' if tool['effect'] == 'control' else
                                   'browser.navigate' if tool['effect'] == 'navigate' else 'external.read']
            if tool['name'].startswith('home.'):
                from core.home.smartthings_pc import read_private, PcError
                defaults = {'RIO_HOME_TARGET_FILE': 'google-home-lights.json',
                            'RIO_HOME_POLICY_FILE': 'google-home-policy.json',
                            'RIO_HOME_DEVICE_TARGET_FILE': 'google-home-devices.json'}
                revisions = {}
                for variable, filename in defaults.items():
                    path = Path(os.getenv(variable, str(Path.home()/'.config/rio'/filename)))
                    try:
                        revisions[variable] = fingerprint(read_private(path)) if path.exists() else None
                    except (OSError, ValueError, PcError):
                        revisions[variable] = 'UNREADABLE'
                revisions['execution_enabled'] = os.getenv('RIO_HOME_EXECUTION_ENABLED')
                tool['binding_revision'] = fingerprint(revisions)
            tool['revision'] = fingerprint(tool)
            if search.casefold() in (tool['name'] + ' ' + tool['description']).casefold():
                result.append(tool)
        return result

    def describe(self, name):
        return next((t for t in self.list() if t['name'] == name), None)

    def check(self, name, arguments, revision=None):
        tool = self.describe(name)
        if tool is None or not validate(tool['parameters'], arguments):
            raise ValueError('TOOL_OR_ARGUMENTS_CHANGED')
        if revision is not None and revision != tool['revision']:
            raise ValueError('TOOL_REVISION_CHANGED')
        return tool

    @staticmethod
    def effect(tool, args):
        if tool['name'] == 'spotify.control' and args.get('action') == 'status':
            return 'read'
        return tool['effect']

    def resolve(self, text, browser):
        """Parse without executing. Natural and explicit commands share descriptors."""
        from core.spotify import parse_spotify_request
        from core.spotify_timer import parse_timer_request, TimerParseError
        from core.home.device_controls import parse_device_request
        from core.home.tools import parse_light_request
        from core.home.smartthings_pc import parse_pc_request
        def chosen(name, **arguments):
            return {'kind': 'tool', 'name': name, 'arguments': arguments, 'explicit': True}
        clean = text.strip()
        if clean in {'/core status', '/status core'}:
            return chosen('core.status')
        pc = parse_pc_request(clean)
        if pc in {'on', 'off', 'status'}:
            return chosen({'on': 'pc.wake', 'off': 'pc.shutdown', 'status': 'pc.status'}[pc])
        if clean.startswith('/search '):
            return chosen('web.search', query=clean[8:].strip())
        if clean.startswith('/tools search '):
            return chosen('registry.search', query=clean[14:].strip())
        if clean.startswith('/tools describe '):
            return chosen('registry.describe', name=clean[16:].strip())
        if clean.startswith('/tools call '):
            import json
            try:
                name, arguments = clean[12:].split(None, 1)
                return {'kind': 'tool', 'name': 'registered.' + name, 'arguments': json.loads(arguments), 'explicit': True}
            except (ValueError, TypeError):
                return {'kind': 'clarify', 'question': '기존 도구 호출은 /tools call 도구이름 JSON 형식으로 알려줘.'}
        if clean.startswith('/tools'):
            return {'kind': 'clarify', 'question': '기존 도구는 /tools search 검색어로 찾을 수 있어. 새 도구 제작은 기능과 검증 방법을 먼저 정리한 뒤 별도 요청으로 시작할게.'}
        # Timers retain their existing clock/ambiguity policy and worker guard.
        try:
            timer = parse_timer_request(clean)
        except TimerParseError:
            return {'kind': 'clarify', 'question': '예약 시각을 확정하지 못했어. 24시간 시각이나 몇 분 뒤인지 알려줘.'}
        if timer:
            if timer['kind'] in {'status', 'cancel'}:
                return chosen('spotify.timer.' + timer['kind'])
            return chosen('spotify.timer.schedule', due=timer['due'], play=timer['play'])
        action = parse_spotify_request(clean)
        if action:
            return chosen('spotify.control', action=action)
        device = parse_device_request(clean)
        if device:
            return chosen('home.device.' + device[0], action=device[1])
        light = parse_light_request(clean)
        direct = re.fullmatch(r'/home (\S+) (on|off)', clean)
        if light:
            return chosen('home.light', target=light['target'], action='on' if light['on'] else 'off')
        if direct:
            return chosen('home.light', target=direct[1], action=direct[2])
        if clean in {'/home', '/home status', '조명 상태', '전등 상태'}:
            return chosen('home.status')
        if clean == '/pc status':
            return chosen('pc.status')
        if clean in {'/dubbing health', '/dubbing status'}:
            return chosen('dubbing.health')
        if clean.startswith(('/home ', '/pc ', '/dubbing ')):
            return chosen('legacy.' + clean.split()[0][1:], command=clean)
        # BrowserConversation's parser can be reused with inert callbacks. No UI
        # or network is touched here, even for single-sentence topic opening.
        if not clean.startswith('/') and re.search(r'그리고|찾고|확인하고|한\s*뒤|고\s*나서', clean):
            return None
        import copy as _copy
        probe = _copy.copy(browser)
        captured = []
        def capture(command):
            parts = command.removeprefix('/browse ').split(None, 1)
            op, tail = parts[0], parts[1] if len(parts) > 1 else ''
            if op == 'search':
                captured.append(chosen('browser.search', query=tail))
            elif op == 'open' and tail.isdigit():
                captured.append(chosen('browser.open_result', index=int(tail)))
            elif op in {'back', 'status'}:
                captured.append(chosen('browser.' + {'back': 'back_results', 'status': 'status'}[op]))
            return ''
        probe._run = capture
        probe.open_topic = lambda query: captured.append(chosen('browser.open_topic', query=query)) or ''
        reply = probe.handle(clean)
        if captured:
            return captured[0]
        if reply:
            return {'kind': 'clarify', 'question': reply}
        return None

    def compat_execute(self, decision, browser, handlers, contracts=None):
        """Compatibility only for the old runtime; new core uses ToolRunner."""
        name, args = decision['name'], decision['arguments']
        current = contracts if contracts is not None else self.legacy()
        if name not in current or not validate(current[name]['parameters'], args):
            return '도구 설정이 달라져서 실행하지 않았어. 다시 요청해 줘.'
        callbacks = {
            'spotify.control': lambda: handlers['spotify']('/spotify ' + args['action']),
            'home.light': lambda: handlers['home']('/home ' + args['target'] + ' ' + args['action']),
            'home.status': lambda: handlers['home']('/home status'),
            'pc.status': lambda: handlers['pc']('/pc status'),
            'dubbing.health': lambda: handlers['dubbing']('/dubbing health'),
        }
        if name.startswith('home.device.'):
            return handlers['home']('/home ' + name.removeprefix('home.device.') + ' ' + args['action'])
        if name.startswith('browser.'):
            if name == 'browser.open_topic':
                return browser.open_topic(args['query'])
            operations = {'browser.search': lambda: 'search ' + args['query'],
                          'browser.open_result': lambda: 'open ' + str(args['index']),
                          'browser.back_results': lambda: 'back', 'browser.status': lambda: 'status'}
            return browser._run(operations[name]())
        if name not in callbacks:
            raise ValueError('Tool requires the existing web-evidence path')
        return callbacks[name]()
