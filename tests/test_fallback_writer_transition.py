"""All audited appliance entry points, temporary ledgers and fake sends only."""
import asyncio
import importlib.util
import json
import os
from pathlib import Path
import socket
import sqlite3
import time

import pytest

from core.home import smartthings_fallback as fb
from core.home.device_controls import DeviceControlRegistry
from core.home.smartthings_direct import DirectLight
from core.home.smartthings_lights import LightBackup
from core.home.tools import HomeToolRegistry, CommandLedger
from test_fallback_independent_audit import policy_fixture


ROUTES = ('gh-light', 'gh-device', 'st-direct', 'st-backup', 'st-fallback',
          'core-gh', 'core-fallback', 'explicit-light', 'explicit-device',
          'explicit-direct', 'explicit-backup', 'web-light', 'web-device',
          'legacy-chat', 'registry-light', 'core-direct', 'core-light')


@pytest.fixture
def circuit(tmp_path, monkeypatch):
    monkeypatch.setenv('HOME', str(tmp_path))
    for key in tuple(os.environ):
        if key.startswith('RIO_'):
            monkeypatch.delenv(key)
    def forbidden(*args, **kwargs):
        raise AssertionError('No network or operating service access in transition tests')
    monkeypatch.setattr(socket.socket, 'connect', forbidden)
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1]/'services/rio-tools'))
    policy, primary, _ = policy_fixture(tmp_path, monkeypatch)
    monkeypatch.setenv('RIO_HOME_LEDGER', str(primary.ledger_path))
    profile = tmp_path/'.config/rio/pc-auth-provider.json'
    profile.write_text(json.dumps({'schema_version': 1, 'provider': 'oauth-cli',
                                  'cli_root': '/synthetic-unused', 'cli_sha256': 'a'*64}))
    profile.chmod(0o600)
    writes = []
    class Transport:
        control_enabled = True
        def verify_target(self): pass
        def switch(self): return {'value': 'off', 'timestamp': '2020-01-01T00:00:00Z'}
        def on(self):
            writes.append('st')
            return {'results': [{'status': 'UNKNOWN'}]}
        def off(self): raise AssertionError('Only synthetic on requested')
        def status(self):
            return {'available': True, 'permission': 'GRANTED', 'bridge_state': 'READY',
                    'controls_enabled': True}
        def catalog(self):
            devices = []
            for name in ('거실', '에어컨'):
                alias = primary.targets[name]['alias']
                kind = 'OnOffPluginUnitDevice'
                binding = {'status': 'SELECTED_EXPLICIT_TYPE', 'device_type': kind,
                           'allowed_commands': {'on': True, 'off': True}}
                devices.append({'alias': alias, 'name': name, 'lighting': name == '거실',
                    'commands': {'on': True, 'off': True}, 'control_binding': binding,
                    'device_control_binding': binding,
                    'device_capabilities': [{'device_type': kind,
                                             'supported_commands': {'on': True, 'off': True}}]})
            return {'bridge': self.status(), 'devices': devices}
        def result(self, key): return {'id': key, 'status': 'UNKNOWN'}
        def request(self, path, payload):
            assert path == '/v1/commands'
            writes.append('gh')
            return {'id': payload['idempotency_key'], 'status': 'UNKNOWN'}
        def set_power(self, alias, on, key):
            return self.request('/v1/commands', {'idempotency_key': key})
    transport = Transport()
    def light():
        binding = {k: primary.targets['거실'][k] for k in
                   ('alias', 'device_type', 'confirmed_physical_light')}
        return HomeToolRegistry(transport, {'거실': binding}, primary.ledger_path, True)
    def device():
        binding = {k: primary.targets['에어컨'][k] for k in
                   ('alias', 'device_type', 'family', 'expected_name', 'allowed_commands')}
        return DeviceControlRegistry(transport, {'schema_version': 1, 'enabled': True,
                                     'targets': {'에어컨': binding}}, primary.ledger_path)
    def direct():
        binding = {k: policy['targets']['에어컨'][k] for k in ('device_id', 'label', 'device_type')}
        binding['component'] = 'main'
        return DirectLight('에어컨', binding, transport,
                           tmp_path/'.local/state/rio/smartthings-direct/commands.sqlite3', True,
                           primary_ledger_path=primary.ledger_path)
    def backup():
        binding = {k: policy['targets']['거실'][k] for k in ('device_id', 'label', 'device_type')}
        binding.update(component='main', confirmed_same_physical_light=True)
        return LightBackup(binding, transport, tmp_path/'.local/state/rio/home-backup/commands.sqlite3',
                           primary.ledger_path, True)
    def invoke(route, key):
        from core.home import main_adapter, device_controls, smartthings_direct
        from core.home.smartthings_lights import handle_secondary_command
        from core.execution.adapters import Adapters
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(fb, 'VerifiedLightCli', lambda *a, **kw: transport)
            patch.setattr(smartthings_direct, 'from_environment', lambda _: direct())
            patch.setattr(device_controls, 'registry_from_environment', device)
            patch.setattr(main_adapter, 'registry_from_environment', light)
            registry = None
            try:
                if route == 'st-direct': return direct().set_power(True, key)
                if route == 'st-backup': return backup().set_power(True, key)
                if route == 'st-fallback': return fb.attempt('에어컨', 'on', key, primary)
                if route == 'explicit-direct':
                    return smartthings_direct.handle_direct_command('/home smartthings 에어컨 on', request_key=key)
                if route == 'explicit-backup':
                    return handle_secondary_command('/home backup 거실 on', lambda _: backup(), request_key=key)
                if route.startswith('web-'):
                    from core.home.web_console import HomeConsole
                    is_light = route == 'web-light'
                    return HomeConsole(transport, light, device).execute({
                        'kind': 'light' if is_light else 'device',
                        'target': '거실' if is_light else '에어컨', 'command': 'on', 'request_id': key})
                if route == 'legacy-chat':
                    from core.home.chat import run_chat
                    from types import SimpleNamespace
                    registry = light()
                    inputs = iter(('거실 불 켜줘', '/exit'))
                    module = SimpleNamespace(read_user_input=lambda prompt: next(inputs))
                    module.main = lambda: module.read_user_input('You: ')
                    return run_chat(module, registry)
                if route == 'registry-light':
                    from rio_tools.registry import Registry
                    from rio_tools.adapters import AdapterCatalog
                    registry = light()
                    store = Registry(tmp_path/'synthetic-tools-registry')
                    try:
                        adapters = AdapterCatalog(store)
                        adapters.home(registry)
                        # This approval applies only to the synthetic temporary registry.
                        adapters.approve(adapters.bindings['home_set_light'][2], ['device.control'])
                        return adapters.call('home_set_light', {'target': '거실', 'on': True}, key)
                    finally: store.close()
                if route == 'core-direct':
                    return Adapters(None).call('home.smartthings', {'target': '에어컨', 'action': 'on'}, key, None)
                if route.startswith('core-'):
                    if route == 'core-fallback':
                        registry = device()
                        registry.transport = type('Down', (), {
                            'status': lambda _: {'available': False, 'code': 'GATEWAY_UNREACHABLE'},
                            'catalog': lambda _: {'bridge': {'available': False}, 'devices': []}})()
                        patch.setattr(device_controls, 'registry_from_environment', lambda: registry)
                    if route == 'core-light':
                        return Adapters(None).home('home.light', {'target': '거실', 'action': 'on'}, key)
                    return Adapters(None).home('home.device.에어컨', {'action': 'on'}, key)
                registry = light() if route in ('gh-light', 'explicit-light') else device()
                if route == 'gh-light':
                    return registry.call('home_set_light', {'target': '거실', 'on': True, 'idempotency_key': key})
                if route == 'gh-device': return registry.execute('에어컨', 'on', key)
                if route == 'explicit-light':
                    return main_adapter.handle_home_command('/home 거실 on', lambda: registry, request_key=key)
                if route == 'explicit-device':
                    return device_controls.handle_device_command('에어컨 켜줘', lambda: registry, request_key=key)
                raise AssertionError(route)
            finally:
                if registry is not None: registry.close()
    return tmp_path, primary.ledger_path, writes, invoke


