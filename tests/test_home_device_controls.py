import asyncio
import importlib.util
import json
from pathlib import Path
import time
from unittest.mock import Mock, AsyncMock

import pytest

from core.home.device_controls import DeviceControlRegistry, parse_device_request, validate_device_targets
from core.home.tools import CommandLedger

ALIAS = 'dev_' + 'a'*24


def config(enabled=True):
    return {'schema_version': 1, 'enabled': enabled, 'targets': {'lens': {
        'alias': ALIAS, 'device_type': 'OnOffPluginUnitDevice', 'family': 'smart_plug',
        'expected_name': 'lens', 'allowed_commands': ['on', 'off']}}}


def catalog():
    return {'bridge': {'available': True, 'permission': 'GRANTED', 'bridge_state': 'READY', 'controls_enabled': True},
        'devices': [{'alias': ALIAS, 'name': 'lens', 'lighting': False,
        'device_capabilities': [{'device_type': 'OnOffPluginUnitDevice', 'supported_commands': {'on': True, 'off': True}}],
        'device_control_binding': {'status': 'SELECTED_EXPLICIT_TYPE', 'device_type': 'OnOffPluginUnitDevice',
                                  'allowed_commands': {'on': True, 'off': True}}}]}


def registry(tmp_path):
    transport = Mock()
    transport.catalog.return_value = catalog()
    transport.request.return_value = {'status': 'DISPATCHED', 'id': 'request01'}
    return DeviceControlRegistry(transport, config(), tmp_path/'ledger/commands.sqlite3')


def test_result_collection_reports_final_sdk_failure_without_another_write(tmp_path,monkeypatch):
    r=registry(tmp_path)
    try:
        initial=r.execute('lens','on','request01')
        r.transport.result.side_effect=[{'status':'DISPATCHED'},
            {'status':'UNKNOWN','result':{'code':'SDK_EXECUTION_UNCERTAIN','sdk_error_code':19,
                                         'sdk_unwrapped_error_code':5,'sdk_sub_error_codes':[1905]}}]
        monkeypatch.setattr('core.home.tools.time.sleep',lambda _:None)
        final=r.wait_result(initial)
        assert final['status']=='UNKNOWN' and final['sdk_unwrapped_error_code']==5
        assert r.transport.request.call_count==1 and r.transport.result.call_count==2
        assert r.ledger.db.execute('SELECT status FROM light_commands').fetchone()[0]=='UNKNOWN'
    finally:r.close()


@pytest.mark.parametrize('response',[{'http_status':404},None,{'id':'wrongrequest','status':'SUCCEEDED'},{}])
def test_result_read_failure_never_turns_a_dispatched_write_into_success_or_rejection(tmp_path,response):
    r=registry(tmp_path)
    try:
        initial=r.execute('lens','on','request01');r.transport.result.return_value=response
        assert r.wait_result(initial)==initial
        assert r.transport.request.call_count==1
        assert r.ledger.db.execute('SELECT status FROM light_commands').fetchone()[0]=='DISPATCHED'
    finally:r.close()


def test_zero_wait_preserves_pending_result_without_any_network_read(tmp_path):
    r=registry(tmp_path)
    try:
        initial=r.execute('lens','on','request01');r.transport.result.reset_mock()
        assert r.wait_result(initial,timeout_s=0)==initial
        r.transport.result.assert_not_called()
        assert r.transport.request.call_count==1
    finally:r.close()


def test_chat_surfaces_not_found_instead_of_claiming_request_completion(tmp_path,monkeypatch):
    from types import SimpleNamespace
    from core.home.device_controls import handle_device_command
    r=registry(tmp_path)
    r.transport.result.return_value={'status':'UNKNOWN','result':{
        'code':'SDK_EXECUTION_UNCERTAIN','sdk_error_code':19,'sdk_unwrapped_error_code':5}}
    monkeypatch.setattr('core.home.device_controls.uuid.uuid4',lambda:SimpleNamespace(hex='request01'))
    message=handle_device_command('lens 켜줘',factory=lambda:r)
    assert 'NOT_FOUND, 5' in message and '다시 보내지' in message
    assert '완료됐' not in message and r.transport.request.call_count==1


