import asyncio
import importlib.util
import json
from pathlib import Path
from unittest.mock import Mock,AsyncMock

import pytest

from core import browser_transport as mode


def test_modes_have_separate_profiles_sockets_and_no_hidden_security_flags():
    assert len({mode.profile(m) for m in mode.MODES})==3
    assert len({mode.endpoint(m) for m in mode.MODES})==3
    managed=mode.mcp_arguments('managed',Path('/private/proof'))
    assert '--sandbox' in managed and '--extension' not in managed and '--cdp-endpoint' not in managed
    cdp=mode.mcp_arguments('cdp',Path('/private/proof'))
    assert mode.CDP_ENDPOINT=='http://127.0.0.1:9337'
    assert '--cdp-endpoint' in cdp and '--extension' not in cdp
    for m in mode.MODES:
        args=mode.mcp_arguments(m,Path('/private/proof'))
        assert '--no-sandbox' not in args and '--ignore-https-errors' not in args and '--storage-state' not in args


def test_no_selection_keeps_original_extension_and_private_config_selects_mode(tmp_path,monkeypatch):
    monkeypatch.delenv('RIO_BROWSER_TRANSPORT',raising=False)
    monkeypatch.setattr(mode,'SELECTION',tmp_path/'selection.json')
    assert mode.selected_mode()=='extension'
    mode.write_selection('managed')
    assert mode.selected_mode()=='managed'
    assert mode.SELECTION.stat().st_mode&0o777==0o600


def test_wrong_config_permissions_or_symlink_never_fall_back_silently(tmp_path,monkeypatch):
    monkeypatch.delenv('RIO_BROWSER_TRANSPORT',raising=False)
    p=tmp_path/'selection';monkeypatch.setattr(mode,'SELECTION',p)
    p.write_text(json.dumps({'schema_version':1,'transport':'cdp'}));p.chmod(0o644)
    with pytest.raises(ValueError):mode.selected_mode()
    p.unlink();p.symlink_to(tmp_path/'missing')
    with pytest.raises(OSError):mode.selected_mode()


@pytest.mark.parametrize('value',['http://example.org','http://127.0.0.1:9999','personal',None])
def test_endpoint_cannot_be_overridden_by_transport(value):
    with pytest.raises(ValueError):mode.mode_name(value)


def table(address):
    return 'header\n 0: '+address+':2479 00000000:0000 0A 0 0 0 1000 0 12345\n'


def test_only_local_listeners_are_accepted():
    assert mode.listener_inodes([table('0100007F')])==['12345']
    for address in ['00000000','EA3200C0','00000000000000000000000000000000']:
        with pytest.raises(ValueError,match='LOOPBACK'):mode.listener_inodes([table(address)])


def test_cdp_requires_receipt_for_the_dedicated_profile(monkeypatch):
    monkeypatch.setattr(mode,'read_private_json',lambda _: {'pid':100,'start':'1','profile':'/personal/browser','port':9337})
    with pytest.raises(ValueError,match='USER_CDP_LAUNCH_REQUIRED'):mode.cdp_identity()


def launcher():
    source=Path(__file__).resolve().parents[1]/'tools/rio-runtime/nas/rio-browser-direct.py'
    spec=importlib.util.spec_from_file_location('rio_direct_launcher_test',source)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    return module


def test_busy_debug_port_never_launches_or_attaches_another_browser(tmp_path,monkeypatch):
    app=launcher();monkeypatch.setattr(app,'prepare',lambda:{})
    monkeypatch.setattr(app,'profile',lambda _:tmp_path)
    monkeypatch.setattr(app,'cdp_identity',Mock(side_effect=ValueError('not our browser')))
    check=Mock();check.__enter__=Mock(return_value=check);check.__exit__=Mock(return_value=False)
    check.bind.side_effect=OSError('busy')
    monkeypatch.setattr(app.socket,'socket',lambda:check)
    launched=Mock();monkeypatch.setattr(app.subprocess,'Popen',launched)
    with pytest.raises(OSError):app.cdp_browser()
    launched.assert_not_called()


def test_failure_to_verify_new_mode_never_switches_current_selection(monkeypatch):
    app=launcher();monkeypatch.setattr(app,'prepare',lambda:{})
    monkeypatch.setattr(app,'RUNTIME',Path(__file__))
    monkeypatch.setattr(app.subprocess,'run',lambda *a,**k:Mock(stdout='active'))
    import core.research_browser as client
    monkeypatch.setattr(client,'request',lambda _:{'ok':False,'status':'STARTING_DEDICATED_BROWSER'})
    monkeypatch.setattr(app.time,'sleep',lambda _:None)
    selected=Mock();monkeypatch.setattr(app,'write_selection',selected)
    with pytest.raises(ValueError,match='기존 선택'):app.start('managed')
    selected.assert_not_called()


def test_new_selection_preserves_shared_legacy_directory(tmp_path,monkeypatch):
    monkeypatch.delenv('RIO_BROWSER_TRANSPORT',raising=False)
    shared=tmp_path/'shared';shared.mkdir(mode=0o775);shared.chmod(0o775)
    legacy=shared/'browser-transport.json'
    legacy.write_text(json.dumps({'schema_version':1,'transport':'extension'}));legacy.chmod(0o600)
    monkeypatch.setattr(mode,'LEGACY_SELECTION',legacy)
    monkeypatch.setattr(mode,'SELECTION',tmp_path/'private'/'transport.json')
    assert mode.selected_mode()=='extension'
    mode.write_selection('managed')
    assert mode.selected_mode()=='managed'
    assert shared.stat().st_mode&0o777==0o775
    assert json.loads(legacy.read_text())['transport']=='extension'
    assert not list(mode.SELECTION.parent.glob('.transport-*'))


def test_dead_local_browser_lock_is_left_for_chromium_to_recover(tmp_path,monkeypatch):
    import os,socket
    monkeypatch.setattr(mode,'profile',lambda _:tmp_path)
    lock=tmp_path/'SingletonLock'
    lock.symlink_to(socket.gethostname()+'-999999999')
    assert not mode.profile_in_use('managed') and lock.is_symlink()
    lock.unlink();lock.symlink_to(socket.gethostname()+'-'+str(os.getpid()))
    assert mode.profile_in_use('managed')
    lock.unlink();lock.symlink_to('another-host-999999999')
    assert mode.profile_in_use('managed')


def test_stale_socket_cleanup_never_removes_live_socket(tmp_path):
    import socket
    app=launcher();path=tmp_path/'browser.sock'
    with socket.socket(socket.AF_UNIX,socket.SOCK_STREAM) as server:
        server.bind(str(path));path.chmod(0o600);server.listen()
        with pytest.raises(ValueError,match='사용 중'):app.remove_stale_endpoint(path)
        assert path.exists()
    app.remove_stale_endpoint(path)
    assert not path.exists()
