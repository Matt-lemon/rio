"""Fail-closed fallbacks: only outage-before-send and pinned human confirmation."""
from pathlib import Path
from unittest.mock import Mock
import json
import sqlite3
import hashlib

import pytest

from core.home.smartthings_pc import PcError
from core.home import smartthings_fallback as fb

UUID='12345678-1234-4234-8234-123456789abc'

@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setenv('HOME',str(tmp_path))
    config=tmp_path/'.config/rio'
    config.mkdir(parents=True)
    path=config/'smartthings-fallback.json'
    data={'schema_version':2,'enabled':False,'targets':{
        '에어컨':{'device_id':UUID,'label':'에어컨','device_type':'ZIGBEE',
                   'confirmed_same_physical_device':False,'allowed_commands':['on','off']},
        '주방':{'device_id':'12345678-1234-4234-8234-123456789abd',
                 'label':'주방','device_type':'EDGE_CHILD',
                 'confirmed_same_physical_device':False,'allowed_commands':['on','off']}
    }}
    for index,item in enumerate(data['targets'].values(),1):
        item['primary_alias']='dev_'+str(index)*24
        item['identity_evidence']={'method':'operator_physical_verification','reference_sha256':'a'*64,'confirmed_at':'2026-01-01T00:00:00Z'}
    def write():
        for target,item in data['targets'].items():
            if 'identity_evidence' not in item:continue
            evidence=item['identity_evidence']
            record=json.dumps({'schema_version':1,'target':target,'primary_alias':item['primary_alias'],
                'smartthings_device_id':item['device_id'],'method':evidence['method'],
                'confirmed_at':evidence['confirmed_at'],'verification_basis':'operator_observed_same_device'})
            hashed=hashlib.sha256(record.encode()).hexdigest();evidence['reference_sha256']=hashed
            directory=config/'fallback-identity-evidence';directory.mkdir(exist_ok=True)
            proof=directory/(hashed+'.json');proof.write_text(record);proof.chmod(0o600)
        path.write_text(json.dumps(data,ensure_ascii=False));path.chmod(0o600)
    write()
    registry=Mock()
    registry.configuration={'enabled':True}
    registry.revalidate=lambda:True
    registry.targets={'에어컨':{'alias':'dev_'+'1'*24,'device_type':'OnOffPluginUnitDevice','family':'air_conditioner_power','expected_name':'에어컨','allowed_commands':['on','off']},
                      '주방':{'alias':'dev_'+'2'*24,'confirmed_physical_light':True}}
    registry.ledger_path=tmp_path/'private/commands.sqlite3'
    registry.transport.status.return_value={'available':False,'code':'GATEWAY_UNREACHABLE'}
    return data,write,registry


def test_stage_is_disabled_and_default_denies(setup):
    data,write,registry=setup
    assert fb.select('에어컨','on',registry) is None
    data['enabled']=True;write()
    assert fb.select('에어컨','on',registry) is None
    assert fb.attempt('에어컨','on','fresh-request',registry)['result']=='FALLBACK_NOT_VERIFIED'


def test_aircon_allows_only_pinned_switch_after_confirmation(setup):
    data,write,registry=setup
    data['enabled']=True;data['targets']['에어컨']['confirmed_same_physical_device']=True;write()
    assert fb.select('에어컨','on',registry)['device_id']==UUID
    assert fb.select('에어컨','mode',registry) is None
    assert fb.select('TV','on',registry) is None
    data['targets']['에어컨']['device_type']='LAN';write()
    with pytest.raises(PcError,match='FALLBACK_POLICY_INVALID'):
        fb.select('에어컨','on',registry)


def test_bridge_permission_revocation_never_triggers_fallback(setup):
    _,_,registry=setup
    registry.transport.status.return_value={'available':False,'permission':'DENIED','code':'GATEWAY_UNREACHABLE'}
    assert fb.proven_bridge_outage(registry.transport) is False
    registry.transport.status.return_value={'available':True,'permission':'GRANTED','bridge_state':'READY'}
    assert fb.proven_bridge_outage(registry.transport) is False
    registry.transport.status.return_value={'http_status':401,'available':False}
    assert fb.proven_bridge_outage(registry.transport) is False
    registry.transport.status.return_value={'available':False,'code':'GATEWAY_UNREACHABLE'}
    assert fb.proven_bridge_outage(registry.transport) is True


def test_old_primary_unknown_blocks_even_permitted_aircon(setup):
    data,write,registry=setup
    data['enabled']=True;data['targets']['에어컨']['confirmed_same_physical_device']=True;write()
    registry.ledger_path.parent.mkdir()
    db=sqlite3.connect(registry.ledger_path)
    db.execute('CREATE TABLE light_commands (status TEXT)')
    db.execute('INSERT INTO light_commands VALUES (?)',('UNKNOWN',));db.commit();db.close()
    assert fb.attempt('에어컨','off','request-other',registry)['result']=='PRIMARY_UNRESOLVED'


