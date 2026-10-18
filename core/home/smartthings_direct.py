"""Explicit SmartThings CLI control of individually pinned lights.

This route is independent from Google Home and never asserts that a SmartThings
light is the same physical device as any Google Home alias. Control is opt-in,
restricted to operator-approved names, and never retried after uncertainty.
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
from datetime import datetime, timezone
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

    @staticmethod
    def _switch_updated_after_request(timestamp, created):
        # SmartThings timestamps, not our observation time, guard against stale
        # cloud-cached states. No timestamp -> no automatic reconciliation.
        if not isinstance(timestamp, str) or len(timestamp) > 80:
            return False
        try:
            parsed = datetime.fromisoformat(timestamp.replace('Z', '+00:00'))
        except ValueError:
            return False
        if parsed.tzinfo is None:
            return False
        changed = parsed.timestamp()
        return created < changed <= time.time() + 60

    def _reconcile_observed(self, switch, timestamp):
        if not self.ledger_path.is_file():
            return None
        with WakeLedger(self.ledger_path) as ledger:
            db = ledger.db
            db.execute('BEGIN IMMEDIATE')
            try:
                matching = []
                for table, expected in (('wake_requests', 'on'), ('shutdown_requests', 'off')):
                    rows = db.execute('SELECT id, state, created FROM ' + table +
                        " WHERE target=? AND state IN ('DISPATCHING','ACCEPTED','UNKNOWN')",
                        (self.binding['device_id'],)).fetchall()
                    for ident, state, created in rows:
                        matching.append((table, expected, ident, state, created))
                # Never resolve an in-flight request or a conflicting ledger.
                if (len(matching) != 1 or matching[0][3] != 'UNKNOWN'
                        or matching[0][1] != switch
                        or not self._switch_updated_after_request(timestamp, matching[0][4])):
                    db.commit()
                    return None
                table, _, ident, prior, _ = matching[0]
                db.execute('CREATE TABLE IF NOT EXISTS smartthings_observations '
                    '(request_id TEXT PRIMARY KEY, target TEXT NOT NULL, original_state TEXT NOT NULL, '
                    'reported_state TEXT NOT NULL, device_timestamp TEXT NOT NULL, observed_at REAL NOT NULL)')
                db.execute('INSERT INTO smartthings_observations VALUES (?,?,?,?,?,?)',
                    (ident, self.binding['device_id'], prior, switch, timestamp, time.time()))
                state = 'OBSERVED_SWITCH_ON' if switch == 'on' else 'OBSERVED_SWITCH_OFF'
                updated = db.execute('UPDATE ' + table + ' SET state=? WHERE id=? AND target=? AND state=?',
                    (state, ident, self.binding['device_id'], prior))
                if updated.rowcount != 1:
                    raise RuntimeError('LEDGER_CHANGED')
                db.commit()
                return ident
            except Exception:
                db.rollback()
                raise

    def status(self):
        self.transport.verify_target()
        reported = self.transport.switch()
        reconciled = self._reconcile_observed(reported['value'], reported.get('timestamp'))
        return {'target': self.target, 'switch': reported['value'],
                'switch_timestamp': reported.get('timestamp'),
                'physical_state_confirmed': False, 'provider': 'smartthings-cli',
                'pending_resolved_by_fresh_status': reconciled is not None,
                'reconciled_request_id': reconciled}

    def request_status(self, key):
        # Read-only lookup; notably never reissues a CLI command.
        if not self.ledger_path.is_file():
            return {'result': 'UNKNOWN', 'commands_sent': 0}
        with WakeLedger(self.ledger_path) as ledger:
            for table, state in (('wake_requests', 'OBSERVED_SWITCH_ON'),
                                 ('shutdown_requests', 'OBSERVED_SWITCH_OFF')):
                row = ledger.db.execute('SELECT state FROM ' + table +
                        ' WHERE id=? AND target=?', (key, self.binding['device_id'])).fetchone()
                if row:
                    return {'result': 'OBSERVED_SWITCH' if row[0] == state else 'UNKNOWN',
                            'reported': 'on' if table == 'wake_requests' else 'off',
                            'commands_sent': 0, 'physical_state_confirmed': False}
        return {'result': 'UNKNOWN', 'commands_sent': 0}

    def set_power(self, on, request_key=None, before_dispatch=None):
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
            if before_dispatch is not None:
                before_dispatch()
            # The official CLI hides the API's typed receipt. Never equate
            # process exit 0 with ACCEPTED or physical operation.
            cli_completed = False
            try:
                self.transport.on() if on else self.transport.off()
                cli_completed = True
            except Exception:
                pass
            ledger.record(key, 'UNKNOWN')
        # Exactly one bounded read-only status verification, no further writes.
        try:
            after = self.status()
        except Exception:
            after = None
        if after is not None and after.get('reconciled_request_id') == key:
            return {**after, 'result': 'OBSERVED_SWITCH', 'commands_sent': 1,
                    'cli_process_completed': cli_completed, 'api_acceptance_verified': False,
                    'post_command_status_checked': True,
                    'post_command_switch_report': after['switch'],
                    'post_command_switch_matches_request': True,
                    'post_command_change_time_verified': True}
        return {**observed, 'result': 'UNKNOWN', 'commands_sent': 1,
                'cli_process_completed': cli_completed, 'api_acceptance_verified': False,
                'post_command_status_checked': after is not None,
                'post_command_switch_report': after['switch'] if after is not None else None,
                'post_command_switch_matches_request': (
                    after['switch'] == ('on' if on else 'off')) if after is not None else None,
                'post_command_change_time_verified': False,
                'code': 'WAITING_FOR_FRESH_DEVICE_REPORT' if cli_completed else 'CLI_OUTCOME_UNCERTAIN'}


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
        if result['result'] == 'OBSERVED_SWITCH':
            return 'SmartThings '+name+'가 요청한 스위치 상태를 새 시각으로 보고했어. 실제 전등은 별도 확인 필요.'
        if result['result'] == 'UNKNOWN':
            if result.get('post_command_status_checked') is True:
                reported = result.get('post_command_switch_report')
                reported_text = '켜짐' if reported == 'on' else '꺼짐' if reported == 'off' else '미확인'
                return 'SmartThings '+name+' 명령 후 상태 1회 조회: '+reported_text+'. 변경 시각으로 반영을 입증하지 못해 미확정으로 남겼어. 자동 재전송하지 않아.'
            return 'SmartThings '+name+' 요청을 한 번 전송했지만 명령 후 상태 조회도 확인하지 못했어. 자동 재전송하지 않아.'
        return 'SmartThings '+name+' 제어 정책이 잠겨 있어 명령을 보내지 않았어.'
    except Exception:
        return 'SmartThings 대상·인증·연결 상태를 확인하지 못했어. 다른 경로로 자동 재전송하지 않아.'
