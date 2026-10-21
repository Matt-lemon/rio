"""Strict optional Google Home outage failover to pinned SmartThings devices.

No name-based automatic mapping, no fallback after attempted Google Home writes,
no claim of air conditioner temperature/mode support from a switch capability.
"""
from __future__ import annotations

import json
from pathlib import Path
import sqlite3
import uuid
import re
import hashlib
from datetime import datetime, timezone

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
            or type(value['schema_version']) is not int or value['schema_version'] not in {1,2}
            or type(value['enabled']) is not bool or not isinstance(value['targets'],dict)
            or set(value['targets']) - CANDIDATES):
        raise PcError('FALLBACK_POLICY_INVALID')
    ids=[]
    aliases=[]
    for target, item in value['targets'].items():
        fields={'device_id','label','device_type','confirmed_same_physical_device','allowed_commands'}
        if value['schema_version']==2:fields|={'primary_alias','identity_evidence'}
        if (not isinstance(item,dict) or set(item) != fields
                or item['label'] != target or item['device_type'] not in {'ZIGBEE','EDGE_CHILD','MATTER'}
                or type(item['confirmed_same_physical_device']) is not bool
                or type(item['allowed_commands']) is not list or any(type(x) is not str or x not in {'on','off'} for x in item['allowed_commands'])
                or len(set(item['allowed_commands'])) != len(item['allowed_commands'])):
            raise PcError('FALLBACK_POLICY_INVALID')
        if value['schema_version']==2:
            aliases.append(item['primary_alias'])
            evidence=item['identity_evidence']
            if (not isinstance(item['primary_alias'],str) or not re.fullmatch(r'dev_[0-9a-f]{24}',item['primary_alias'])
                    or not isinstance(evidence,dict) or set(evidence)!={'method','reference_sha256','confirmed_at'}
                    or evidence['method'] not in {'operator_physical_verification','manufacturer_device_identity'}
                    or not isinstance(evidence['reference_sha256'],str)
                    or not re.fullmatch(r'[a-f0-9]{64}',evidence['reference_sha256'])):
                raise PcError('FALLBACK_IDENTITY_EVIDENCE_REQUIRED')
            try:
                confirmed=datetime.fromisoformat(evidence['confirmed_at'].replace('Z','+00:00'))
                if confirmed.tzinfo is None or confirmed>datetime.now(timezone.utc):raise ValueError()
            except (ValueError,TypeError,AttributeError):
                raise PcError('FALLBACK_IDENTITY_EVIDENCE_REQUIRED') from None
        try:
            if str(uuid.UUID(item['device_id'])) != item['device_id']:
                raise ValueError()
        except (ValueError,TypeError,AttributeError):
            raise PcError('FALLBACK_POLICY_INVALID') from None
        ids.append(item['device_id'])
    if len(ids)!=len(set(ids)):
        raise PcError('FALLBACK_POLICY_DUPLICATE_TARGET')
    if len(aliases)!=len(set(aliases)):
        raise PcError('FALLBACK_POLICY_DUPLICATE_PRIMARY')
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
        return False
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
    if policy['enabled'] is not True or policy['schema_version']!=2:
        return None
    configuration=getattr(primary_registry,'configuration',None)
    enabled=(configuration.get('enabled') is True if isinstance(configuration,dict)
             else getattr(primary_registry,'allow_execution',None) is True)
    if not enabled:
        return None
    validate=getattr(primary_registry,'revalidate',None)
    if validate is not None and (not callable(validate) or validate() is not True):
        return None
    item=policy['targets'].get(target)
    if not item or item['confirmed_same_physical_device'] is not True or action not in item['allowed_commands']:
        return None
    if not identity_evidence_matches(target,item):
        return None
    original=primary_registry.targets.get(target)
    if not isinstance(original,dict) or original.get('alias')!=item['primary_alias']:
        return None
    if target in LIGHTS:
        # Secondary light bindings stay independent; same-device consent is
        # represented by this explicit fallback mapping, not guessed by alias.
        gh=original.get('alias')
        if not isinstance(gh,str) or not gh:
            return None
        if original.get('confirmed_physical_light') is not True:
            return None
        kinds={'주방':'EDGE_CHILD','거실':'ZIGBEE','세면대':'EDGE_CHILD','샤워실':'ZIGBEE'}
        if item['device_type'] != kinds[target]:
            return None
    else:
        if original.get('family') != DEVICE_FAMILIES[target] or original.get('expected_name') != target:
            return None
        if action not in original.get('allowed_commands',[]):
            return None
        if original.get('device_type') not in {'OnOffPluginUnitDevice','RoomAirConditionerDevice'}:
            return None
        if target=='에어컨' and item['device_type']!='ZIGBEE':
            return None
        if target=='lens' and item['device_type']!='MATTER':
            return None
    return item