def media_registry(tmp_path, connectivity='ONLINE'):
    value = config()
    binding = value['targets'].pop('lens')
    binding.update(family='soundbar', device_type='SpeakerDevice', expected_name='사운드바', allowed_commands=['play'])
    value['targets']['사운드바'] = binding
    transport = Mock()
    transport.catalog.return_value = catalog()
    item = transport.catalog.return_value['devices'][0]
    item.update(name='사운드바', device_capabilities=[{'device_type': 'SpeakerDevice',
        'media_connectivity': connectivity, 'supported_commands': {'play': True, 'pause': False}}],
        device_control_binding={'status': 'SELECTED_EXPLICIT_TYPE', 'device_type': 'SpeakerDevice',
                                'allowed_commands': {'play': True, 'pause': False}})
    transport.request.return_value = {'status': 'DISPATCHED', 'id': 'request01'}
    return DeviceControlRegistry(transport, value, tmp_path/'ledger/commands.sqlite3')


def test_cast_media_uses_verified_speaker_route_not_power_or_ir_toggle(tmp_path):
    r = media_registry(tmp_path)
    try:
        assert r.execute('사운드바', 'play', 'request01')['status'] == 'DISPATCHED'
        payload = r.transport.request.call_args.args[1]
        assert payload['arguments'] == {'device_type': 'SpeakerDevice', 'command': 'play'}
        assert r.execute('사운드바', 'off', 'request02')['status'] == 'REJECTED'
        assert r.transport.request.call_count == 1
    finally: r.close()


@pytest.mark.parametrize('connectivity', ['OFFLINE', 'UNKNOWN', None])
def test_media_connectivity_cannot_be_inferred_from_another_trait(tmp_path, connectivity):
    r = media_registry(tmp_path, connectivity)
    try:
        cap = r.transport.catalog.return_value['devices'][0]['device_capabilities'][0]
        cap['power_connectivity'] = 'ONLINE'
        assert r.execute('사운드바', 'play', 'request01')['code'] == 'DEVICE_OFFLINE_OR_UNKNOWN'
        r.transport.request.assert_not_called()
    finally: r.close()


def test_media_unknown_blocks_power_and_media_without_automatic_fallback(tmp_path):
    r = media_registry(tmp_path)
    try:
        r.transport.request.side_effect = TimeoutError()
        assert r.execute('사운드바', 'play', 'request01')['status'] == 'UNKNOWN'
        assert r.execute('사운드바', 'play', 'request02')['code'] == 'PREVIOUS_RESULT_UNRESOLVED'
        assert r.transport.request.call_count == 1
    finally: r.close()


@pytest.mark.parametrize('text,command', [('사운드바 재생해줘','play'), ('TV 일시 정지해줘','pause'),
                                        ('/home 사운드바 stop','stop')])
def test_media_requests_require_explicit_actions(text, command):
    assert parse_device_request(text)[1] == command


def test_plug_has_dedicated_route_without_light_spoofing(tmp_path):
    r = registry(tmp_path)
    assert r.execute('lens', 'on', 'request01')['status'] == 'DISPATCHED'
    payload = r.transport.request.call_args.args[1]
    assert payload['operation'] == 'device_command'
    assert payload['arguments'] == {'device_type': 'OnOffPluginUnitDevice', 'command': 'on'}
    assert 'on' not in payload
    r.close()


@pytest.mark.parametrize('mutation', ['stale', 'rename', 'unsupported', 'duplicate_type', 'unapproved'])
def test_changed_or_unverified_current_inventory_never_dispatches(tmp_path, mutation):
    r = registry(tmp_path)
    value = r.transport.catalog.return_value
    item = value['devices'][0]
    if mutation == 'stale': value['bridge']['available'] = False
    if mutation == 'rename': item['name'] = '환풍기'
    if mutation == 'unsupported': item['device_capabilities'][0]['supported_commands']['on'] = False
    if mutation == 'duplicate_type': item['device_capabilities'] *= 2
    if mutation == 'unapproved': item['device_control_binding']['allowed_commands']['on'] = False
    assert r.execute('lens', 'on', 'request01')['status'] == 'REJECTED'
    r.transport.request.assert_not_called()
    assert not r.ledger_path.exists()


def test_unknown_send_blocks_any_new_write_and_same_request_is_not_resent(tmp_path):
    r = registry(tmp_path)
    r.transport.request.side_effect = TimeoutError('private credential')
    first = r.execute('lens', 'on', 'request01')
    assert first['status'] == 'UNKNOWN'
    assert r.execute('lens', 'on', 'request01') == first
    assert r.execute('lens', 'off', 'request02')['code'] == 'PREVIOUS_RESULT_UNRESOLVED'
    assert r.transport.request.call_count == 1
    assert 'private credential' not in repr(first)
    r.close()


def test_light_and_device_writes_share_uncertainty_gate(tmp_path):
    r = registry(tmp_path)
    ledger = CommandLedger(r.ledger_path)
    ledger.reserve('lightkey01', {'alias': 'dev_'+'b'*24, 'on': True})
    ledger.close()
    assert r.execute('lens', 'on', 'request01')['code'] == 'PREVIOUS_RESULT_UNRESOLVED'
    r.transport.request.assert_not_called()
    r.close()


