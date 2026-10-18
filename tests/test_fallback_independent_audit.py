"""Independent audit reproducers. All devices/transports and state are synthetic."""
from datetime import datetime, timezone
import json
import hashlib
import socket
import subprocess
from unittest.mock import Mock

import pytest

from core.home import smartthings_fallback as fb
from core.home.smartthings_direct import DirectLight
from core.home.smartthings_pc import WakeLedger
from core.home.dispatch_guard import DispatchGuard

UUID = '12345678-1234-4234-8234-123456789abc'


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv('HOME', str(tmp_path))
    for key in tuple(__import__('os').environ):
        if key.startswith('RIO_'):
            monkeypatch.delenv(key)
    def forbidden(*args, **kwargs):
        raise AssertionError('Real device/network/subprocess calls forbidden in audit')
    monkeypatch.setattr(socket.socket, 'connect', forbidden)
    monkeypatch.setattr(subprocess, 'run', forbidden)


def stamp(value):
    return datetime.fromtimestamp(value, timezone.utc).isoformat()


def device(tmp_path, monkeypatch, values):
    clock = [1000.0]
    monkeypatch.setattr('core.home.smartthings_direct.time.time', lambda: clock[0])
    cli = Mock(control_enabled=True)
    cli.switch.side_effect = values
    ctrl = DirectLight('에어컨', {'device_id': UUID, 'label': '에어컨',
        'device_type': 'ZIGBEE', 'component': 'main'}, cli, tmp_path/'st/commands.sqlite3', True)
    return ctrl, cli, clock


@pytest.mark.parametrize('failure', [ValueError('bad JSON'), OSError('bad local token')])
def test_probe_exception_is_not_evidence_of_network_outage(failure):
    transport = Mock()
    transport.status.side_effect = failure
    assert fb.proven_bridge_outage(transport) is False


def test_fresh_observation_preserves_original_unknown(tmp_path, monkeypatch):
    ctrl, cli, clock = device(tmp_path, monkeypatch, [
        {'value': 'off', 'timestamp': stamp(900)},
        {'value': 'on', 'timestamp': stamp(1001)}])
    def sent():
        clock[0] = 1002
    cli.on.side_effect = sent
    value = ctrl.set_power(True, 'audit-unknown-01')
    assert value['result'] == 'OBSERVED_SWITCH'
    with WakeLedger(ctrl.ledger_path) as ledger:
        assert ledger.db.execute('SELECT state FROM wake_requests').fetchone()[0] == 'UNKNOWN'
        assert ledger.db.execute('SELECT original_state FROM smartthings_observations').fetchone()[0] == 'UNKNOWN'


def test_report_between_reservation_and_dispatch_is_not_fresh(tmp_path, monkeypatch):
    ctrl, cli, clock = device(tmp_path, monkeypatch, [
        {'value': 'off', 'timestamp': stamp(900)},
        {'value': 'on', 'timestamp': stamp(1005)}])
    value = ctrl.set_power(True, 'audit-clock-01', before_dispatch=lambda: clock.__setitem__(0, 1010))
    assert value['result'] == 'UNKNOWN'
    assert value['post_command_change_time_verified'] is False
    assert cli.switch.call_count == 2
    cli.on.assert_called_once()


def test_future_device_timestamp_cannot_confirm(tmp_path, monkeypatch):
    ctrl, cli, clock = device(tmp_path, monkeypatch, [
        {'value': 'off', 'timestamp': stamp(900)},
        {'value': 'on', 'timestamp': stamp(1001)}])
    assert ctrl.set_power(True, 'audit-future-01')['result'] == 'UNKNOWN'


def test_already_reported_does_not_bypass_id_conflict(tmp_path, monkeypatch):
    ctrl, cli, clock = device(tmp_path, monkeypatch, [
        {'value': 'off', 'timestamp': stamp(900)},
        {'value': 'off', 'timestamp': stamp(900)},
        {'value': 'off', 'timestamp': stamp(900)}])
    assert ctrl.set_power(True, 'audit-key-01')['result'] == 'UNKNOWN'
    answer = ctrl.set_power(False, 'audit-key-01')
    assert answer['result'] != 'ALREADY_REPORTED'
    cli.on.assert_called_once()
    cli.off.assert_not_called()


