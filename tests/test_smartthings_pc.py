import io
import json
import os
from pathlib import Path
from unittest.mock import Mock
import urllib.error

import pytest

from core.home.smartthings_pc import (
    PcController, PcError, SmartThingsPcTransport, WakeLedger,
    handle_pc_command, load_binding, parse_pc_request, read_private,
)


DEVICE = "12345678-1234-4234-8234-123456789abc"
TOKEN = "test-secret-never-log-123456"


@pytest.fixture
def binding():
    return {"schema_version": 1, "enabled": True, "device_id": DEVICE,
            "label": "컴퓨터", "component": "main",
            "confirmed_pc_virtual_switch": True, "pc_host": "192.0.2.129"}


def fake_transport(state="off"):
    transport = Mock()
    transport.switch.return_value = {"value": state, "timestamp": "reported-time"}
    transport.on.return_value = {"results": [{"status": "ACCEPTED"}]}
    transport.off.return_value = {"results": [{"status": "ACCEPTED"}]}
    return transport


@pytest.mark.parametrize("text", ["컴퓨터 켜줘", "컴퓨터 켜 줘", "PC 켜주세요", "리오야 컴퓨터 켜줘", "/pc on"])
def test_explicit_wake_requests(text):
    assert parse_pc_request(text) == "on"


@pytest.mark.parametrize("text", ["컴퓨터 켤까?", "컴퓨터 켜줘?", "컴퓨터 켜줘라는 말", "컴퓨터 꺼줘?",
                                "에어컨 켜줘", "컴퓨터 켜지 마", "켜줘", "리오 컴퓨터 켜줘 그리고 꺼줘"])
def test_ambiguous_or_other_actions_never_wake(text):
    assert parse_pc_request(text) is None
    factory = Mock()
    assert handle_pc_command(text, factory) is None
    factory.assert_not_called()


@pytest.mark.parametrize("text", ["컴퓨터 상태", "컴퓨터 상태 알려줘", "컴퓨터 켜져 있어?", "/pc status"])
def test_status_requests(text):
    assert parse_pc_request(text) == "status"


def test_private_binding_rejects_unconfirmed_wrong_target_and_public_file(tmp_path, binding):
    path = tmp_path / "binding.json"
    path.write_text(json.dumps(binding)); path.chmod(0o600)
    assert load_binding(path) == binding
    for key, value in [("component", "Utility"), ("label", "에어컨"),
                       ("confirmed_pc_virtual_switch", False), ("pc_host", "127.0.0.1"),
                       ("pc_host", "8.8.8.8"), ("device_id", "invalid")]:
        altered = dict(binding, **{key: value}); path.write_text(json.dumps(altered))
        with pytest.raises((PcError, ValueError)):
            load_binding(path)
    path.write_text(json.dumps(binding)); path.chmod(0o644)
    if os.name == "posix":
        with pytest.raises(PcError): load_binding(path)


@pytest.mark.skipif(os.name != "posix", reason="POSIX private-file behavior")
def test_credentials_and_ledger_reject_symlinks(tmp_path, binding):
    target = tmp_path / "secret"; target.write_text(TOKEN); target.chmod(0o600)
    link = tmp_path / "link"; link.symlink_to(target)
    with pytest.raises(OSError): read_private(link)
    with pytest.raises(OSError): WakeLedger(link)


def test_only_pinned_main_on_can_be_sent_and_auth_is_private(binding):
    transport = SmartThingsPcTransport(binding, TOKEN)
    response = io.BytesIO(json.dumps({"results": [{"status": "ACCEPTED"}]}).encode())
    transport._opener = Mock()
    transport._opener.open.return_value = response
    transport.on()
    request = transport._opener.open.call_args.args[0]
    assert request.full_url == f"https://api.smartthings.com/v1/devices/{DEVICE}/commands"
    assert request.method == "POST"
    assert json.loads(request.data) == {"commands": [{"component": "main", "capability": "switch",
                                                      "command": "on", "arguments": []}]}
    assert request.get_header("Authorization") == "Bearer " + TOKEN
    for suffix, on in [("/status", True), ("/commands", False), ("/other", False)]:
        with pytest.raises(PcError): transport.request(suffix, on=on)