def test_completed_previous_delivery_is_refreshed_before_next_request(tmp_path):
    r=registry(tmp_path)
    r.execute('lens','on','request01')
    r.transport.result.return_value={'status':'SUCCEEDED','result':{'code':'SDK_ACK_NOT_PHYSICAL_CONFIRMATION'}}
    r.transport.request.return_value={'status':'DISPATCHED','id':'request02'}
    assert r.execute('lens','off','request02')['status']=='DISPATCHED'
    assert r.transport.request.call_count==2
    r.transport.result.assert_called_once_with('request01')
    assert r.ledger.db.execute('SELECT status FROM light_commands WHERE key=?',('request01',)).fetchone()[0]=='SUCCEEDED'
    assert r.ledger.db.execute('SELECT previous_status,new_status FROM command_result_refreshes').fetchall()==[('DISPATCHED','SUCCEEDED')]
    r.close()


@pytest.mark.parametrize('response',[{'http_status':404,'code':'NOT_FOUND'}, {'status':'SUCCEEDED','id':'wrong_id'}, {}, {'status':'DISPATCHED'}, {'status':'UNKNOWN'}])
def test_unverified_result_cannot_unlock_next_write(tmp_path,response):
    r=registry(tmp_path)
    r.execute('lens','on','request01')
    r.transport.result.return_value=response
    assert r.execute('lens','off','request02')['code']=='PREVIOUS_RESULT_UNRESOLVED'
    assert r.transport.request.call_count==1
    r.close()


def test_unknown_and_human_review_are_preserved_by_result_refresh(tmp_path):
    r=registry(tmp_path)
    r.transport.request.side_effect=TimeoutError()
    r.execute('lens','on','request01')
    r.transport.result.return_value={'status':'SUCCEEDED'}
    r.ledger.refresh_dispatched(r.transport.result)
    r.transport.result.assert_not_called()
    assert r.ledger.db.execute('SELECT status FROM light_commands').fetchone()[0]=='UNKNOWN'
    r.close()


def test_safe_sdk_failure_code_survives_without_private_messages():
    from core.home.tools import HomeToolRegistry
    answer=HomeToolRegistry.answer({'status':'UNKNOWN','result':{'code':'SDK_EXECUTION_UNCERTAIN',
        'sdk_error_code':19,'message':'private value','subErrors':{'private-device-id':'secret'}}},'request01')
    assert answer['sdk_error_code']==19 and answer['physical_state_confirmed'] is False
    assert answer['code']=='SDK_EXECUTION_UNCERTAIN'
    assert 'private' not in repr(answer) and 'secret' not in repr(answer)


def test_sdk_subcodes_stay_numeric_without_dropping_uncertain_status():
    from core.home.tools import HomeToolRegistry
    answer=HomeToolRegistry.answer({'status':'UNKNOWN','result':{
        'code':'SDK_EXECUTION_UNCERTAIN','sdk_error_code':19,
        'sdk_error_domain':'UNKNOWN','sdk_sub_error_codes':[1907,1907,1914],
        'sdk_unwrapped_error_code':7,'sdk_unwrapped_error_domain':'HOME',
        'sdk_sub_error_codes_truncated':True,
        'subErrors':{'private-device-id':{'message':'secret'}}}},'request01')
    assert answer['status']=='UNKNOWN' and answer['physical_state_confirmed'] is False
    assert answer['sdk_sub_error_codes']==[1907,1914]
    assert answer['sdk_unwrapped_error_code']==7
    assert answer['sdk_error_domain']=='UNKNOWN'
    assert answer['sdk_sub_error_codes_truncated'] is True
    assert 'private-device-id' not in repr(answer) and 'secret' not in repr(answer)


@pytest.mark.parametrize('subcodes',[[True],[None],['secret'],[-1],[2147483648],list(range(17)),{'device-id':7}])
def test_invalid_sdk_subcodes_do_not_reach_callers(subcodes):
    from core.home.tools import HomeToolRegistry
    answer=HomeToolRegistry.answer({'status':'UNKNOWN','result':{
        'sdk_sub_error_codes':subcodes,'sdk_error_domain':'private/device-id',
        'sdk_unwrapped_error_code':True,'sdk_unwrapped_error_domain':'secret query=value'}},'request01')
    assert answer['status']=='UNKNOWN'
    assert not any(k.startswith('sdk_') for k in answer)