def policy_fixture(tmp_path, monkeypatch):
    from types import SimpleNamespace
    targets={}
    primary={}
    types={'에어컨':'ZIGBEE','주방':'EDGE_CHILD','거실':'ZIGBEE','샤워실':'ZIGBEE','세면대':'EDGE_CHILD','lens':'MATTER'}
    for index,(name,kind) in enumerate(types.items(),1):
        alias='dev_'+str(index)*24
        targets[name]={'device_id':f'12345678-1234-4234-8234-{index:012d}','label':name,'device_type':kind,
            'confirmed_same_physical_device':True,'allowed_commands':['on','off'],'primary_alias':alias,
            'identity_evidence':{'method':'operator_physical_verification','reference_sha256':'a'*64,
                                 'confirmed_at':'2026-01-01T00:00:00Z'}}
        primary[name]={'alias':alias,'confirmed_physical_light':True,'device_type':'OnOffPluginUnitDevice',
            'family':'air_conditioner_power' if name=='에어컨' else 'smart_plug',
            'expected_name':name,'allowed_commands':['on','off']}
    data={'schema_version':2,'enabled':True,'targets':targets}
    path=tmp_path/'.config/rio/smartthings-fallback.json'
    path.parent.mkdir(parents=True,exist_ok=True)
    def write():
        for target,item in data['targets'].items():
            if 'identity_evidence' not in item:continue
            evidence=item['identity_evidence']
            record=json.dumps({'schema_version':1,'target':target,'primary_alias':item['primary_alias'],
                'smartthings_device_id':item['device_id'],'method':evidence['method'],
                'confirmed_at':evidence['confirmed_at'],'verification_basis':'operator_observed_same_device'})
            hashed=hashlib.sha256(record.encode()).hexdigest();evidence['reference_sha256']=hashed
            directory=path.parent/'fallback-identity-evidence';directory.mkdir(exist_ok=True)
            proof=directory/(hashed+'.json');proof.write_text(record);proof.chmod(0o600)
        path.write_text(json.dumps(data));path.chmod(0o600)
    write()
    registry=SimpleNamespace(targets=primary,configuration={'enabled':True},revalidate=lambda:True,
        ledger_path=tmp_path/'primary/commands.sqlite3',transport=Mock())
    registry.transport.status.return_value={'available':False,'code':'GATEWAY_UNREACHABLE'}
    return data,registry,write


@pytest.mark.parametrize('target',['에어컨','주방','거실','샤워실','세면대','lens'])
def test_six_pinned_bindings_require_original_alias_and_identity_evidence(tmp_path,monkeypatch,target):
    data,registry,write=policy_fixture(tmp_path,monkeypatch)
    assert fb.select(target,'on',registry)==data['targets'][target]
    registry.targets[target]['alias']='dev_'+'f'*24
    assert fb.select(target,'on',registry) is None
    registry.targets[target]['alias']=data['targets'][target]['primary_alias']
    data['targets'][target]['confirmed_same_physical_device']=False;write()
    assert fb.select(target,'on',registry) is None


@pytest.mark.parametrize('action',['cool','heat','temperature','setCoolingSetpoint','toggle',23])
def test_aircon_never_translates_mode_or_temperature_to_power(tmp_path,monkeypatch,action):
    _,registry,_=policy_fixture(tmp_path,monkeypatch)
    assert fb.select('에어컨',action,registry) is None


def test_legacy_name_and_confirmation_bool_are_not_new_identity_evidence(tmp_path,monkeypatch):
    data,registry,write=policy_fixture(tmp_path,monkeypatch)
    data['schema_version']=1
    for item in data['targets'].values():
        del item['primary_alias'];del item['identity_evidence']
    write()
    assert fb.select('에어컨','on',registry) is None


