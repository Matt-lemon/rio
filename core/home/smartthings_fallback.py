"""Strict optional Google Home outage failover to pinned SmartThings devices.

No name-based automatic mapping, no fallback after attempted Google Home writes,
no claim of air conditioner temperature/mode support from a switch capability.
"""
from __future__ import annotations

import json
from pathlib import Path
import sqlite3
import uuid

from .smartthings_pc import PcError, read_private
from .smartthings_direct import DirectLight, VerifiedLightCli

LIGHTS = frozenset({'주방', '거실', '세면대', '샤워실'})
DEVICE_FAMILIES = {'에어컨': 'air_conditioner_power', 'lens': 'smart_plug'}
CANDIDATES = LIGHTS | set(DEVICE_FAMILIES)


def load_policy(path=None):
    path = Path(path or Path.home()/'.config/rio/smartthings-fallback.json')
    if not path.exists():
        return {'schema_version':1,'enabled':False,'targets':{}}
    value = json.loads(read_private(path))
    if (not isinstance(value,dict) or set(value) != {'schema_version','enabled','targets'}
            or type(value['schema_version']) is not int or value['schema_version'] != 1
            or type(value['enabled']) is not bool or not isinstance(value['targets'],dict)
            or set(value['targets']) - CANDIDATES):
        raise PcError('FALLBACK_POLICY_INVALID')
    ids=[]
    for target, item in value['targets'].items():
        if (not isinstance(item,dict) or set(item) != {'device_id','label','device_type',
                'confirmed_same_physical_device','allowed_commands'}
                or item['label'] != target or item['device_type'] not in {'ZIGBEE','EDGE_CHILD','MATTER'}
                or type(item['confirmed_same_physical_device']) is not bool
                or type(item['allowed_commands']) is not list or any(type(x) is not str or x not in {'on','off'} for x in item['allowed_commands'])
                or len(set(item['allowed_commands'])) != len(item['allowed_commands'])):
            raise PcError('FALLBACK_POLICY_INVALID')
        try:
            if str(uuid.UUID(item['device_id'])) != item['device_id']:
                raise ValueError()
        except (ValueError,TypeError,AttributeError):
            raise PcError('FALLBACK_POLICY_INVALID') from None
        ids.append(item['device_id'])
    if len(ids)!=len(set(ids)):
        raise PcError('FALLBACK_POLICY_DUPLICATE_TARGET')
    return value


def no_unresolved_primary(path):
    """One global primary ledger is deliberately conservative about unknown writes."""
    path=Path(path)
    if not path.exists():
        return True
    if path.is_symlink() or not path.is_file():
        return False
    try:
        db=sqlite3.connect('file:'+str(path)+'?mode=ro',uri=True,timeout=2)
        try:
            return db.execute("SELECT 1 FROM light_commands WHERE status IN ('DISPATCHING','DISPATCHED','UNKNOWN') LIMIT 1").fetchone() is None
        finally:
            db.close()
    except sqlite3.Error:
        return False


def proven_bridge_outage(transport):
    """Policy/permission lock is not an outage; SDK NOT_FOUND is not an outage."""
    try:
        status=transport.status()
    except (TimeoutError,OSError,ValueError):
        return True
    if not isinstance(status,dict) or status.get('http_status'):
        return False
    if status.get('permission') not in (None,'GRANTED'):
        return False
    if status.get('available') is True and status.get('bridge_state') == 'READY':
        return False
    return (status.get('available') is False and (
        status.get('code') == 'GATEWAY_UNREACHABLE' or
        status.get('bridge_state') in ('DOWN', 'DISCONNECTED', 'UNAVAILABLE')))


def select(target, action, primary_registry):
    """All validations happen before reserve or SmartThings command dispatch."""
    if target not in CANDIDATES or action not in {'on','off'}:
        return None
    policy=load_policy()
    if policy['enabled'] is not True:
        return None
    item=policy['targets'].get(target)
    if not item or item['confirmed_same_physical_device'] is not True or action not in item['allowed_commands']:
        return None
    original=primary_registry.targets.get(target)
    if not isinstance(original,dict):
        return None
    if target in LIGHTS:
        # Secondary light bindings stay independent; same-device consent is
        # represented by this explicit fallback mapping, not guessed by alias.
        gh=original.get('alias')
        if not isinstance(gh,str) or not gh:
            return None
        kinds={'주방':'EDGE_CHILD','거실':'ZIGBEE','세면대':'EDGE_CHILD','샤워실':'ZIGBEE'}
        if item['device_type'] != kinds[target]:
            return None
    else:
        if original.get('family') != DEVICE_FAMILIES[target] or original.get('expected_name') != target:
            return None
        if action not in original.get('allowed_commands',[]):
            return None
        if target=='에어컨' and item['device_type']!='ZIGBEE':
            return None
        if target=='lens' and item['device_type']!='MATTER':
            return None
    return item


