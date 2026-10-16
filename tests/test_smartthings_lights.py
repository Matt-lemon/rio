import json
from pathlib import Path
from unittest.mock import Mock

import pytest

from core.home.smartthings_lights import LightBackup, LightTransport, backup_from_environment, handle_secondary_command
from core.home.smartthings_pc import PcError
from core.home.tools import CommandLedger


@pytest.fixture
def binding():
    return {'device_id':'12345678-1234-4234-8234-123456789abc','label':'거실','component':'main',
            'device_type':'ZIGBEE','confirmed_same_physical_light':True}


def controller(tmp_path, binding, *, enabled=True):
    transport=Mock()
    transport.switch.return_value={'value':'off','timestamp':'observed-time'}
    transport.on.return_value={'results':[{'status':'ACCEPTED','id':'one'}]}
    transport.off.return_value={'results':[{'status':'ACCEPTED'}]}
    return LightBackup(binding,transport,tmp_path/'backup/commands.sqlite3',tmp_path/'primary/commands.sqlite3',enabled),transport


def test_secondary_is_read_only_and_does_not_construct_write_controller():
    factory=Mock()
    assert '조회만' in handle_secondary_command('/home secondary 거실 on',factory)
    factory.assert_not_called()


@pytest.mark.parametrize('confirmed,enabled',[(False,True),(True,False)])
def test_unconfirmed_or_disabled_never_dispatches(tmp_path,binding,confirmed,enabled):
    binding['confirmed_same_physical_light']=confirmed
    ctrl,transport=controller(tmp_path,binding,enabled=enabled)
    assert ctrl.set_power(True)['commands_sent']==0
    transport.on.assert_not_called();transport.verify_target.assert_not_called()


@pytest.mark.parametrize('pending',['DISPATCHING','DISPATCHED','UNKNOWN'])
def test_uncertain_google_home_request_blocks_explicit_backup(tmp_path,binding,pending):
    ctrl,transport=controller(tmp_path,binding)
    ledger=CommandLedger(ctrl.primary_ledger_path)
    ledger.reserve('request01',{'alias':'one','on':True});ledger.record('request01',{'status':pending});ledger.close()
    assert ctrl.set_power(True)['result']=='PRIMARY_PENDING'
    transport.on.assert_not_called();transport.verify_target.assert_not_called()


def test_uncertain_backup_persists_and_never_resends_after_restart(tmp_path,binding):
    ctrl,transport=controller(tmp_path,binding);transport.on.side_effect=TimeoutError('private-token')
    assert ctrl.set_power(True)['result']=='UNKNOWN'
    ctrl2,_=controller(tmp_path,binding)
    assert ctrl2.set_power(True)['commands_sent']==0
    transport.on.assert_called_once()
    assert 'private-token' not in handle_secondary_command('/home backup 거실 on',lambda _:ctrl)


def test_accepted_does_not_mean_physical_completion(tmp_path,binding):
    ctrl,transport=controller(tmp_path,binding)
    result=ctrl.set_power(True)
    assert result['result']=='ACCEPTED' and result['physical_state_confirmed'] is False
    assert ctrl.set_power(False)['result']=='ALREADY_REPORTED'
    transport.on.assert_called_once();transport.off.assert_not_called()


def test_observation_never_posts(tmp_path,binding):
    ctrl,transport=controller(tmp_path,binding)
    reply=handle_secondary_command('/home secondary 거실 status',lambda _:ctrl)
    assert '보고값' in reply and '별도 확인' in reply
    transport.on.assert_not_called();transport.off.assert_not_called()


@pytest.mark.parametrize('value',[1,'on',None])
def test_nonboolean_request_rejected(tmp_path,binding,value):
    ctrl,transport=controller(tmp_path,binding)
    with pytest.raises(PcError):ctrl.set_power(value)
    transport.on.assert_not_called()


@pytest.mark.parametrize('device_type,label,component',[('LAN','거실','main'),('ZIGBEE','컴퓨터','main'),('ZIGBEE','거실','Utility')])
def test_secondary_target_cannot_silently_change_to_pc_or_component(tmp_path,binding,device_type,label,component):
    transport=LightTransport(binding,'a'*20)
    transport.request=Mock(return_value={'deviceId':binding['device_id'],'type':device_type,'label':label,
        'components':[{'id':component,'capabilities':[{'id':'switch'}]}]})
    with pytest.raises(PcError):transport.verify_target()


def test_private_binding_defaults_to_disabled(tmp_path,binding,monkeypatch):
    cfg=tmp_path/'bindings.json';cfg.write_text(json.dumps({'schema_version':1,'targets':{'거실':binding}}));cfg.chmod(0o600)
    token=tmp_path/'token';token.write_text('a'*20);token.chmod(0o600)
    monkeypatch.setenv('RIO_HOME_SECONDARY_TARGET_FILE',str(cfg));monkeypatch.setenv('RIO_SMARTTHINGS_TOKEN_FILE',str(token))
    monkeypatch.delenv('RIO_HOME_BACKUP_EXECUTION_ENABLED',raising=False)
    assert backup_from_environment('거실').allow_execution is False
    binding['device_type']='LAN';cfg.write_text(json.dumps({'schema_version':1,'targets':{'거실':binding}}))
    with pytest.raises(PcError):backup_from_environment('거실')