@pytest.mark.parametrize('mutation',['uuid','type','component','duplicate_main','capability','version','duplicate_switch'])
def test_aircon_metadata_revalidated_as_zigbee_main_switch_v1(mutation):
    from core.home.smartthings_lights import LightTransport
    from core.home.smartthings_pc import PcError
    binding={'device_id':UUID,'label':'에어컨','device_type':'ZIGBEE','component':'main'}
    api={'deviceId':UUID,'label':'에어컨','type':'ZIGBEE','components':[{'id':'main','capabilities':[{'id':'switch','version':1}]}]}
    transport=LightTransport(binding,'a'*20)
    transport.request=Mock(return_value=api)
    transport.verify_target()
    if mutation=='uuid':api['deviceId']='12345678-1234-4234-8234-123456789abd'
    elif mutation=='type':api['type']='MATTER'
    elif mutation=='component':api['components'][0]['id']='other'
    elif mutation=='duplicate_main':api['components']*=2
    elif mutation=='capability':api['components'][0]['capabilities']=[]
    elif mutation=='version':api['components'][0]['capabilities'][0]['version']=2
    elif mutation=='duplicate_switch':api['components'][0]['capabilities']*=2
    with pytest.raises(PcError):transport.verify_target()


def test_revoke_after_pre_read_before_actual_write_blocks(tmp_path,monkeypatch):
    ctrl,cli,clock=device(tmp_path,monkeypatch,[{'value':'off','timestamp':stamp(900)}])
    approved=[True]
    ctrl.revalidate=lambda:approved[0]
    result=ctrl.set_power(True,'audit-revoke-01',before_dispatch=lambda:approved.__setitem__(0,False))
    assert result['commands_sent']==0
    assert result['code']=='BINDING_OR_POLICY_CHANGED'
    cli.on.assert_not_called()


def test_post_read_failure_is_one_attempt_not_retried(tmp_path,monkeypatch):
    ctrl,cli,clock=device(tmp_path,monkeypatch,[{'value':'off','timestamp':stamp(900)},TimeoutError()])
    value=ctrl.set_power(True,'audit-read-01')
    assert value['result']=='UNKNOWN' and value['post_command_status_attempts']==1
    assert cli.switch.call_count==2
    cli.on.assert_called_once()


