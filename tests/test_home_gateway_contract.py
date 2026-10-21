"""NAS gateway protocol contract. No real sockets, SDK or appliance calls."""
import asyncio
import hashlib
import importlib.util
import json
from pathlib import Path
import time
from unittest.mock import AsyncMock

import pytest

SOURCE = Path(__file__).resolve().parents[1]/'integrations/google_home/nas/gateway.py'
ALIAS = 'dev_'+'a'*24


def load():
    spec = importlib.util.spec_from_file_location('rio_gateway_contract', SOURCE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def gateway(tmp_path):
    module = load()
    tmp_path.chmod(0o700)
    for name in ('bridge.token', 'rio.token'):
        path=tmp_path/name
        path.write_text('synthetic-token-1234567890');path.chmod(0o600)
    policy={'controls_enabled': True, 'allowed_aliases': [ALIAS],
            'device_controls_enabled': True, 'device_bindings': {ALIAS: {
                'expected_name': '에어컨', 'device_type': 'OnOffPluginUnitDevice',
                'allowed_commands': ['on', 'off']}}}
    (tmp_path/'policy.json').write_text(json.dumps(policy))
    instance=module.Gateway(tmp_path)
    instance.ws=type('WS', (), {'send': AsyncMock()})()
    instance.session='synthetic-session'
    instance.mode='READY';instance.permission='GRANTED'
    instance.heartbeat_at=instance.snapshot_at=time.monotonic()
    instance.catalog=[{'alias': ALIAS, 'name': '에어컨', 'lighting': True,
        'commands': {'on': True, 'off': True},
        'device_control_binding': {'status': 'SELECTED_EXPLICIT_TYPE',
            'device_type': 'OnOffPluginUnitDevice', 'allowed_commands': {'on': True, 'off': True}},
        'device_capabilities': [{'device_type': 'OnOffPluginUnitDevice',
                                'supported_commands': {'on': True, 'off': True}}]}]
    yield instance
    instance.db.close()


def request(action='on'):
    return {'idempotency_key': 'gateway-contract-01', 'operation': 'device_command',
            'alias': ALIAS, 'arguments': {'device_type': 'OnOffPluginUnitDevice', 'command': action}, 'ttl_s': 20}


def test_status_identifies_exact_executing_source_and_protocol(gateway):
    value=gateway.status()
    assert value['gateway_revision']==hashlib.sha256(SOURCE.read_bytes()).hexdigest()
    assert value['protocol_version']==1
    assert value['supported_operations']==['get_state', 'set_power', 'device_command']
    assert value['device_controls_enabled'] is True
    assert not any(x in json.dumps(value) for x in ('synthetic-token', 'synthetic-session', ALIAS))
    gateway.mode='OFFLINE'
    assert gateway.status()['device_controls_enabled'] is False
    gateway.ws.send.assert_not_awaited()


@pytest.mark.parametrize('action', ['on', 'off'])
def test_aircon_power_contract_persists_before_single_send(gateway, action):
    async def inspect(raw):
        packet=json.loads(raw)
        assert packet['operation']=='device_command'
        assert packet['arguments']==request(action)['arguments']
        assert packet['session']=='synthetic-session'
        assert gateway.db.execute('SELECT status FROM jobs').fetchone()[0]=='DISPATCHED'
    gateway.ws.send.side_effect=inspect
    code,answer=asyncio.run(gateway.command(request(action)))
    assert code==202 and answer['status']=='DISPATCHED'
    assert asyncio.run(gateway.command(request(action)))[0]==200
    gateway.ws.send.assert_awaited_once()


@pytest.mark.parametrize('arguments', [
    {'device_type': 'OnOffPluginUnitDevice', 'command': 'cool'},
    {'device_type': 'RoomAirConditionerDevice', 'command': 'setCoolingSetpoint'},
    {'device_type': 'OnOffPluginUnitDevice', 'command': 'toggle'},
    {'device_type': 'OnOffPluginUnitDevice', 'command': 'on', 'temperature': 24},
    {'device_type': 'OnOffPluginUnitDevice', 'command': 'on', 'mode': 'cool'},
])
def test_temperature_mode_and_toggle_never_degrade_to_power(gateway, arguments):
    code,_=asyncio.run(gateway.command({**request(), 'arguments': arguments}))
    assert code==400
    assert gateway.db.execute('SELECT count(*) FROM jobs').fetchone()[0]==0
    gateway.ws.send.assert_not_awaited()


@pytest.mark.parametrize('mutation', ['permission', 'stale', 'policy', 'type', 'duplicate', 'allowed'])
def test_boundary_gates_reject_without_persisting_or_sending(gateway, mutation):
    if mutation=='permission': gateway.permission='DENIED'
    elif mutation=='stale': gateway.snapshot_at-=100
    elif mutation=='policy': gateway.policy['device_controls_enabled']=False
    elif mutation=='type': gateway.catalog[0]['device_control_binding']['device_type']='GoogleTVDevice'
    elif mutation=='duplicate': gateway.catalog[0]['device_capabilities']*=2
    elif mutation=='allowed': gateway.policy['device_bindings'][ALIAS]['allowed_commands']=[]
    code,_=asyncio.run(gateway.command(request()))
    assert code in (403,422,503)
    assert gateway.db.execute('SELECT count(*) FROM jobs').fetchone()[0]==0
    gateway.ws.send.assert_not_awaited()


def test_old_lighting_operation_remains_compatible(gateway):
    code,answer=asyncio.run(gateway.command({**request(), 'operation': 'set_power', 'arguments': {'on': True}}))
    assert code==202 and answer['status']=='DISPATCHED'
    gateway.ws.send.assert_awaited_once()


@pytest.mark.parametrize('error', [TimeoutError(), ConnectionError(), OSError()])
def test_uncertain_send_is_recorded_and_same_id_never_retries(gateway, error):
    gateway.ws.send.side_effect=error
    code,answer=asyncio.run(gateway.command(request()))
    assert code==202 and answer['status']=='UNKNOWN'
    assert gateway.pending is None
    code,answer=asyncio.run(gateway.command(request()))
    assert code==200 and answer['result']['code']=='SEND_UNCERTAIN'
    gateway.ws.send.assert_awaited_once()


def test_restart_preserves_old_unknown_and_prior_dispatched_evidence(gateway):
    original={'code': 'ORIGINAL_UNKNOWN', 'sdk_error_code': 19}
    partial={'code': 'PARTIAL_RECEIPT'}
    gateway.db.execute('INSERT INTO jobs VALUES (?,?,?,?,?)', ('old-unknown-01', 'd1', 'UNKNOWN', json.dumps(original), 0))
    gateway.db.execute('INSERT INTO jobs VALUES (?,?,?,?,?)', ('old-dispatch-01', 'd2', 'DISPATCHED', json.dumps(partial), 0))
    gateway.db.commit()
    recovered=load().Gateway(gateway.state)
    try:
        assert json.loads(recovered.db.execute('SELECT result FROM jobs WHERE id=?', ('old-unknown-01',)).fetchone()[0])==original
        row=recovered.db.execute('SELECT status,result FROM jobs WHERE id=?', ('old-dispatch-01',)).fetchone()
        assert row[0]=='UNKNOWN' and json.loads(row[1])['code']=='GATEWAY_RESTART_DO_NOT_RETRY'
        row=recovered.db.execute('SELECT previous_status,previous_result FROM job_recoveries').fetchone()
        assert row[0]=='DISPATCHED' and json.loads(row[1])==partial
        assert recovered.ws is None
    finally: recovered.db.close()


async def http_request(gateway, method, path, body=None, authenticated=True):
    reader=asyncio.StreamReader()
    data=json.dumps(body).encode() if body is not None else b''
    auth='Bearer '+gateway.rio_token if authenticated else 'Bearer invalid'
    reader.feed_data((f'{method} {path} HTTP/1.1\r\nAuthorization: {auth}\r\nContent-Length: {len(data)}\r\n\r\n').encode()+data)
    reader.feed_eof()
    class Writer:
        answer=b''
        def write(self, value): self.answer+=value
        async def drain(self): pass
        def close(self): pass
        async def wait_closed(self): pass
    writer=Writer()
    await gateway.local_http(reader, writer)
    head,content=writer.answer.split(b'\r\n\r\n', 1)
    return int(head.split(b' ')[1]),json.loads(content)


def test_actual_http_handler_accepts_device_contract_and_returns_same_job_id(gateway):
    assert asyncio.run(http_request(gateway, 'POST', '/v1/commands', request()))[0]==202
    code,answer=asyncio.run(http_request(gateway, 'GET', '/v1/jobs/gateway-contract-01'))
    assert code==200 and answer['id']=='gateway-contract-01'
    assert answer['status']=='DISPATCHED'
    gateway.ws.send.assert_awaited_once()


def test_http_auth_and_read_only_health_never_dispatch(gateway):
    code,_=asyncio.run(http_request(gateway, 'POST', '/v1/commands', request(), False))
    assert code==401
    code,answer=asyncio.run(http_request(gateway, 'GET', '/v1/status'))
    assert code==200 and answer['supported_operations'][-1]=='device_command'
    gateway.ws.send.assert_not_awaited()
