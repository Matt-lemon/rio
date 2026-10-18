"""Explicit SmartThings CLI control of individually pinned lights.

This route is independent from Google Home and never asserts that a SmartThings
light is the same physical device as any Google Home alias. Control is opt-in,
restricted to operator-approved names, and never retried after uncertainty.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import uuid

from .smartthings_pc import PcError, WakeLedger, read_private
from .smartthings_cli import OfficialCliTransport
from .smartthings_lights import LightTransport

ALLOWED_NAMES = frozenset({'주방', '거실'})


class VerifiedLightCli(OfficialCliTransport):
    verify_target = LightTransport.verify_target


class DirectLight:
    def __init__(self, target, binding, transport, ledger_path, enabled):
        self.target = target
        self.binding = binding
        self.transport = transport
        self.ledger_path = Path(ledger_path)
        self.enabled = enabled is True

    def status(self):
        self.transport.verify_target()
        reported = self.transport.switch()
        return {'target': self.target, 'switch': reported['value'],
                'physical_state_confirmed': False, 'provider': 'smartthings-cli'}

    def set_power(self, on, request_key=None):
        if type(on) is not bool:
            return {'result': 'REJECTED', 'code': 'INVALID_OPERATION', 'commands_sent': 0}
        if not self.enabled or self.transport.control_enabled is not True:
            return {'result': 'REJECTED', 'code': 'DIRECT_CONTROL_DISABLED', 'commands_sent': 0}
        observed = self.status()
        if observed['switch'] == ('on' if on else 'off'):
            return {**observed, 'result': 'ALREADY_REPORTED', 'commands_sent': 0}
        with WakeLedger(self.ledger_path) as ledger:
            key = ledger.reserve(self.binding['device_id'], request_key) if on else ledger.reserve_shutdown(self.binding['device_id'], request_key)
            if key is None:
                return {**observed, 'result': 'PENDING_DO_NOT_RETRY', 'commands_sent': 0}
            # The CLI may have dispatched a command without returning the API's
            # receipt. Store an indeterminate result even when its exit code is 0.
            try:
                self.transport.on() if on else self.transport.off()
            except Exception:
                pass
            ledger.record(key, 'UNKNOWN')
            return {**observed, 'result': 'UNKNOWN', 'commands_sent': 1}


def from_environment(target):
    if target not in ALLOWED_NAMES:
        raise PcError('DIRECT_TARGET_FORBIDDEN')
    config_file = Path(os.getenv('RIO_HOME_SECONDARY_TARGET_FILE',
                                  str(Path.home()/'.config/rio/smartthings-lights.json')))
    policy_file = Path.home()/'.config/rio/smartthings-direct-policy.json'
    selection_file = Path.home()/'.config/rio/pc-auth-provider.json'
    config = json.loads(read_private(config_file))
    if not isinstance(config, dict) or set(config) != {'schema_version', 'targets'} or type(config['schema_version']) is not int or config['schema_version'] != 1:
        raise PcError('DIRECT_CONFIG_INVALID')
    binding = config['targets'].get(target)
    if (not isinstance(binding, dict) or set(binding) != {'device_id','label','component',
            'device_type','confirmed_same_physical_light'} or binding['label'] != target
            or binding['component'] != 'main' or binding['device_type'] not in {'ZIGBEE','EDGE_CHILD'}):
        raise PcError('DIRECT_TARGET_CHANGED')
    binding = dict(binding)
    try:
        binding['device_id'] = str(uuid.UUID(binding['device_id']))
    except (ValueError, TypeError):
        raise PcError('DIRECT_TARGET_CHANGED') from None
    policy = json.loads(read_private(policy_file))
    if (not isinstance(policy, dict) or set(policy) != {'schema_version','enabled','allowed_targets'}
            or type(policy['schema_version']) is not int or policy['schema_version'] != 1
            or type(policy['enabled']) is not bool or type(policy['allowed_targets']) is not list
            or any(type(x) is not str or x not in ALLOWED_NAMES for x in policy['allowed_targets'])
            or len(set(policy['allowed_targets'])) != len(policy['allowed_targets'])):
        raise PcError('DIRECT_POLICY_INVALID')
    selection = json.loads(read_private(selection_file))
    if (not isinstance(selection, dict) or set(selection) != {'schema_version','provider','cli_root','cli_sha256'}
            or type(selection['schema_version']) is not int or selection['schema_version'] != 1
            or selection['provider'] != 'oauth-cli'):
        raise PcError('DIRECT_CLI_CONFIG_INVALID')
    enabled = policy['enabled'] and target in policy['allowed_targets']
    transport = VerifiedLightCli(binding, root=Path(selection['cli_root']),
                                 expected_sha256=selection['cli_sha256'], control_enabled=enabled)
    return DirectLight(target, binding, transport,
                       Path.home()/'.local/state/rio/smartthings-direct/commands.sqlite3', enabled)


def handle_direct_command(text):
    import re
    if not text.startswith('/home smartthings '):
        return None
    matched = re.fullmatch(r'/home smartthings (\S+) (status|on|off)', text)
    if not matched:
        return 'SmartThings 명령: /home smartthings 주방 status|on|off · /home smartthings 거실 status|on|off'
    name, action = matched.groups()
    try:
        direct = from_environment(name)
        if action == 'status':
            state = direct.status()['switch']
            return 'SmartThings '+name+' 스위치 보고값: '+('켜짐' if state == 'on' else '꺼짐')+'. 실물 상태는 별도 확인이 필요해.'
        result = direct.set_power(action == 'on')
        if result['result'] == 'ALREADY_REPORTED':
            return 'SmartThings '+name+' 스위치가 이미 요청한 상태로 보고돼. 명령을 보내지 않았어.'
        if result['result'] == 'PENDING_DO_NOT_RETRY':
            return 'SmartThings '+name+' 이전 전원 명령 결과가 불확실해. 중복 전송하지 않았어.'
        if result['result'] == 'UNKNOWN':
            return 'SmartThings '+name+' '+('켜기' if action == 'on' else '끄기')+' 요청을 한 번 전송했으나 실제 반영은 미확정이야. 자동 재전송하지 않아.'
        return 'SmartThings '+name+' 제어 정책이 잠겨 있어 명령을 보내지 않았어.'
    except Exception:
        return 'SmartThings 대상·인증·연결 상태를 확인하지 못했어. 다른 경로로 자동 재전송하지 않아.'