def test_adapter_actual_fallback_then_recovered_google_never_resends_same_id(tmp_path,monkeypatch):
    from core.execution.adapters import Adapters
    from core.home import device_controls
    import time
    data,fixture,write=policy_fixture(tmp_path,monkeypatch)
    configuration={'schema_version':1,'enabled':True,'targets':{'에어컨':{
        k:v for k,v in fixture.targets['에어컨'].items() if k!='confirmed_physical_light'}}}
    primary=device_controls.DeviceControlRegistry(fixture.transport,configuration,fixture.ledger_path)
    primary.transport.catalog.return_value={'bridge':{'available':False},'devices':[]}
    monkeypatch.setattr(device_controls,'registry_from_environment',lambda:primary)
    cli=Mock(control_enabled=True)
    changed={}
    def on():
        changed['timestamp']=stamp(time.time())
        return {'results':[{'status':'UNKNOWN'}]}
    cli.on.side_effect=on
    cli.switch.side_effect=lambda: {'value':'on','timestamp':changed['timestamp']} if changed else {'value':'off','timestamp':stamp(time.time()-60)}
    monkeypatch.setattr(fb,'VerifiedLightCli',lambda *a,**kw:cli)
    path=tmp_path/'.config/rio/pc-auth-provider.json'
    path.write_text(json.dumps({'schema_version':1,'provider':'oauth-cli','cli_root':'/not-used','cli_sha256':'a'*64}));path.chmod(0o600)
    result=Adapters(None).home('home.device.에어컨',{'action':'on'},'end-to-end-01')
    assert result.status=='OBSERVED'
    assert result.evidence['mode']=='switch_only'
    assert result.evidence['api_acceptance_verified'] is False
    assert result.evidence['physical_state_confirmed'] is False
    cli.on.assert_called_once()
    assert cli.switch.call_count==2
    primary.transport.request.assert_not_called()
    # Policy can disappear without losing the original request's identity.
    (tmp_path/'.config/rio/smartthings-fallback.json').unlink()
    old=fb.lookup_existing('에어컨','end-to-end-01')
    assert old['result']=='OBSERVED_SWITCH' and old['commands_sent']==0
    path.unlink()  # Existing results must not require current OAuth/profile state.
    for _ in range(3):
        again=Adapters(None).status('home.device.에어컨',{'action':'on'},'end-to-end-01')
        assert again.status=='OBSERVED'
    assert cli.switch.call_count==2  # No second automatic post-command read.
    cli.on.assert_called_once()
    # New process / recovered primary: same ID cannot select another provider.
    primary._ledger=None
    primary.transport.catalog.return_value={'bridge':{'available':True,'permission':'GRANTED','bridge_state':'READY','controls_enabled':True},
        'devices':[{'alias':configuration['targets']['에어컨']['alias'],'name':'에어컨',
            'device_capabilities':[{'device_type':'OnOffPluginUnitDevice','supported_commands':{'on':True}}],
            'device_control_binding':{'status':'SELECTED_EXPLICIT_TYPE','device_type':'OnOffPluginUnitDevice','allowed_commands':{'on':True}}}]}
    replay=Adapters(None).home('home.device.에어컨',{'action':'on'},'end-to-end-01')
    assert replay.status=='REJECTED'
    primary.transport.request.assert_not_called()


@pytest.mark.parametrize('status',[{'permission':'DENIED','available':False,'code':'GATEWAY_UNREACHABLE'},
    {'http_status':403,'available':False},{'available':False,'code':'GATEWAY_LOCAL_OR_RESPONSE_INVALID'},
    {'available':True,'permission':'GRANTED','bridge_state':'READY'}])
def test_not_an_outage_means_no_secondary(tmp_path,monkeypatch,status):
    _,registry,_=policy_fixture(tmp_path,monkeypatch)
    registry.transport.status.return_value=status
    forbidden=Mock(side_effect=AssertionError('Must not construct secondary'))
    monkeypatch.setattr(fb,'VerifiedLightCli',forbidden)
    assert fb.attempt('에어컨','on','not-outage-01',registry)['commands_sent']==0
    forbidden.assert_not_called()


def test_corrupt_or_deleted_known_ledger_blocks(tmp_path):
    primary=tmp_path/'primary/commands.sqlite3'
    primary.parent.mkdir(mode=0o700)
    primary.write_bytes(b'not sqlite');primary.chmod(0o600)
    from core.home.dispatch_guard import DispatchBlocked
    with pytest.raises(DispatchBlocked):
        with DispatchGuard(primary) as fence:fence.admit('corrupt-key-01','google-home',{'on':True})
    primary.unlink()
    with pytest.raises(DispatchBlocked,match='KNOWN_LEDGER_MISSING'):
        with DispatchGuard(primary):pass


def test_sanitized_local_credential_error_is_not_transport_outage(tmp_path):
    from core.home.tools import LocalHomeTransport
    transport=LocalHomeTransport(tmp_path/'nonexistent-token')
    assert transport.status()['code']=='GATEWAY_LOCAL_OR_RESPONSE_INVALID'
    assert fb.proven_bridge_outage(transport) is False