def test_dispatch_at_most_once_after_all_authorizations(setup,monkeypatch):
    data,write,registry=setup
    data['enabled']=True;data['targets']['에어컨']['confirmed_same_physical_device']=True;write()
    fake=Mock()
    fake.set_power.return_value={'result':'UNKNOWN','commands_sent':1}
    monkeypatch.setattr(fb,'DirectLight',lambda *args,**kw:fake)
    monkeypatch.setattr(fb,'VerifiedLightCli',lambda *args,**kw:Mock())
    profile={'schema_version':1,'provider':'oauth-cli','cli_root':'/opt/private','cli_sha256':'a'*64}
    f=Path.home()/'.config/rio/pc-auth-provider.json';f.write_text(json.dumps(profile));f.chmod(0o600)
    result=fb.attempt('에어컨','on','req-12345678',registry)
    assert result['result']=='UNKNOWN' and result['fallback'] is True
    fake.set_power.assert_called_once()
    assert fake.set_power.call_args.kwargs['request_key']=='req-12345678'
    assert result['mode']=='switch_only'


def test_unconfirmed_shower_cannot_be_assumed_same_device(setup):
    data,write,registry=setup
    data['enabled']=True;data['targets']['주방']['confirmed_same_physical_device']=True;write()
    assert fb.select('샤워실','on',registry) is None
    assert fb.select('주방','on',registry) is not None


def test_policy_disallows_duplicate_uuids_and_untrusted_fields(setup):
    data,write,registry=setup
    data['targets']['주방']['device_id']=UUID;write()
    with pytest.raises(PcError):fb.load_policy()
    data['targets']['주방']['device_id']='12345678-1234-4234-8234-123456789abd'
    data['targets']['에어컨']['secret_control']=True;write()
    with pytest.raises(PcError):fb.load_policy()


def test_missing_policy_is_disabled(tmp_path,monkeypatch):
    monkeypatch.setenv('HOME',str(tmp_path))
    assert fb.load_policy()['enabled'] is False


def test_adapter_fallback_only_before_google_light_dispatch(monkeypatch):
    from core.execution.adapters import Adapters
    from core.home import main_adapter
    primary=Mock()
    primary.allow_execution=True
    primary.targets={'주방':{'alias':'dev_'+'1'*24,'confirmed_physical_light':True}}
    primary.plan.return_value={'status':'REJECTED','code':'BRIDGE_NOT_READY'}
    monkeypatch.setattr(main_adapter,'registry_from_environment',lambda:primary)
    called=[]
    def once(*args):
        called.append(args)
        return {'result':'UNKNOWN','commands_sent':1,'fallback':True}
    monkeypatch.setattr(fb,'attempt',once)
    result=Adapters(None).home('home.light',{'target':'주방','action':'on'},'req-test-123')
    assert result.status=='UNKNOWN' and 'SmartThings' in result.message
    assert len(called)==1 and called[0][:3]==('주방','on','req-test-123')
    primary.call.assert_not_called()
    primary.close.assert_called_once()


def test_adapter_never_fallback_after_google_receipt(monkeypatch):
    from core.execution.adapters import Adapters
    from core.home import main_adapter
    primary=Mock()
    primary.allow_execution=True
    primary.plan.return_value={'status':'PREVIEW','target':'주방','on':True,'ready_for_execution':True}
    primary.call.return_value={'status':'UNKNOWN','code':'SDK_EXECUTION_UNCERTAIN','request_id':'req-gh-01'}
    primary.wait_result.side_effect=lambda value:value
    monkeypatch.setattr(main_adapter,'registry_from_environment',lambda:primary)
    invoked=[]
    monkeypatch.setattr(fb,'attempt',lambda *args:(invoked.append(True),{})[1])
    result=Adapters(None).home('home.light',{'target':'주방','action':'on'},'req-gh-01')
    assert result.status=='UNKNOWN'
    assert invoked==[]
    primary.call.assert_called_once()


def test_aircon_adapter_selects_guarded_fallback_on_bridge_outage(monkeypatch):
    from core.execution.adapters import Adapters
    from core.home import device_controls
    primary=Mock()
    primary.targets={'에어컨':{'alias':'dev_'+'1'*24,'device_type':'OnOffPluginUnitDevice','family':'air_conditioner_power','expected_name':'에어컨','allowed_commands':['on','off']}}
    primary.configuration={'enabled':True}
    primary.plan.return_value={'status':'REJECTED','code':'BRIDGE_NOT_READY'}
    monkeypatch.setattr(device_controls,'registry_from_environment',lambda:primary)
    called=[]
    monkeypatch.setattr(fb,'attempt',lambda *a:(called.append(a),{'result':'FALLBACK_NOT_VERIFIED','commands_sent':0})[1])
    result=Adapters(None).home('home.device.에어컨',{'action':'on'},'req-ac-1234')
    assert result.status=='REJECTED'
    assert len(called)==1 and called[0][:3]==('에어컨','on','req-ac-1234')
    primary.execute.assert_not_called()


def test_outage_probe_after_failed_preflight_plan(monkeypatch):
    from core.execution.adapters import Adapters
    from core.home import device_controls
    primary=Mock()
    primary.configuration={'enabled':True}
    primary.targets={'에어컨':{'alias':'dev_'+'1'*24,'device_type':'OnOffPluginUnitDevice','family':'air_conditioner_power','expected_name':'에어컨','allowed_commands':['on','off']}}
    primary.plan.side_effect=ConnectionError('gateway down')
    monkeypatch.setattr(device_controls,'registry_from_environment',lambda:primary)
    invoked=[]
    monkeypatch.setattr(fb,'attempt',lambda *a:(invoked.append(a),{'result':'FALLBACK_NOT_VERIFIED','commands_sent':0})[1])
    answer=Adapters(None).home('home.device.에어컨',{'action':'on'},'ac-one-2345')
    assert answer.status=='REJECTED' and len(invoked)==1
    primary.execute.assert_not_called()
