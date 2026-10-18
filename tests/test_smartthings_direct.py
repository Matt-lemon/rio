"""Isolated explicit direct CLI light path tests; no real Docker or device calls."""
import json
from pathlib import Path
from unittest.mock import Mock

import pytest

from core.home import smartthings_direct as direct
from core.home.smartthings_pc import PcError, WakeLedger

KITCHEN='12345678-1234-4234-8234-123456789abc'
LIVING='12345678-1234-4234-8234-123456789abd'


@pytest.fixture
def private(tmp_path, monkeypatch):
    monkeypatch.setenv('HOME',str(tmp_path))
    conf=tmp_path/'.config/rio'
    conf.mkdir(parents=True)
    policy=conf/'smartthings-direct-policy.json'
    policy.write_text(json.dumps({'schema_version':1,'enabled':True,'allowed_targets':['주방','거실']}))
    policy.chmod(0o600)
    selection=conf/'pc-auth-provider.json'
    selection.write_text(json.dumps({'schema_version':1,'provider':'oauth-cli','cli_root':str(tmp_path/'oauth'),'cli_sha256':'a'*64}))
    selection.chmod(0o600)
    config=conf/'smartthings-lights.json'
    config.write_text(json.dumps({'schema_version':1,'targets':{
        '주방':{'device_id':KITCHEN,'label':'주방','component':'main','device_type':'EDGE_CHILD','confirmed_same_physical_light':False},
        '거실':{'device_id':LIVING,'label':'거실','component':'main','device_type':'ZIGBEE','confirmed_same_physical_light':False}
    }}))
    config.chmod(0o600)
    fake=Mock()
    fake.control_enabled=True
    fake.switch.return_value={'value':'off','timestamp':'now'}
    fake.on.return_value={'results':[{'status':'UNKNOWN'}]}
    fake.off.return_value={'results':[{'status':'UNKNOWN'}]}
    monkeypatch.setattr(direct,'VerifiedLightCli',lambda *a,**kw: fake)
    return conf,fake


def test_verified_pinned_names_allow_read_without_physical_equivalence(private):
    _,fake=private
    assert direct.from_environment('주방').status()['switch']=='off'
    assert direct.from_environment('거실').status()['target']=='거실'
    assert '주방' in direct.handle_direct_command('/home smartthings 주방 status')
    assert '꺼짐' in direct.handle_direct_command('/home smartthings 거실 status')
    fake.on.assert_not_called()


def test_explicit_once_and_persistent_unknown_never_resends(private):
    _,fake=private
    controller=direct.from_environment('거실')
    first=controller.set_power(True,'12345678')
    assert first['result']=='UNKNOWN' and first['commands_sent']==1
    assert fake.on.call_count==1
    second=direct.from_environment('거실').set_power(True,'other-request')
    assert second['result']=='PENDING_DO_NOT_RETRY' and second['commands_sent']==0
    fake.on.assert_called_once()
    with WakeLedger(controller.ledger_path) as db:
        assert db.db.execute('SELECT state FROM wake_requests WHERE target=?',(LIVING,)).fetchone()[0]=='UNKNOWN'


def test_targets_isolated_in_persistent_ledger(private):
    _,fake=private
    assert direct.from_environment('거실').set_power(True)['commands_sent']==1
    assert direct.from_environment('주방').set_power(True)['commands_sent']==1
    assert fake.on.call_count==2


def test_already_reported_does_not_send(private):
    _,fake=private
    assert direct.from_environment('주방').set_power(False)['result']=='ALREADY_REPORTED'
    fake.off.assert_not_called()


def test_control_locked_even_if_binding_exists(private):
    conf,fake=private
    policy=conf/'smartthings-direct-policy.json'
    policy.write_text(json.dumps({'schema_version':1,'enabled':True,'allowed_targets':['주방']}))
    result=direct.from_environment('거실').set_power(True)
    assert result['result']=='REJECTED' and result['commands_sent']==0
    fake.on.assert_not_called()


def test_config_rejects_extra_flag_and_hidden_cross_provider_confirm(private):
    conf,fake=private
    policy=conf/'smartthings-direct-policy.json'
    policy.write_text(json.dumps({'schema_version':1,'enabled':True,'allowed_targets':['주방','거실'],'trust_google_mapping':True}))
    with pytest.raises(PcError,match='DIRECT_POLICY_INVALID'):
        direct.from_environment('주방')
    fake.on.assert_not_called()


def test_nonlisted_name_does_not_instantiate_provider(private):
    _,fake=private
    with pytest.raises(PcError,match='DIRECT_TARGET_FORBIDDEN'):
        direct.from_environment('세면대')
    assert '확인하지 못했어' in direct.handle_direct_command('/home smartthings 세면대 on')
    fake.on.assert_not_called()


def test_no_implicit_fallback_from_regular_home(private):
    _,fake=private
    assert direct.handle_direct_command('/home 거실 on') is None
    assert direct.handle_direct_command('/home backup 거실 on') is None
    assert '명령:' in direct.handle_direct_command('/home smartthings 거실 toggle')
    fake.on.assert_not_called()


def test_unverified_selection_fails_closed(private):
    conf,fake=private
    selection=conf/'pc-auth-provider.json'
    selection.write_text(json.dumps({'schema_version':1,'provider':'pat','cli_root':'/tmp','cli_sha256':'a'*64}))
    with pytest.raises(PcError,match='DIRECT_CLI_CONFIG_INVALID'):
        direct.from_environment('주방')
    fake.on.assert_not_called()


def test_main_adapter_explicit_provider_route_precedes_generic_inventory(private):
    from core.home.main_adapter import handle_home_command
    _,fake=private
    assert 'SmartThings 거실 스위치 보고값:' in handle_home_command('/home smartthings 거실 status')
    assert 'SmartThings 주방 스위치 보고값:' in handle_home_command('/home smartthings 주방 status')
    fake.on.assert_not_called()
