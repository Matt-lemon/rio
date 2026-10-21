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
from .dispatch_guard import DispatchGuard, DispatchBlocked, primary_path

ALLOWED_NAMES = frozenset({'주방', '거실'})


class VerifiedLightCli(OfficialCliTransport):
    verify_target = LightTransport.verify_target


class DirectLight:
    def __init__(self, target, binding, transport, ledger_path, enabled, *, primary_ledger_path=None, revalidate=None):
        self.target = target
        self.binding = dict(binding)
        self.transport = transport
        self.ledger_path = Path(ledger_path)
        self.enabled = enabled is True
        # Omitted arguments must still join the shared home fence. Deriving a
        # private primary path from this provider's DB silently splits writers.
        self.primary_ledger_path = Path(primary_ledger_path) if primary_ledger_path is not None else primary_path()
        self.revalidate = revalidate

    @staticmethod
    def _switch_updated_after_request(timestamp, created):
        # SmartThings timestamps, not our observation time, guard against stale
        # cloud-cached states. No timestamp -> no automatic reconciliation.
        if not isinstance(timestamp, str) or len(timestamp) > 80:
            return False
        try:
            parsed = datetime.fromisoformat(timestamp.replace('Z', '+00:00'))
        except (ValueError,OverflowError,OSError):
            return False
        if parsed.tzinfo is None:
            return False
        changed = parsed.timestamp()
        return created < changed <= time.time()

    @staticmethod
    def _after_previous_report(timestamp, previous):
        if previous is None:return True
        try:
            before=datetime.fromisoformat(previous.replace('Z','+00:00'))
            after=datetime.fromisoformat(timestamp.replace('Z','+00:00'))
            return before.tzinfo is not None and after>before
        except (ValueError,TypeError,AttributeError):
            return False

    def _reconcile_observed(self, switch, timestamp):
        if not self.ledger_path.is_file():
            return None
        with WakeLedger(self.ledger_path) as ledger:
            db = ledger.db
            db.execute('BEGIN IMMEDIATE')
            try:
                db.execute('CREATE TABLE IF NOT EXISTS smartthings_attempts '
                    '(request_id TEXT PRIMARY KEY, target TEXT NOT NULL, action TEXT NOT NULL, '
                    'attempted_at REAL NOT NULL, prior_device_timestamp TEXT)')
                db.execute('CREATE TABLE IF NOT EXISTS smartthings_observations '
                    '(request_id TEXT PRIMARY KEY, target TEXT NOT NULL, original_state TEXT NOT NULL, '
                    'reported_state TEXT NOT NULL, device_timestamp TEXT NOT NULL, observed_at REAL NOT NULL)')
                matching = []
                for table, expected in (('wake_requests', 'on'), ('shutdown_requests', 'off')):
                    rows = db.execute('SELECT id, state, created FROM ' + table +
                        " WHERE target=? AND state IN ('DISPATCHING','ACCEPTED','UNKNOWN')",
                        (self.binding['device_id'],)).fetchall()
                    for ident, state, created in rows:
                        if db.execute('SELECT 1 FROM smartthings_observations o JOIN smartthings_attempts a '
                                'ON o.request_id=a.request_id AND o.target=a.target WHERE o.request_id=?', (ident,)).fetchone():
                            continue
                        attempt = db.execute('SELECT attempted_at,prior_device_timestamp FROM smartthings_attempts '
                            'WHERE request_id=? AND target=? AND action=?', (ident,self.binding['device_id'],expected)).fetchone()
                        matching.append((table, expected, ident, state, created, attempt))
                # Never resolve an in-flight request or a conflicting ledger.
                if (len(matching) != 1 or matching[0][3] != 'UNKNOWN'
                        or matching[0][1] != switch
                        or matching[0][5] is None
                        or not self._switch_updated_after_request(timestamp, matching[0][5][0])
                        or not self._after_previous_report(timestamp,matching[0][5][1])):
                    db.commit()
                    return None
                table, _, ident, prior, _, _ = matching[0]
                db.execute('INSERT OR IGNORE INTO smartthings_observations VALUES (?,?,?,?,?,?)',
                    (ident, self.binding['device_id'], prior, switch, timestamp, time.time()))
                # Original UNKNOWN is immutable. Observations are separate evidence,
                # never API acceptance or proof of physical/causal operation.
                db.commit()
                return ident
            except Exception:
                db.rollback()
                raise

    def status(self):
        self.transport.verify_target()
        reported = self.transport.switch()
        return self._observe_report(reported)

    def _observe_report(self, reported):
        reconciled = self._reconcile_observed(reported['value'], reported.get('timestamp'))
        return {'target': self.target, 'switch': reported['value'],
                'switch_timestamp': reported.get('timestamp'),
                'physical_state_confirmed': False, 'causality_verified': False, 'provider': 'smartthings',
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
                    tables = {r[0] for r in ledger.db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                    observed = None
                    if {'smartthings_observations','smartthings_attempts'} <= tables:
                        observed = ledger.db.execute('SELECT 1 FROM smartthings_observations o JOIN smartthings_attempts a '
                            'ON a.request_id=o.request_id AND a.target=o.target WHERE o.request_id=? AND o.target=? '
                            'AND o.reported_state=a.action AND o.original_state=\'UNKNOWN\'', (key,self.binding['device_id'])).fetchone()
                    receipt=None
                    if 'smartthings_receipts' in tables:
                        receipt=ledger.db.execute('SELECT cli_completed,api_accepted FROM smartthings_receipts WHERE request_id=?',(key,)).fetchone()
                    expected='on' if table=='wake_requests' else 'off'
                    cached={}
                    if 'smartthings_postchecks' in tables:
                        entry=ledger.db.execute('SELECT answer FROM smartthings_postchecks WHERE request_id=?',(key,)).fetchone()
                        if entry and entry[0]:cached=json.loads(entry[0])
                    return {**cached,'result': 'OBSERVED_SWITCH' if observed else 'UNKNOWN',
                            'requested_switch':expected,'reported':expected if observed else None,
                            'api_acceptance_verified':bool(receipt and receipt[1]),
                            'cli_process_completed':bool(receipt and receipt[0]),
                            'commands_sent': 0, 'physical_state_confirmed': False}
        return {'result': 'UNKNOWN', 'commands_sent': 0}

    def set_power(self, on, request_key=None, before_dispatch=None):
        if type(on) is not bool:
            return {'result': 'REJECTED', 'code': 'INVALID_OPERATION', 'commands_sent': 0}
        if not self.enabled or self.transport.control_enabled is not True:
            return {'result': 'REJECTED', 'code': 'DIRECT_CONTROL_DISABLED', 'commands_sent': 0}
        key = request_key or uuid.uuid4().hex
        action = 'on' if on else 'off'
        payload = {'target': self.binding['device_id'], 'action': action}
        sent = False
        try:
            with DispatchGuard(self.primary_ledger_path, (self.ledger_path,)) as fence:
                prior = fence.admit(key, 'smartthings', payload)
                if prior is not None:
                    return {'result':'PENDING_DO_NOT_RETRY','commands_sent':0,'code':prior.get('code','REQUEST_ALREADY_RECORDED')}
                try:
                    allowed=self.revalidate is None or self.revalidate() is True
                except Exception:
                    allowed=False
                if not allowed:
                    fence.finish({'status':'REJECTED','code':'BINDING_OR_POLICY_CHANGED'},terminal=True)
                    return {'result':'REJECTED','code':'BINDING_OR_POLICY_CHANGED','commands_sent':0}
                try:
                    observed = self.status()
                except Exception:
                    fence.finish({'status':'REJECTED','code':'TARGET_PREFLIGHT_FAILED'},terminal=True)
                    return {'result':'REJECTED','code':'TARGET_PREFLIGHT_FAILED','commands_sent':0}
                if observed['switch'] == action:
                    fence.finish({'status':'OBSERVED','code':'ALREADY_REPORTED'},terminal=True)
                    return {**observed,'result':'ALREADY_REPORTED','commands_sent':0}
                with WakeLedger(self.ledger_path) as ledger:
                    reserved = ledger.reserve(self.binding['device_id'], key,allow_observed=True) if on else ledger.reserve_shutdown(self.binding['device_id'], key,allow_observed=True)
                    if reserved is None:
                        fence.finish({'status':'UNKNOWN','code':'LEGACY_REQUEST_EXISTS'},terminal=False)
                        return {**observed,'result':'PENDING_DO_NOT_RETRY','commands_sent':0}
                    if before_dispatch is not None:
                        before_dispatch()
                    # Recheck identity/policy at the actual boundary, after callbacks.
                    try:
                        self.transport.verify_target()
                        ready=self.transport.control_enabled is True and (self.revalidate is None or self.revalidate() is True)
                    except Exception:
                        ready=False
                    if not ready:
                        ledger.record(key,'REJECTED')
                        fence.finish({'status':'REJECTED','code':'BINDING_OR_POLICY_CHANGED'},terminal=True)
                        return {'result':'REJECTED','code':'BINDING_OR_POLICY_CHANGED','commands_sent':0}
                    fence.attempted()
                    ledger.db.execute('CREATE TABLE IF NOT EXISTS smartthings_attempts '
                        '(request_id TEXT PRIMARY KEY, target TEXT NOT NULL, action TEXT NOT NULL, '
                        'attempted_at REAL NOT NULL, prior_device_timestamp TEXT)')
                    ledger.db.execute('INSERT INTO smartthings_attempts VALUES (?,?,?,?,?)',
                        (key,self.binding['device_id'],action,time.time(),observed.get('switch_timestamp')))
                    ledger.db.execute('CREATE TABLE IF NOT EXISTS smartthings_requests '
                        '(request_id TEXT PRIMARY KEY, label TEXT NOT NULL, binding TEXT NOT NULL)')
                    ledger.db.execute('INSERT INTO smartthings_requests VALUES (?,?,?)',
                        (key,self.target,json.dumps({k:self.binding[k] for k in ('device_id','label','device_type')}
                            | {'component':self.binding.get('component','main')},sort_keys=True)))
                    ledger.record(key,'UNKNOWN')
                    cli_completed = False
                    api_accepted = False
                    try:
                        sent = True
                        receipt=self.transport.on() if on else self.transport.off()
                        cli_completed = True
                        entries=receipt.get('results') if isinstance(receipt,dict) else None
                        api_accepted=(isinstance(entries,list) and len(entries)==1
                            and isinstance(entries[0],dict) and entries[0].get('status')=='ACCEPTED')
                    except Exception:
                        pass
                    try:
                        ledger.db.execute('CREATE TABLE IF NOT EXISTS smartthings_receipts '
                            '(request_id TEXT PRIMARY KEY, cli_completed INTEGER NOT NULL, api_accepted INTEGER NOT NULL, recorded_at REAL NOT NULL)')
                        ledger.db.execute('INSERT INTO smartthings_receipts VALUES (?,?,?,?)',
                            (key,int(cli_completed),int(api_accepted),time.time()))
                        ledger.db.commit()
                    except sqlite3.Error:
                        ledger.db.rollback()
                result = self._post_command(observed, key, action, cli_completed)
                result['api_acceptance_verified']=api_accepted
                fence.finish({'status':'OBSERVED' if result['result']=='OBSERVED_SWITCH' else 'UNKNOWN',
                              'code':result.get('code','SWITCH_REPORT')},terminal=result['result']=='OBSERVED_SWITCH')
                return result
        except (DispatchBlocked,sqlite3.Error,OSError) as error:
            return {'result':'UNKNOWN' if sent else 'PENDING_DO_NOT_RETRY','commands_sent':int(sent),
                    'code':str(error) if isinstance(error,DispatchBlocked) else 'DISPATCH_STORAGE_UNAVAILABLE'}

    def _post_command(self, observed, key, action, cli_completed):
        # Reserve the read budget durably before touching the network. A crash
        # consumes it; result lookup only reads this record and cannot retry it.
        with WakeLedger(self.ledger_path) as ledger:
            ledger.db.execute('CREATE TABLE IF NOT EXISTS smartthings_postchecks '
                '(request_id TEXT PRIMARY KEY, attempted_at REAL NOT NULL, answer TEXT)')
            inserted=ledger.db.execute('INSERT OR IGNORE INTO smartthings_postchecks VALUES (?,?,NULL)',(key,time.time()))
            ledger.db.commit()
            if inserted.rowcount!=1:
                row=ledger.db.execute('SELECT answer FROM smartthings_postchecks WHERE request_id=?',(key,)).fetchone()
                return json.loads(row[0]) if row and row[0] else {
                    **observed,'result':'UNKNOWN','commands_sent':1,'post_command_status_attempts':1,
                    'post_command_status_checked':False,'code':'POSTCHECK_INTERRUPTED_DO_NOT_RETRY'}
        result=self._read_after_command(observed,key,action,cli_completed)
        with WakeLedger(self.ledger_path) as ledger:
            ledger.db.execute('UPDATE smartthings_postchecks SET answer=? WHERE request_id=? AND answer IS NULL',
                              (json.dumps(result),key))
            ledger.db.commit()
        return result

    def _read_after_command(self, observed, key, action, cli_completed):
        # Exactly one bounded read-only status verification, no further writes.
        try:
            # Identity was checked immediately before dispatch. Exactly one
            # status operation here, even if a later metadata read would fail.
            after = self._observe_report(self.transport.switch())
        except Exception:
            after = None
        if after is not None and after.get('reconciled_request_id') == key:
            return {**after, 'result': 'OBSERVED_SWITCH', 'commands_sent': 1,
                    'cli_process_completed': cli_completed, 'api_acceptance_verified': False,
                    'post_command_status_checked': True,
                    'post_command_switch_report': after['switch'],
                    'post_command_switch_matches_request': True,
                    'post_command_status_attempts': 1,
                    'post_command_change_time_verified': True}
        return {**observed, 'result': 'UNKNOWN', 'commands_sent': 1,
                'cli_process_completed': cli_completed, 'api_acceptance_verified': False,
                'post_command_status_checked': after is not None,
                'post_command_switch_report': after['switch'] if after is not None else None,
                'post_command_switch_matches_request': (
                    after['switch'] == action) if after is not None else None,
                'post_command_status_attempts': 1,
                'post_command_change_time_verified': False,
                'code': 'WAITING_FOR_FRESH_DEVICE_REPORT' if cli_completed else 'CLI_OUTCOME_UNCERTAIN'}


def from_environment(target):
    if target not in ALLOWED_NAMES:
        raise PcError('DIRECT_TARGET_FORBIDDEN')
    config_file = Path(os.getenv('RIO_HOME_SECONDARY_TARGET_FILE',
                                  str(Path.home()/'.config/rio/smartthings-lights.json')))
    policy_file = Path.home()/'.config/rio/smartthings-direct-policy.json'
    selection_file = Path.home()/'.config/rio/pc-auth-provider.json'
    config_text=read_private(config_file)
    config = json.loads(config_text)
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
    policy_text=read_private(policy_file)
    policy = json.loads(policy_text)
    if (not isinstance(policy, dict) or set(policy) != {'schema_version','enabled','allowed_targets'}
            or type(policy['schema_version']) is not int or policy['schema_version'] != 1
            or type(policy['enabled']) is not bool or type(policy['allowed_targets']) is not list
            or any(type(x) is not str or x not in ALLOWED_NAMES for x in policy['allowed_targets'])
            or len(set(policy['allowed_targets'])) != len(policy['allowed_targets'])):
        raise PcError('DIRECT_POLICY_INVALID')
    selection_text=read_private(selection_file)
    selection = json.loads(selection_text)
    if (not isinstance(selection, dict) or set(selection) != {'schema_version','provider','cli_root','cli_sha256'}
            or type(selection['schema_version']) is not int or selection['schema_version'] != 1
            or selection['provider'] != 'oauth-cli'):
        raise PcError('DIRECT_CLI_CONFIG_INVALID')
    enabled = policy['enabled'] and target in policy['allowed_targets']
    transport = VerifiedLightCli(binding, root=Path(selection['cli_root']),
                                 expected_sha256=selection['cli_sha256'], control_enabled=enabled)
    snapshots = {config_file:config_text,policy_file:policy_text,selection_file:selection_text}
    def revalidate():
        return all(read_private(p)==value for p,value in snapshots.items())
    return DirectLight(target, binding, transport,
                       Path.home()/'.local/state/rio/smartthings-direct/commands.sqlite3', enabled,
                       primary_ledger_path=primary_path(),revalidate=revalidate)


def handle_direct_command(text, *, request_key=None):
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
        result = direct.set_power(action == 'on',request_key=request_key)
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
