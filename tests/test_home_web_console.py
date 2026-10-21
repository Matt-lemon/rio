import http.client
import json
from threading import Thread
from unittest.mock import Mock

import pytest

from core.home.web_console import HomeConsole, HomeWebServer


@pytest.fixture
def server():
    backend = Mock()
    backend.snapshot.return_value = {"ready": True, "devices": [], "physical_state_confirmed": False}
    backend.execute.return_value = {"status": "SUCCEEDED", "token": "remote-secret", "alias": "private-id"}
    value = HomeWebServer(0, backend)
    thread = Thread(target=value.serve_forever, daemon=True)
    thread.start()
    yield value, backend
    value.shutdown()
    value.server_close()
    thread.join()


def request(server, method, path, body=None, headers=None):
    c = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=3)
    c.request(method, path, body=body, headers=headers or {})
    r = c.getresponse()
    result = r.status, dict(r.getheaders()), r.read()
    c.close()
    return result


def login(server):
    status, headers, _ = request(server, "GET", "/")
    assert status == 200
    cookie = headers["Set-Cookie"].split(";", 1)[0]
    status, _, body = request(server, "GET", "/api/session", headers={"Cookie": cookie})
    assert status == 200
    return {"Cookie": cookie, "X-Rio-Web-Token": json.loads(body)["token"],
            "Origin": f"http://127.0.0.1:{server.server_port}", "Content-Type": "application/json"}


def command():
    return {"kind": "light", "target": "거실", "command": "off", "request_id": "request_001"}


def test_reading_page_and_inventory_never_dispatches(server):
    s, backend = server
    headers = login(s)
    status, h, body = request(s, "GET", "/api/home", headers=headers)
    assert status == 200 and json.loads(body)["physical_state_confirmed"] is False
    assert h["Cache-Control"] == "no-store"
    assert "frame-ancestors 'none'" in h["Content-Security-Policy"]
    backend.execute.assert_not_called()


@pytest.mark.parametrize("change", ["no_cookie", "wrong_token", "foreign_origin", "foreign_host", "wrong_type"])
def test_untrusted_browser_request_never_dispatches(server, change):
    s, backend = server
    headers = login(s)
    if change == "no_cookie":
        headers.pop("Cookie")
    elif change == "wrong_token":
        headers["X-Rio-Web-Token"] = "incorrect"
    elif change == "foreign_origin":
        headers["Origin"] = "https://example.org"
    elif change == "foreign_host":
        headers["Host"] = f"attacker.example:{s.server_port}"
    else:
        headers["Content-Type"] = "text/plain"
    status, _, _ = request(s, "POST", "/api/commands", json.dumps(command()), headers)
    assert status in (400, 403)
    backend.execute.assert_not_called()


def test_dns_rebinding_host_cannot_get_session(server):
    s, backend = server
    assert request(s, "GET", "/", headers={"Host": "attacker.example"})[0] == 403
    backend.execute.assert_not_called()


def test_explicit_action_uses_same_request_id_and_hides_credentials(server):
    s, backend = server
    status, _, body = request(s, "POST", "/api/commands", json.dumps(command()), login(s))
    assert status == 200
    backend.execute.assert_called_once_with(command())
    assert json.loads(body) == {"status": "SUCCEEDED", "physical_state_confirmed": False}
    assert b"remote-secret" not in body and b"private-id" not in body


def test_ambiguous_dispatch_never_retries_and_result_reads_are_get_only(server):
    s, backend = server
    backend.execute.side_effect = TimeoutError("private credentials")
    headers = login(s)
    _, _, body = request(s, "POST", "/api/commands", json.dumps(command()), headers)
    assert json.loads(body)["status"] == "UNKNOWN"
    backend.execute.assert_called_once()
    backend.result.return_value = {"status": "UNKNOWN"}
    assert request(s, "GET", "/api/results/request_001", headers=headers)[0] == 200
    backend.execute.assert_called_once()


@pytest.mark.parametrize("payload", [b"invalid-json", b"{}" * 3000])
def test_invalid_body_does_not_reach_controller(server, payload):
    s, backend = server
    assert request(s, "POST", "/api/commands", payload, login(s))[0] == 400
    backend.execute.assert_not_called()


@pytest.mark.parametrize("name", ["컴퓨터", "환풍기", "청소기", "Mi Robot Vacuum", "lens"])
@pytest.mark.parametrize("kind", ["light", "device"])
def test_reserved_or_unidentified_load_never_creates_control_registry(name, kind):
    lights, devices = Mock(), Mock()
    backend = HomeConsole(Mock(), lights, devices)
    assert backend.execute({**command(), "target": name, "kind": kind})["status"] == "REJECTED"
    lights.assert_not_called()
    devices.assert_not_called()


def test_lamp_alias_maps_to_reviewed_name_and_snapshot_is_read_only():
    transport, lamp, device = Mock(), Mock(), Mock()
    transport.catalog.return_value = {"bridge": {"available": True, "permission": "GRANTED", "bridge_state": "READY"},
        "devices": [{"alias": "lamp1", "name": "Mi LED 탁상 램프", "state": [{"trait": "OnOff", "on": False}]},
                    {"alias": "pc1", "name": "컴퓨터", "state": []}, {"name": "청소기"}, {"name": "lens"}]}
    lamp.targets = {"탁상등": {"alias": "lamp1"}}
    lamp.allow_execution = True
    lamp.plan.return_value = {"ready_for_execution": True}
    device.targets = {}
    backend = HomeConsole(transport, lambda: lamp, lambda: device)
    result = backend.snapshot()
    assert result["devices"][0]["name"] == "탁상등"
    assert result["devices"][0]["commands"] == ["on", "off"]
    assert result["devices"][1]["commands"] == []
    assert result["devices"][2]["name"] == "lens" and result["devices"][2]["commands"] == []
    lamp.call.assert_not_called()
    device.execute.assert_not_called()


def test_stale_inventory_never_becomes_actionable():
    transport, lights, devices = Mock(), Mock(), Mock()
    transport.catalog.return_value = {"bridge": {"available": False}, "devices": [{"name": "거실", "token": "secret"}]}
    result = HomeConsole(transport, lights, devices).snapshot()
    assert result["devices"] == [] and result["ready"] is False
    lights.assert_not_called()
    devices.assert_not_called()