def identity_evidence_matches(target,item):
    """Require a separate private identity record, not a name/boolean or fake hash.

The operator attests physical identity; software verifies the record's integrity
and exact two-provider bindings, not the truth of a physical observation.
"""
    evidence=item['identity_evidence']
    path=Path.home()/'.config/rio/fallback-identity-evidence'/(evidence['reference_sha256']+'.json')
    try:
        text=read_private(path)
        if hashlib.sha256(text.encode('utf-8')).hexdigest()!=evidence['reference_sha256']:
            return False
        record=json.loads(text)
        return (isinstance(record,dict) and set(record)=={'schema_version','target','primary_alias',
            'smartthings_device_id','method','confirmed_at','verification_basis'}
            and type(record['schema_version']) is int and record['schema_version']==1
            and record['target']==target and record['primary_alias']==item['primary_alias']
            and record['smartthings_device_id']==item['device_id']
            and record['method']==evidence['method'] and record['confirmed_at']==evidence['confirmed_at']
            and record['verification_basis']=={'operator_physical_verification':'operator_observed_same_device',
                'manufacturer_device_identity':'manufacturer_identifier_match'}[evidence['method']])
    except (OSError,PcError,ValueError,KeyError,TypeError):
        return False


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
        Path.home()/'.local/state/rio/smartthings-direct/commands.sqlite3',True,
        primary_ledger_path=primary_registry.ledger_path,
        revalidate=lambda: select(target,action,primary_registry)==item
            and json.loads(read_private(Path.home()/'.config/rio/pc-auth-provider.json'))==profile
            and proven_bridge_outage(primary_registry.transport))
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
    ledger_path = Path.home()/'.local/state/rio/smartthings-direct/commands.sqlite3'
    if not ledger_path.is_file() or ledger_path.is_symlink():
        return None
    db = sqlite3.connect('file:'+str(ledger_path)+'?mode=ro',uri=True,timeout=2)
    try:
        tables={r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if 'smartthings_requests' not in tables:
            return None  # Legacy missing provenance: never invent a mapping.
        saved=db.execute('SELECT binding FROM smartthings_requests WHERE request_id=? AND label=?',
                         (request_key,target)).fetchone()
        if saved is None:return None
        item=json.loads(saved[0])
        if (not isinstance(item,dict) or set(item)!={'device_id','label','device_type','component'}
                or item['label']!=target or item['component']!='main'
                or item['device_type'] not in {'ZIGBEE','EDGE_CHILD','MATTER'}):
            return None
        if str(uuid.UUID(item['device_id']))!=item['device_id']:return None
        found=[]
        for table in ('wake_requests','shutdown_requests'):
            row=db.execute('SELECT state FROM '+table+' WHERE id=? AND target=?',
                           (request_key,item['device_id'])).fetchone()
            if row:found.append((table,row[0]))
        if len(found)!=1:
            return None
    finally:
        db.close()
    binding={'device_id':item['device_id'],'label':item['label'],
             'device_type':item['device_type'],'component':'main'}
    direct=DirectLight(target,binding,None,ledger_path,False)
    outcome=direct.request_status(request_key)
    return {**outcome,'result':outcome['result'],'reported_switch':outcome.get('reported'),
            'physical_state_confirmed':False,'commands_sent':0,
            'original_fallback':True,'request_key':request_key}