def test_primary_unknown_answer_is_immutable_when_later_sdk_result_arrives(tmp_path):
    from core.home.tools import CommandLedger
    ledger=CommandLedger(tmp_path/'primary/commands.sqlite3')
    try:
        ledger.reserve('primary-old-01',{'on':True})
        original={'status':'UNKNOWN','code':'SDK_EXECUTION_UNCERTAIN'}
        ledger.record('primary-old-01',original)
        ledger.record('primary-old-01',{'status':'SUCCEEDED','request_id':'primary-old-01'})
        row=ledger.db.execute('SELECT status,answer FROM light_commands').fetchone()
        assert row[0]=='UNKNOWN' and json.loads(row[1])==original
        saved=ledger.db.execute('SELECT answer FROM unknown_result_observations').fetchone()
        assert json.loads(saved[0])['status']=='SUCCEEDED'
    finally:ledger.close()


def test_new_explicit_power_after_fresh_observation_preserves_both_unknowns(tmp_path,monkeypatch):
    ctrl,cli,clock=device(tmp_path,monkeypatch,[])
    state=['off',stamp(900)]
    cli.switch.side_effect=lambda:{'value':state[0],'timestamp':state[1]}
    def apply(value):
        clock[0]+=1
        state[:]=[value,stamp(clock[0])]
        clock[0]+=1
        return {'results':[{'status':'UNKNOWN'}]}
    cli.on.side_effect=lambda:apply('on')
    cli.off.side_effect=lambda:apply('off')
    assert ctrl.set_power(True,'observed-on-01')['result']=='OBSERVED_SWITCH'
    assert ctrl.set_power(False,'observed-off-02')['result']=='OBSERVED_SWITCH'
    with WakeLedger(ctrl.ledger_path) as ledger:
        assert ledger.db.execute('SELECT state FROM wake_requests').fetchone()[0]=='UNKNOWN'
        assert ledger.db.execute('SELECT state FROM shutdown_requests').fetchone()[0]=='UNKNOWN'
        assert ledger.db.execute('SELECT count(*) FROM smartthings_observations').fetchone()[0]==2
    cli.on.assert_called_once();cli.off.assert_called_once()


def test_unproven_legacy_observed_state_is_not_silent_permission_to_send(tmp_path):
    from core.home.dispatch_guard import DispatchBlocked
    secondary=tmp_path/'st/commands.sqlite3'
    with WakeLedger(secondary) as ledger:
        key=ledger.reserve(UUID)
        ledger.record(key,'OBSERVED_SWITCH_ON')
    with pytest.raises(DispatchBlocked,match='SECONDARY_UNRESOLVED'):
        with DispatchGuard(tmp_path/'primary/commands.sqlite3',(secondary,)) as fence:
            fence.admit('legacy-observed-01','google-home',{'on':True})


def test_read_budget_survives_reentry_after_interruption(tmp_path,monkeypatch):
    ctrl,cli,clock=device(tmp_path,monkeypatch,[{'value':'off','timestamp':stamp(900)},TimeoutError()])
    first=ctrl.set_power(True,'postcheck-budget-01')
    assert first['post_command_status_attempts']==1
    second=ctrl._post_command({'switch':'off'},'postcheck-budget-01','on',False)
    assert second['result']=='UNKNOWN'
    assert cli.switch.call_count==2
    cli.on.assert_called_once()


def test_backups_propagate_core_key_to_shared_provider_ledger(tmp_path,monkeypatch):
    from core.home.smartthings_lights import LightBackup,handle_secondary_command
    binding={'device_id':UUID,'label':'거실','component':'main','device_type':'ZIGBEE','confirmed_same_physical_light':True}
    cli=Mock()
    cli.switch.return_value={'value':'off','timestamp':stamp(900)}
    cli.on.side_effect=TimeoutError()
    backup=LightBackup(binding,cli,tmp_path/'backup/commands.sqlite3',tmp_path/'primary/commands.sqlite3',True)
    handle_secondary_command('/home backup 거실 on',lambda _:backup,request_key='legacy-core-key-01')
    with WakeLedger(backup.ledger_path) as ledger:
        assert ledger.db.execute('SELECT id FROM wake_requests').fetchone()[0]=='legacy-core-key-01'
    handle_secondary_command('/home backup 거실 on',lambda _:backup,request_key='legacy-core-key-01')
    cli.on.assert_called_once()


