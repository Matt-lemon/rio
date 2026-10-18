"""Read-only reconciliation and structured Core pathway. No real device writes."""
import time
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock

import pytest

from core.home.smartthings_direct import DirectLight
from core.home.smartthings_pc import WakeLedger
from core.execution.catalog import Catalog
from core.execution.adapters import Adapters

UUID='12345678-1234-4234-8234-123456789abc'


def clock_stamp(offset=0):
    return datetime.fromtimestamp(time.time()+offset,timezone.utc).isoformat()


def sample(tmp_path, *, state='off', timestamp=None):
    cli=Mock()
    cli.control_enabled=True
    cli.switch.return_value={'value':state,'timestamp':timestamp or clock_stamp()}
    binding={'device_id':UUID,'label':'주방','device_type':'EDGE_CHILD'}
    return DirectLight('주방', binding, cli, tmp_path/'private'/'ledger.db', True),cli


def test_legacy_unknown_without_dispatch_time_is_preserved(tmp_path):
    device,cli=sample(tmp_path)
    with WakeLedger(device.ledger_path) as db:
        key=db.reserve(UUID)
        db.record(key,'UNKNOWN')
        # Give the cloud report a timestamp unambiguously after the request.
        db.db.execute('UPDATE wake_requests SET created=? WHERE id=?',(time.time()-20,key))
        db.db.commit()
    cli.switch.return_value={'value':'on','timestamp':clock_stamp(-5)}
    observed=device.status()
    assert not observed['pending_resolved_by_fresh_status']
    assert device.request_status(key)['result']=='UNKNOWN'
    with WakeLedger(device.ledger_path) as db:
        assert db.db.execute('SELECT state FROM wake_requests WHERE id=?',(key,)).fetchone()[0]=='UNKNOWN'
        assert db.db.execute('SELECT original_state,reported_state FROM smartthings_observations WHERE request_id=?',(key,)).fetchone() is None
    cli.on.assert_not_called()
    cli.off.assert_not_called()


def test_stale_timestamp_never_confirms(tmp_path):
    device,cli=sample(tmp_path)
    with WakeLedger(device.ledger_path) as db:
        key=db.reserve(UUID)
        db.record(key,'UNKNOWN')
    cli.switch.return_value={'value':'on','timestamp':clock_stamp(-3600)}
    assert device.status()['pending_resolved_by_fresh_status'] is False
    assert device.request_status(key)['result']=='UNKNOWN'
    cli.on.assert_not_called()


def test_wrong_state_never_confirms(tmp_path):
    device,cli=sample(tmp_path)
    with WakeLedger(device.ledger_path) as db:
        key=db.reserve(UUID)
        db.record(key,'UNKNOWN')
        db.db.execute('UPDATE wake_requests SET created=? WHERE id=?',(time.time()-20,key))
        db.db.commit()
    cli.switch.return_value={'value':'off','timestamp':clock_stamp(-5)}
    assert device.status()['pending_resolved_by_fresh_status'] is False
    assert device.request_status(key)['result']=='UNKNOWN'
    cli.on.assert_not_called()


def test_exception_after_dispatch_no_repeat_and_stays_unknown(tmp_path):
    device,cli=sample(tmp_path,state='off',timestamp=clock_stamp(-60))
    cli.on.side_effect=TimeoutError('PRIVATE')
    progress=Mock()
    value=device.set_power(True,request_key='one-request-01',before_dispatch=progress)
    assert value['result']=='UNKNOWN' and value['commands_sent']==1
    progress.assert_called_once()
    cli.on.assert_called_once()
    next_result=device.set_power(True,request_key='other-request')
    assert next_result['result']=='PENDING_DO_NOT_RETRY'
    cli.on.assert_called_once()


def test_successful_cli_process_with_fresh_change_is_observation_not_api_receipt(tmp_path):
    device,cli=sample(tmp_path,state='off',timestamp=clock_stamp(-60))
    changed={}
    cli.on.side_effect=lambda:changed.update(timestamp=clock_stamp())
    cli.switch.side_effect=lambda: {'value':'on','timestamp':changed['timestamp']} if changed else {'value':'off','timestamp':clock_stamp(-60)}
    result=device.set_power(True,request_key='one-request-02')
    assert result['result']=='OBSERVED_SWITCH'
    assert result['physical_state_confirmed'] is False
    assert result['api_acceptance_verified'] is False
    assert result['cli_process_completed'] is True
    cli.on.assert_called_once()