def test_remote_errors_and_redirects_do_not_expose_secret_or_resend(binding):
    transport = SmartThingsPcTransport(binding, TOKEN)
    transport._opener = Mock()
    transport._opener.open.side_effect = urllib.error.HTTPError("secret-url", 401, TOKEN, {}, None)
    with pytest.raises(PcError, match="^HTTP_401$"):
        transport.on()
    transport._opener.open.assert_called_once()
    from core.home.smartthings_pc import NoRedirect
    with pytest.raises(PcError, match="REDIRECT_REFUSED"):
        NoRedirect().redirect_request(None, None, 302, "", {}, "https://evil.invalid")


@pytest.mark.parametrize("device", [
    {"deviceId": DEVICE, "label": "Other", "components": []},
    {"deviceId": "other", "label": "컴퓨터", "components": []},
    {"deviceId": DEVICE, "label": "컴퓨터", "components": [{"id": "Utility", "capabilities": [{"id": "switch"}]}]},
])
def test_changed_binding_never_dispatches(binding, tmp_path, device):
    transport = SmartThingsPcTransport(binding, TOKEN)
    transport.request = Mock(return_value=device)
    controller = PcController(binding, transport, tmp_path / "ledger.db", lambda _: False)
    with pytest.raises(PcError): controller.wake()
    transport.request.assert_called_once_with()


def test_off_request_is_accepted_but_not_claimed_as_booted(binding, tmp_path):
    transport = fake_transport()
    controller = PcController(binding, transport, tmp_path / "ledger.db", lambda _: False)
    message = handle_pc_command("컴퓨터 켜줘", lambda: controller)
    assert "요청을 보냈어요" in message and "부팅 완료는 아직" in message
    transport.on.assert_called_once()
    with WakeLedger(tmp_path / "ledger.db") as ledger:
        assert ledger.db.execute("SELECT state FROM wake_requests").fetchone() == ("ACCEPTED",)
    if os.name == "posix":
        assert (tmp_path / "ledger.db").stat().st_mode & 0o077 == 0


def test_process_restart_and_concurrent_reservations_do_not_resend(binding, tmp_path):
    path = tmp_path / "ledger.db"
    with WakeLedger(path) as first, WakeLedger(path) as second:
        assert first.reserve(DEVICE)
        assert second.reserve(DEVICE) is None
    transport = fake_transport()
    result = PcController(binding, transport, path, lambda _: False).wake()
    assert result["result"] == "PENDING_DO_NOT_RETRY"
    transport.on.assert_not_called()


@pytest.mark.parametrize("error", [PcError("CONNECTION_FAILED"), PcError("HTTP_500"),
                                  PcError("INVALID_RESPONSE"), RuntimeError(TOKEN)])
def test_unknown_dispatch_survives_restart_without_secret_or_retry(binding, tmp_path, error):
    transport = fake_transport(); transport.on.side_effect = error
    path = tmp_path / "ledger.db"
    result = PcController(binding, transport, path, lambda _: False).wake()
    assert result["result"] == "UNKNOWN" and TOKEN not in json.dumps(result)
    later = fake_transport()
    assert PcController(binding, later, path, lambda _: False).wake()["result"] == "PENDING_DO_NOT_RETRY"
    later.on.assert_not_called()


@pytest.mark.parametrize("reply", [{}, {"results": []}, {"results": [{"status": "REJECTED"}]}])
def test_unverified_post_result_never_claims_accepted(binding, tmp_path, reply):
    transport = fake_transport(); transport.on.return_value = reply
    assert PcController(binding, transport, tmp_path / "ledger.db", lambda _: False).wake()["result"] == "UNKNOWN"


@pytest.mark.parametrize("state,reachable,expected", [("on", False, "ON_NOT_REACHABLE"),
                                                     ("on", True, "ALREADY_REACHABLE"),
                                                     ("off", True, "ALREADY_REACHABLE")])
def test_existing_on_or_reachable_pc_is_not_sent_on_again(binding, tmp_path, state, reachable, expected):
    transport = fake_transport(state)
    result = PcController(binding, transport, tmp_path / "ledger.db", lambda _: reachable).wake()
    assert result["result"] == expected and result["commands_sent"] == 0
    transport.on.assert_not_called()


@pytest.mark.parametrize("reported_switch", ["on", "off"])
def test_reachable_observation_resolves_previous_request_without_changing_reported_switch(binding, tmp_path, reported_switch):
    path = tmp_path / "ledger.db"; transport = fake_transport()
    PcController(binding, transport, path, lambda _: False).wake()
    transport.switch.return_value = {"value": reported_switch, "timestamp": "new-time"}
    observer = PcController(binding, transport, path, lambda _: True)
    status = observer.observe()
    assert status["pc_ssh_reachable"] and status["models_ready"] == "NOT_CHECKED"
    assert status["switch"] == reported_switch
    if reported_switch == "off":
        assert "서로 달라요" in handle_pc_command("컴퓨터 상태", lambda: observer)
    with WakeLedger(path) as ledger:
        assert ledger.reserve(DEVICE)
    assert transport.on.call_count == 1