def test_natural_language_does_not_silently_lower_temperature_to_power():
    from core.home.device_controls import parse_device_request
    assert parse_device_request('에어컨 켜줘')==('에어컨','on')
    assert parse_device_request('에어컨 꺼줘')==('에어컨','off')
    for text in ('에어컨 냉방으로 바꿔줘','에어컨 24도로 켜줘','에어컨 온도 낮춰줘'):
        assert parse_device_request(text) is None


@pytest.mark.parametrize('failure',['missing','tampered','public'])
def test_identity_record_must_exist_match_hash_and_be_private(tmp_path,monkeypatch,failure):
    data,registry,_=policy_fixture(tmp_path,monkeypatch)
    ref=data['targets']['에어컨']['identity_evidence']['reference_sha256']
    record=tmp_path/'.config/rio/fallback-identity-evidence'/(ref+'.json')
    if failure=='missing':record.unlink()
    elif failure=='tampered':record.write_text(record.read_text()+' ')
    else:record.chmod(0o644)
    assert fb.select('에어컨','on',registry) is None


def test_switching_primary_ledger_filename_does_not_bypass_old_history(tmp_path):
    from core.home.dispatch_guard import DispatchBlocked
    primary=tmp_path/'home/commands.sqlite3'
    with DispatchGuard(primary):pass
    with pytest.raises(DispatchBlocked,match='PRIMARY_LEDGER_CHANGED'):
        with DispatchGuard(primary.with_name('new.sqlite3')):pass


@pytest.mark.parametrize('timestamp',[None,'bad timestamp',stamp(999),stamp(1000),stamp(1060),
    '1970-01-01T00:16:41',7])
def test_post_timestamp_requires_aware_strictly_after_dispatch_not_future(tmp_path,monkeypatch,timestamp):
    ctrl,cli,clock=device(tmp_path,monkeypatch,[{'value':'off','timestamp':stamp(900)},
        {'value':'on','timestamp':timestamp}])
    result=ctrl.set_power(True,'timestamp-boundary-01')
    assert result['result']=='UNKNOWN'
    assert result['post_command_status_attempts']==1
    assert cli.switch.call_count==2
    cli.on.assert_called_once()


def test_typed_api_acceptance_is_persistent_but_not_observed_or_physical(tmp_path,monkeypatch):
    ctrl,cli,clock=device(tmp_path,monkeypatch,[{'value':'off','timestamp':stamp(900)},
        {'value':'off','timestamp':stamp(900)}])
    cli.on.return_value={'results':[{'status':'ACCEPTED'}]}
    result=ctrl.set_power(True,'typed-receipt-01')
    assert result['result']=='UNKNOWN'
    assert result['api_acceptance_verified'] is True
    assert result['physical_state_confirmed'] is False
    saved=ctrl.request_status('typed-receipt-01')
    assert saved['result']=='UNKNOWN' and saved['api_acceptance_verified'] is True
    assert saved['reported'] is None and saved['commands_sent']==0
    assert cli.switch.call_count==2


def test_target_preflight_failure_is_not_an_uncertain_send(tmp_path,monkeypatch):
    from core.home.smartthings_pc import PcError
    ctrl,cli,clock=device(tmp_path,monkeypatch,[{'value':'off','timestamp':stamp(900)},
        {'value':'off','timestamp':stamp(900)}])
    cli.verify_target.side_effect=PcError('TARGET_CHANGED')
    rejected=ctrl.set_power(True,'invalid-target-01')
    assert rejected['result']=='REJECTED' and rejected['commands_sent']==0
    cli.on.assert_not_called();cli.switch.assert_not_called()
    cli.verify_target.side_effect=None
    assert ctrl.set_power(True,'verified-target-02')['commands_sent']==1