def test_strict_catalog_routes_explicit_provider_not_opaque_legacy():
    from core.research_browser import BrowserConversation
    catalog=Catalog()
    for name in ('주방','거실'):
        for action in ('on','off','status'):
            value=catalog.resolve(f'/home smartthings {name} {action}',BrowserConversation())
            assert value['name']=='home.smartthings'
            assert value['arguments']=={'target':name,'action':action}
    assert catalog.effect(catalog.describe('home.smartthings'),{'target':'주방','action':'status'})=='read'
    assert catalog.effect(catalog.describe('home.smartthings'),{'target':'주방','action':'on'})=='control'


def test_core_adapter_returns_typed_receipt_and_result_lookup(tmp_path, monkeypatch):
    from core.home import smartthings_direct
    device,cli=sample(tmp_path)
    cli.switch.return_value={'value':'off','timestamp':clock_stamp(-3600)}
    monkeypatch.setattr(smartthings_direct,'from_environment',lambda target:device)
    progress=Mock()
    args={'target':'주방','action':'on'}
    result=Adapters(None).call('home.smartthings',args,'request-typed-01',None,progress=progress)
    assert result.status=='UNKNOWN' and result.request_id=='request-typed-01'
    assert result.evidence['commands_sent']==1
    progress.attempted.assert_called_once()
    cli.on.assert_called_once()
    old=Adapters(None).status('home.smartthings',args,'request-typed-01')
    assert old.status=='UNKNOWN'
    cli.on.assert_called_once()


def test_core_adapter_never_claims_physical_state_on_structured_observation(tmp_path,monkeypatch):
    from core.home import smartthings_direct
    device,cli=sample(tmp_path)
    changed={}
    cli.on.side_effect=lambda:changed.update(timestamp=clock_stamp())
    cli.switch.side_effect=lambda: {'value':'on','timestamp':changed['timestamp']} if changed else {'value':'off','timestamp':clock_stamp(-60)}
    monkeypatch.setattr(smartthings_direct,'from_environment',lambda target:device)
    result=Adapters(None).call('home.smartthings',{'target':'주방','action':'on'},'request-typed-02',None)
    assert result.status=='OBSERVED' and result.confirmation=='SWITCH_REPORT'
    assert result.evidence['physical_state_confirmed'] is False


def test_unknown_triggers_one_post_command_switch_read_no_retry(tmp_path):
    device,cli=sample(tmp_path,state='off',timestamp=clock_stamp(-90))
    cli.on.return_value={'results':[{'status':'UNKNOWN'}]}
    value=device.set_power(True,'request-once-01')
    assert value['result']=='UNKNOWN' and value['commands_sent']==1
    # One pre-command read and exactly one post-command status read.
    assert cli.switch.call_count==2
    assert cli.on.call_count==1
    assert value['code']=='WAITING_FOR_FRESH_DEVICE_REPORT'


def test_cli_error_also_triggers_one_read_and_no_retry(tmp_path):
    device,cli=sample(tmp_path,state='off',timestamp=clock_stamp(-90))
    cli.on.side_effect=TimeoutError('test')
    value=device.set_power(True,'request-once-02')
    assert value['result']=='UNKNOWN'
    assert value['code']=='CLI_OUTCOME_UNCERTAIN'
    assert cli.switch.call_count==2
    assert cli.on.call_count==1


def test_missing_timestamp_not_accepted(tmp_path):
    device,cli=sample(tmp_path,state='off',timestamp=clock_stamp(-100))
    cli.switch.side_effect=[{'value':'off','timestamp':clock_stamp(-100)},
                            {'value':'on','timestamp':None}]
    result=device.set_power(True,'request-once-03')
    assert result['result']=='UNKNOWN'
    assert cli.switch.call_count==2
    assert cli.on.call_count==1


def test_unproven_report_is_visible_as_read_only_evidence(tmp_path):
    device,cli=sample(tmp_path)
    cli.switch.side_effect=[{'value':'off','timestamp':clock_stamp(-30)},
                            {'value':'on','timestamp':clock_stamp(-50)}]
    result=device.set_power(True,'one-request-04')
    assert result['result']=='UNKNOWN'
    assert result['post_command_status_checked'] is True
    assert result['post_command_switch_report']=='on'
    assert result['post_command_switch_matches_request'] is True
    assert result['post_command_change_time_verified'] is False
    assert cli.switch.call_count==2
    cli.on.assert_called_once()


def test_shell_reply_exposes_post_read_report_without_claiming_success(tmp_path,monkeypatch):
    from core.home import smartthings_direct
    device,cli=sample(tmp_path)
    cli.switch.side_effect=[{'value':'off','timestamp':clock_stamp(-120)},
                            {'value':'on','timestamp':clock_stamp(-120)}]
    monkeypatch.setattr(smartthings_direct,'from_environment',lambda _:device)
    value=smartthings_direct.handle_direct_command('/home smartthings 주방 on')
    assert '상태 1회 조회: 켜짐' in value and '미확정' in value
    cli.on.assert_called_once()
    assert cli.switch.call_count==2