def test_status_and_missing_configuration_never_write_to_device(binding, tmp_path):
    transport = fake_transport()
    controller = PcController(binding, transport, tmp_path / "ledger.db", lambda _: False)
    assert "꺼짐" in handle_pc_command("컴퓨터 상태", lambda: controller)
    transport.on.assert_not_called()
    def failed(): raise OSError(TOKEN)
    message = handle_pc_command("컴퓨터 켜줘", failed)
    assert TOKEN not in message and "확인할 수 없어요" in message


@pytest.mark.parametrize("text", ["컴퓨터 꺼줘", "리오야 컴퓨터 꺼 줘", "PC 종료해주세요", "/pc off"])
def test_explicit_shutdown_requests(text):
    assert parse_pc_request(text) == "off"


def test_shutdown_uses_pinned_main_off(binding):
    transport = SmartThingsPcTransport(binding, TOKEN)
    transport._opener = Mock()
    transport._opener.open.return_value = io.BytesIO(b'{"results":[{"status":"ACCEPTED"}]}')
    transport.off()
    request = transport._opener.open.call_args.args[0]
    assert request.full_url == f"https://api.smartthings.com/v1/devices/{DEVICE}/commands"
    assert json.loads(request.data) == {"commands": [{"component": "main", "capability": "switch",
                                                     "command": "off", "arguments": []}]}
    with pytest.raises(PcError): transport.request('/commands', command='toggle')


@pytest.mark.parametrize("readiness", ["BUSY", "UNVERIFIED", None])
def test_busy_or_unverified_shutdown_never_sends_off(binding, tmp_path, readiness):
    transport = fake_transport('on')
    c = PcController(binding, transport, tmp_path/'ledger.db', lambda _: True, lambda: readiness)
    assert c.shutdown()['commands_sent'] == 0
    transport.off.assert_not_called()


def test_shutdown_acceptance_is_not_completion_and_repeat_is_blocked(binding, tmp_path):
    transport = fake_transport('on'); path = tmp_path/'ledger.db'
    c = PcController(binding, transport, path, lambda _: True, lambda: 'IDLE')
    message = handle_pc_command('컴퓨터 꺼줘', lambda: c)
    assert '정상 종료 요청' in message and '완료는 아직' in message
    assert c.shutdown()['result'] == 'PENDING_DO_NOT_RETRY'
    transport.off.assert_called_once()
    with WakeLedger(path) as ledger:
        assert ledger.reserve(DEVICE) is None


def test_uncertain_shutdown_survives_restart_and_only_observation_resolves(binding, tmp_path):
    transport = fake_transport('on');transport.off.side_effect = TimeoutError(TOKEN)
    path=tmp_path/'ledger.db'
    c=PcController(binding, transport, path, lambda _: True, lambda: 'IDLE')
    assert c.shutdown()['result'] == 'UNKNOWN'
    later=fake_transport('on')
    c=PcController(binding, later, path, lambda _: True, lambda: 'IDLE')
    assert c.shutdown()['result'] == 'PENDING_DO_NOT_RETRY'
    later.off.assert_not_called()
    later.switch.return_value={'value':'off','timestamp':'later'}
    c.probe=lambda _: False
    assert c.shutdown()['result'] == 'OFF_NOT_REACHABLE'
    with WakeLedger(path) as ledger:
        assert ledger.db.execute('SELECT state FROM shutdown_requests').fetchone()[0] == 'OBSERVED_OFF_UNREACHABLE'
        assert ledger.reserve(DEVICE)


def test_add_shutdown_table_preserves_legacy_wake_history(binding, tmp_path):
    import sqlite3
    path=tmp_path/'ledger.db'
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE wake_requests (id TEXT PRIMARY KEY,target TEXT,state TEXT,created REAL)')
        db.execute('INSERT INTO wake_requests VALUES (?,?,?,?)', ('old',DEVICE,'OBSERVED_REACHABLE',1))
    path.chmod(0o600)
    with WakeLedger(path) as ledger:
        assert ledger.db.execute('SELECT * FROM wake_requests').fetchone() == ('old',DEVICE,'OBSERVED_REACHABLE',1)
        assert ledger.reserve_shutdown(DEVICE)