def test_error_response_after_send_cannot_become_safe_rejection(tmp_path):
    from core.home.tools import HomeToolRegistry
    cli=Mock()
    alias='dev_'+'a'*24
    cli.catalog.return_value={'bridge':{'available':True,'permission':'GRANTED','bridge_state':'READY','controls_enabled':True},
        'devices':[{'alias':alias,'lighting':True,'commands':{'on':True},
            'control_binding':{'status':'SELECTED_EXPLICIT_TYPE','device_type':'OnOffPluginUnitDevice'}}]}
    cli.set_power.return_value={'id':'ambiguous-http-01','http_status':503,'status':'REJECTED'}
    registry=HomeToolRegistry(cli,{'거실':{'alias':alias,'device_type':'OnOffPluginUnitDevice','confirmed_physical_light':True}},tmp_path/'primary/commands.sqlite3',True)
    try:
        result=registry.call('home_set_light',{'target':'거실','on':True,'idempotency_key':'ambiguous-http-01'})
        assert result['status']=='UNKNOWN'
        secondary=Mock(control_enabled=True)
        ctrl=DirectLight('에어컨',{'device_id':UUID,'label':'에어컨','device_type':'ZIGBEE','component':'main'},secondary,
            tmp_path/'st/commands.sqlite3',True,primary_ledger_path=registry.ledger_path)
        assert ctrl.set_power(True,'no-error-fallback-02')['commands_sent']==0
        secondary.on.assert_not_called()
    finally:registry.close()


def _process_dispatch(root,provider,key,pipe,ready,release,phase=None,on=True):
    """Forked synthetic provider. No sockets, CLI, auth or operating state."""
    from pathlib import Path
    import os,time
    from core.home.tools import HomeToolRegistry,CommandLedger
    root=Path(root)
    primary=root/'primary/commands.sqlite3'
    def checkpoint(name):
        if phase==name:
            ready.set()
            release.wait(20)
    if phase=='PREPARED':
        old=DispatchGuard.admit
        def admit(self,*args):
            result=old(self,*args)
            if result is None:checkpoint('PREPARED')
            return result
        DispatchGuard.admit=admit
    if phase=='ATTEMPTED':
        old=DispatchGuard.attempted
        def attempted(self):
            old(self);checkpoint('ATTEMPTED')
        DispatchGuard.attempted=attempted
    if phase=='RESERVED':
        klass=CommandLedger if provider=='gh' else WakeLedger
        name='reserve'
        old=getattr(klass,name)
        def reserve(self,*args,**kwargs):
            value=old(self,*args,**kwargs)
            if value is None and provider=='gh' or value is not None and provider=='st':checkpoint('RESERVED')
            return value
        setattr(klass,name,reserve)
    if phase=='RESULT_RECORDED':
        old=DispatchGuard.finish
        def finish(self,*args,**kw):
            old(self,*args,**kw);checkpoint('RESULT_RECORDED')
        DispatchGuard.finish=finish
    if phase=='ACCEPTED' and provider=='gh':
        old=CommandLedger.record
        def record(self,key,answer):
            old(self,key,answer)
            if answer.get('status')=='DISPATCHED':checkpoint('ACCEPTED')
        CommandLedger.record=record
    class Transport:
        control_enabled=True
        sent=False
        def verify_target(self):pass
        def catalog(self):
            return {'bridge':{'available':True,'permission':'GRANTED','bridge_state':'READY','controls_enabled':True},
                'devices':[{'alias':'dev_'+'a'*24,'lighting':True,'commands':{'on':True,'off':True},
                    'control_binding':{'status':'SELECTED_EXPLICIT_TYPE','device_type':'OnOffPluginUnitDevice'}}]}
        def result(self,key):return {'id':key,'status':'UNKNOWN'}
        def switch(self):
            if self.sent:checkpoint('ACCEPTED')
            return {'value':'off' if on else 'on','timestamp':stamp(900)}
        def write(self):
            fd=os.open(root/'writes.log',os.O_WRONLY|os.O_CREAT|os.O_APPEND,0o600)
            os.write(fd,(provider+'\n').encode());os.fsync(fd);os.close(fd)
            self.sent=True
            checkpoint('SENT')
            if phase is None:
                ready.set();release.wait(20)
        def set_power(self,alias,on,key):
            self.write()
            return {'id':key,'status':'DISPATCHED' if phase=='ACCEPTED' else 'UNKNOWN'}
        def on(self):
            self.write();return {'results':[{'status':'ACCEPTED'}]}
        off=on
    try:
        transport=Transport()
        if provider=='gh':
            ctrl=HomeToolRegistry(transport,{'거실':{'alias':'dev_'+'a'*24,'device_type':'OnOffPluginUnitDevice','confirmed_physical_light':True}},primary,True)
            answer=ctrl.call('home_set_light',{'target':'거실','on':on,'idempotency_key':key});ctrl.close()
            result={'commands_sent':int(transport.sent),'answer':answer}
        else:
            ctrl=DirectLight('에어컨',{'device_id':UUID,'label':'에어컨','device_type':'ZIGBEE','component':'main'},transport,root/'st/commands.sqlite3',True,primary_ledger_path=primary)
            result=ctrl.set_power(on,key)
        pipe.send(result)
    except BaseException as error:
        pipe.send({'error':type(error).__name__,'detail':str(error)})
    finally:pipe.close()