def attempt(target, action, request_key, primary_registry, progress=None):
    """Only call before any Google Home dispatch, on a confirmed bridge outage."""
    item=select(target,action,primary_registry)
    if item is None:
        return {'result':'FALLBACK_NOT_VERIFIED','commands_sent':0}
    if not no_unresolved_primary(primary_registry.ledger_path):
        return {'result':'PRIMARY_UNRESOLVED','commands_sent':0}
    if not proven_bridge_outage(primary_registry.transport):
        return {'result':'BRIDGE_OUTAGE_NOT_PROVEN','commands_sent':0}
    profile = json.loads(read_private(Path.home()/'.config/rio/pc-auth-provider.json'))
    if (not isinstance(profile,dict) or set(profile) != {'schema_version','provider','cli_root','cli_sha256'}
            or type(profile['schema_version']) is not int or profile['schema_version'] != 1
            or profile['provider'] != 'oauth-cli'):
        raise PcError('FALLBACK_CLI_CONFIG_INVALID')
    binding={'device_id':item['device_id'],'label':item['label'],'device_type':item['device_type'],'component':'main'}
    transport=VerifiedLightCli(binding,root=Path(profile['cli_root']),
                               expected_sha256=profile['cli_sha256'],control_enabled=True)
    direct=DirectLight(target,binding,transport,
        Path.home()/'.local/state/rio/smartthings-direct/commands.sqlite3',True)
    result=direct.set_power(action=='on',request_key=request_key,
                            before_dispatch=(lambda:progress.attempted('FALLBACK_CLI_DISPATCH')) if progress else None)
    return {**result,'fallback':True,'primary':'GOOGLE_HOME_UNAVAILABLE',
            'mode':'switch_only' if target=='에어컨' else 'on_off'}


def lookup_existing(target, request_key):
    """Read only a pinned prior SmartThings command, never repeat it.

    This is available even when future fallback permissions have been revoked.
    It does not infer a relationship from matching device names.
    """
    if target not in CANDIDATES:
        return None
    policy = load_policy()
    item = policy['targets'].get(target)
    if not item:
        return None
    ledger_path = Path.home()/'.local/state/rio/smartthings-direct/commands.sqlite3'
    if not ledger_path.is_file() or ledger_path.is_symlink():
        return None
    db = sqlite3.connect('file:'+str(ledger_path)+'?mode=ro',uri=True,timeout=2)
    try:
        found=[]
        for table in ('wake_requests','shutdown_requests'):
            row=db.execute('SELECT state FROM '+table+' WHERE id=? AND target=?',
                           (request_key,item['device_id'])).fetchone()
            if row:found.append((table,row[0]))
        if len(found)!=1:
            return None
    finally:
        db.close()
    profile = json.loads(read_private(Path.home()/'.config/rio/pc-auth-provider.json'))
    if (not isinstance(profile,dict) or set(profile) != {'schema_version','provider','cli_root','cli_sha256'}
            or profile['provider'] != 'oauth-cli'):
        raise PcError('FALLBACK_CLI_CONFIG_INVALID')
    binding={'device_id':item['device_id'],'label':item['label'],
             'device_type':item['device_type'],'component':'main'}
    transport=VerifiedLightCli(binding,root=Path(profile['cli_root']),
                               expected_sha256=profile['cli_sha256'],control_enabled=False)
    direct=DirectLight(target,binding,transport,ledger_path,False)
    observed=direct.status()
    outcome=direct.request_status(request_key)
    return {'result':outcome['result'],'reported_switch':observed['switch'],
            'physical_state_confirmed':False,'commands_sent':0,
            'original_fallback':True,'request_key':request_key}