@pytest.mark.parametrize('first', ROUTES)
@pytest.mark.parametrize('second', ROUTES)
def test_every_appliance_entry_point_respects_other_route_unknown(circuit, first, second):
    root, primary, writes, invoke = circuit
    result = invoke(first, 'transition-first-01')
    assert len(writes) == 1, (first, result)
    coordinator = primary.with_name('dispatch-coordinator.sqlite3')
    with sqlite3.connect(coordinator) as db:
        original = db.execute('SELECT * FROM dispatches').fetchall()
    invoke(second, 'transition-second-02')
    invoke(first, 'transition-first-01')  # Same ID, including recovered Core paths.
    assert len(writes) == 1, (first, second)
    with sqlite3.connect(coordinator) as db:
        assert db.execute('SELECT * FROM dispatches').fetchall() == original


def load_gateway():
    source = Path(__file__).resolve().parents[1]/'integrations/google_home/nas/gateway.py'
    spec = importlib.util.spec_from_file_location('transition_gateway', source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize('phase', ['before-send', 'after-send'])
def test_gateway_uncertainty_and_restart_block_all_other_routes(circuit, phase):
    root, primary, writes, invoke = circuit
    module = load_gateway()
    state = root/'gateway'
    state.mkdir(mode=0o700)
    for name in ('bridge.token', 'rio.token'): (state/name).write_text('synthetic-token')
    alias = 'dev_'+'1'*24
    binding = {'alias': alias, 'device_type': 'OnOffPluginUnitDevice',
               'family': 'air_conditioner_power', 'expected_name': '에어컨', 'allowed_commands': ['on', 'off']}
    (state/'policy.json').write_text(json.dumps({'controls_enabled': True, 'device_controls_enabled': True,
                                               'device_bindings': {alias: binding}}))
    gateway = module.Gateway(state)
    class WS:
        async def send(self, text):
            packet = json.loads(text)
            assert gateway.db.execute('SELECT status FROM jobs WHERE id=?',
                                      (packet['id'],)).fetchone()[0] == 'DISPATCHED'
            if phase == 'after-send': writes.append('gateway')
            raise TimeoutError('synthetic send uncertainty')
    gateway.ws = WS()
    gateway.mode = 'READY'; gateway.permission = 'GRANTED'
    gateway.heartbeat_at = gateway.snapshot_at = time.monotonic()
    current = {'status': 'SELECTED_EXPLICIT_TYPE', 'device_type': binding['device_type'],
               'allowed_commands': {'on': True, 'off': True}}
    gateway.catalog = [{'alias': alias, 'name': '에어컨', 'device_control_binding': current,
                        'device_capabilities': [{'device_type': binding['device_type'],
                                                'supported_commands': {'on': True, 'off': True}}]}]
    class Transport:
        def catalog(self): return {'bridge': gateway.status(), 'devices': gateway.catalog}
        def request(self, path, payload): return asyncio.run(gateway.command(payload))[1]
        def result(self, key):
            row = gateway.db.execute('SELECT status FROM jobs WHERE id=?', (key,)).fetchone()
            return {'id': key, 'status': row[0] if row else 'UNKNOWN'}
    registry = DeviceControlRegistry(Transport(), {'schema_version': 1, 'enabled': True,
                                          'targets': {'에어컨': binding}}, primary)
    try:
        assert registry.execute('에어컨', 'on', 'gateway-unknown-01')['status'] == 'UNKNOWN'
        row = gateway.db.execute('SELECT * FROM jobs').fetchone()
        gateway.db.close()
        resumed = module.Gateway(state)
        assert resumed.db.execute('SELECT status FROM jobs').fetchone()[0] == 'UNKNOWN'
        assert asyncio.run(resumed.command({'idempotency_key': 'gateway-unknown-01',
            'operation': 'device_command', 'alias': alias, 'arguments': {
                'device_type': binding['device_type'], 'command': 'on'}, 'ttl_s': 20}))[1]['status'] == 'UNKNOWN'
        assert resumed.ws is None
        for route in ROUTES: invoke(route, 'after-gateway-'+route)
        assert len(writes) == (1 if phase == 'after-send' else 0)
        assert resumed.db.execute('SELECT count(*) FROM jobs').fetchone()[0] == 1
        assert row[0] == 'gateway-unknown-01'
        resumed.db.close()
    finally:
        registry.close()


@pytest.mark.parametrize('pending', ['DISPATCHING', 'DISPATCHED', 'UNKNOWN'])
def test_legacy_primary_migration_preserves_evidence_and_blocks_every_writer(circuit, pending):
    root, primary, writes, invoke = circuit
    ledger = CommandLedger(primary)
    ledger.reserve('pre-upgrade-01', {'operation': 'set_power', 'alias': 'synthetic', 'arguments': {'on': True}})
    ledger.record('pre-upgrade-01', {'status': pending, 'code': 'ORIGINAL_EVIDENCE'})
    original = ledger.db.execute('SELECT * FROM light_commands').fetchall()
    # Consistent SQLite backup/restore rehearsal, never an operating database.
    backup = root/'before-transition.sqlite3'
    with sqlite3.connect(backup) as destination: ledger.db.backup(destination)
    ledger.close()
    with sqlite3.connect(backup) as source, sqlite3.connect(primary) as destination:
        source.backup(destination)
    for route in ROUTES: invoke(route, 'legacy-'+route)
    assert writes == []
    with sqlite3.connect(primary) as db:
        if pending == 'DISPATCHED':
            assert db.execute('SELECT status FROM light_commands').fetchone()[0] == 'UNKNOWN'
            assert db.execute('SELECT previous_status,previous_answer FROM command_result_refreshes').fetchall() == [
                (original[0][2], original[0][3])]
        else:
            assert db.execute('SELECT * FROM light_commands').fetchall() == original
    with sqlite3.connect(backup) as db:
        assert db.execute('SELECT * FROM light_commands').fetchall() == original


@pytest.mark.parametrize('first', ROUTES)
def test_every_entry_point_holds_process_fence_against_all_other_routes(circuit, first):
    import multiprocessing
    from core.home.dispatch_guard import DispatchGuard
    root, primary, writes, invoke = circuit
    ctx = multiprocessing.get_context('fork')
    ready, release = ctx.Event(), ctx.Event()
    recv, send = ctx.Pipe(duplex=False)
    def worker():
        original = DispatchGuard.attempted
        def checkpoint(self):
            original(self)
            ready.set()
            assert release.wait(8)
        DispatchGuard.attempted = checkpoint
        try:
            invoke(first, 'process-first-01')
            send.send({'writes': len(writes)})
        except BaseException as error:
            send.send({'error': type(error).__name__})
        finally: send.close()
    process = ctx.Process(target=worker)
    process.start(); send.close()
    try:
        assert ready.wait(8), first
        for route in ROUTES: invoke(route, 'competing-'+route)
        assert writes == []
        release.set()
        assert recv.poll(8)
        assert recv.recv() == {'writes': 1}
        process.join(8)
        assert process.exitcode == 0
    finally:
        if process.is_alive(): process.kill(); process.join(8)
        recv.close()


def test_direct_constructor_default_uses_shared_primary_instead_of_a_private_fence(circuit):
    root, primary, writes, invoke = circuit
    ledger = CommandLedger(primary)
    ledger.reserve('implicit-primary-old', {'on': True})
    ledger.record('implicit-primary-old', {'status': 'UNKNOWN', 'code': 'ORIGINAL'})
    ledger.close()
    class Transport:
        control_enabled = True
        def verify_target(self): pass
        def switch(self): return {'value': 'off', 'timestamp': '2020-01-01T00:00:00Z'}
        def on(self): writes.append('unsafe'); return {}
    direct = DirectLight('에어컨', {'device_id': '12345678-1234-4234-8234-123456789abc',
                         'label': '에어컨', 'device_type': 'ZIGBEE', 'component': 'main'},
                         Transport(), root/'another/commands.sqlite3', True)
    assert direct.set_power(True, 'implicit-primary-new')['commands_sent'] == 0
    assert writes == []


@pytest.mark.parametrize('provider', ['smartthings-direct', 'home-backup'])
@pytest.mark.parametrize('pending', ['DISPATCHING', 'ACCEPTED', 'UNKNOWN'])
@pytest.mark.parametrize('off', [False, True])
def test_legacy_secondary_transition_keeps_both_tables_and_blocks_all_routes(circuit, provider, pending, off):
    from core.home.smartthings_pc import WakeLedger
    root, primary, writes, invoke = circuit
    path = root/'.local/state/rio'/provider/'commands.sqlite3'
    with WakeLedger(path) as ledger:
        reserve = ledger.reserve_shutdown if off else ledger.reserve
        reserve('12345678-1234-4234-8234-123456789abc', 'legacy-secondary-01')
        ledger.record('legacy-secondary-01', pending)
        original = {table: ledger.db.execute('SELECT * FROM '+table).fetchall()
                    for table in ('wake_requests', 'shutdown_requests')}
        backup = root/'secondary-backup.sqlite3'
        with sqlite3.connect(backup) as destination: ledger.db.backup(destination)
    for route in ROUTES: invoke(route, 'old-secondary-'+route)
    assert writes == []
    with sqlite3.connect(path) as db, sqlite3.connect(backup) as saved:
        for table, rows in original.items():
            assert db.execute('SELECT * FROM '+table).fetchall() == rows
            assert saved.execute('SELECT * FROM '+table).fetchall() == rows


@pytest.mark.parametrize('phase', ['PREPARED', 'ATTEMPTED'])
def test_coordinator_only_backup_restore_does_not_authorize_any_writer(circuit, phase):
    from core.home.dispatch_guard import DispatchGuard
    root, primary, writes, invoke = circuit
    with DispatchGuard(primary) as fence:
        assert fence.admit('coordinator-only-01', 'google-home', {'on': True}) is None
        if phase == 'ATTEMPTED': fence.attempted()
        original = fence.db.execute('SELECT * FROM dispatches').fetchall()
        backup = root/'coordinator-backup.sqlite3'
        with sqlite3.connect(backup) as destination: fence.db.backup(destination)
    coordinator = primary.with_name('dispatch-coordinator.sqlite3')
    with sqlite3.connect(backup) as source, sqlite3.connect(coordinator) as destination:
        source.backup(destination)
    for route in ROUTES: invoke(route, 'after-restore-'+route)
    assert writes == []
    with sqlite3.connect(coordinator) as db:
        assert db.execute('SELECT * FROM dispatches').fetchall() == original