def start_process(root,provider,key,phase=None,on=True):
    import multiprocessing
    ctx=multiprocessing.get_context('fork')
    recv,send=ctx.Pipe(duplex=False)
    ready,release=ctx.Event(),ctx.Event()
    process=ctx.Process(target=_process_dispatch,args=(str(root),provider,key,send,ready,release,phase,on))
    process.start();send.close()
    return process,recv,ready,release


def finish_process(process,pipe):
    try:
        assert pipe.poll(8),'worker did not report'
        result=pipe.recv()
        process.join(8)
        assert process.exitcode==0
        assert 'error' not in result,result
        return result
    finally:
        if process.is_alive():process.kill();process.join(8)
        pipe.close()


@pytest.mark.parametrize('first,second',[('gh','st'),('st','gh'),('gh','gh'),('st','st')])
@pytest.mark.parametrize('opposite',[False,True])
def test_two_process_race_has_one_dispatch(tmp_path,first,second,opposite):
    process,pipe,ready,release=start_process(tmp_path,first,'race-first-01')
    try:
        assert ready.wait(8)
        other,opipe,_,_=start_process(tmp_path,second,'race-second-02',on=not opposite)
        result=finish_process(other,opipe)
        assert result['commands_sent']==0
        release.set()
        assert finish_process(process,pipe)['commands_sent']==1
        assert len((tmp_path/'writes.log').read_text().splitlines())==1
    finally:
        if process.is_alive():process.kill();process.join(8)


@pytest.mark.parametrize('provider',['gh','st'])
@pytest.mark.parametrize('phase',['PREPARED','RESERVED','ATTEMPTED','SENT','ACCEPTED','RESULT_RECORDED'])
def test_sigkill_restart_never_replays_or_switches_provider(tmp_path,provider,phase):
    process,pipe,ready,release=start_process(tmp_path,provider,'crash-original-01',phase)
    try:
        assert ready.wait(8),phase
        process.kill();process.join(8)
        assert process.exitcode==-9
    finally:
        if process.is_alive():process.kill();process.join(8)
        pipe.close()
    expected=1 if phase in {'SENT','ACCEPTED','RESULT_RECORDED'} else 0
    for retry_provider,key in [(provider,'crash-original-01'),('st' if provider=='gh' else 'gh','crash-new-key-02')]:
        retry,rpipe,_,_=start_process(tmp_path,retry_provider,key)
        assert finish_process(retry,rpipe)['commands_sent']==0
    written=(tmp_path/'writes.log').read_text().splitlines() if (tmp_path/'writes.log').exists() else []
    assert len(written)==expected