@pytest.mark.parametrize('connection', ['OFFLINE', 'UNKNOWN'])
def test_connectivity_blocks_advertised_supported_power_before_dispatch(tmp_path, connection):
    r = registry(tmp_path)
    r.transport.catalog.return_value['devices'][0]['device_capabilities'][0]['power_connectivity'] = connection
    assert r.execute('lens', 'on', 'request01')['code'] == 'DEVICE_OFFLINE_OR_UNKNOWN'
    r.transport.request.assert_not_called()
    assert not r.ledger_path.exists()


@pytest.mark.parametrize('name', ['컴퓨터', '환풍기', '거실', '청소기', 'Mi Robot Vacuum'])
def test_reserved_devices_cannot_enter_general_manifest(name):
    value = config()
    value['targets'][name] = value['targets'].pop('lens')
    with pytest.raises(ValueError): validate_device_targets(value)


@pytest.mark.parametrize('text', ['컴퓨터 꺼줘', '/home 컴퓨터 off', '/home 환풍기 on',
                                 'TV 켜줘?', 'TV 켜고 사운드바 꺼줘', 'TV 켜줘라고 말해'])
def test_ambiguous_and_reserved_requests_are_not_device_actions(text):
    assert parse_device_request(text) is None


def test_gateway_separate_approval_and_type_qualification(tmp_path):
    source = Path(__file__).resolve().parents[1]/'integrations/google_home/nas/gateway.py'
    spec = importlib.util.spec_from_file_location('rio_candidate_gateway', source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    tmp_path.chmod(0o700)
    for name in ['bridge.token', 'rio.token']:
        (tmp_path/name).write_text('x'*40)
    (tmp_path/'policy.json').write_text(json.dumps({'controls_enabled': True, 'allowed_aliases': []}))
    g = module.Gateway(tmp_path)
    g.ws = Mock(); g.ws.send = Mock()
    g.mode = 'READY'; g.permission = 'GRANTED'; g.session = 'test'
    g.heartbeat_at = g.snapshot_at = time.monotonic(); g.catalog = catalog()['devices']
    request = {'idempotency_key': 'request01', 'operation': 'device_command', 'alias': ALIAS,
               'arguments': {'device_type': 'OnOffPluginUnitDevice', 'command': 'on'}}
    code, result = asyncio.run(g.command(request))
    assert code == 403 and result['code'] == 'CONTROL_NOT_APPROVED'
    assert g.db.execute('SELECT COUNT(*) FROM jobs').fetchone()[0] == 0
    g.policy['device_controls_enabled'] = True
    g.policy['device_bindings'] = {ALIAS: config()['targets']['lens']}
    g.catalog[0]['device_control_binding']['device_type'] = 'GoogleTVDevice'
    code, result = asyncio.run(g.command(request))
    assert code == 422 and result['code'] == 'UNSUPPORTED_COMMAND'
    assert g.db.execute('SELECT COUNT(*) FROM jobs').fetchone()[0] == 0
    g.ws.send.assert_not_called()
    g.db.close()


def test_gateway_persists_intent_before_send_and_restart_never_resends(tmp_path):
    source = Path(__file__).resolve().parents[1]/'integrations/google_home/nas/gateway.py'
    spec = importlib.util.spec_from_file_location('rio_gateway_durability', source)
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    for name in ['bridge.token', 'rio.token']: (tmp_path/name).write_text('x'*40)
    (tmp_path/'policy.json').write_text(json.dumps({'controls_enabled': True, 'allowed_aliases': [],
        'device_controls_enabled': True, 'device_bindings': {ALIAS: config()['targets']['lens']}}))
    g = module.Gateway(tmp_path)
    g.mode = 'READY'; g.permission = 'GRANTED'; g.session = 'test'
    g.heartbeat_at = g.snapshot_at = time.monotonic(); g.catalog = catalog()['devices']
    g.ws = Mock()
    async def inspect_before_send(_):
        assert g.db.execute('SELECT status FROM jobs WHERE id=?', ('request01',)).fetchone()[0] == 'DISPATCHED'
    g.ws.send = AsyncMock(side_effect=inspect_before_send)
    request = {'idempotency_key': 'request01', 'operation': 'device_command', 'alias': ALIAS,
               'arguments': {'device_type': 'OnOffPluginUnitDevice', 'command': 'on'}}
    assert asyncio.run(g.command(request))[0] == 202
    assert asyncio.run(g.command(request))[1]['replayed'] is False
    assert g.ws.send.call_count == 1
    g.db.close()
    resumed = module.Gateway(tmp_path)
    assert asyncio.run(resumed.command(request))[1]['status'] == 'UNKNOWN'
    assert resumed.ws is None
    resumed.db.close()
