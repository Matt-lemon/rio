"""Local NAS web console using existing home policies and durable command ledgers."""
from __future__ import annotations

from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hmac
import json
from pathlib import Path
import re
import secrets

from .device_catalog import EXCLUDED_NAMES, display_name
from .device_controls import registry_from_environment as device_registry
from .main_adapter import registry_from_environment as light_registry
from .tools import LocalHomeTransport

STATIC = Path(__file__).with_name("web")
WEB_DEVICE_TARGETS = frozenset({"TV", "에어컨", "사운드바"})
REQUEST_ID = re.compile(r"[A-Za-z0-9_-]{8,100}")


def reported_power(item: dict) -> bool | None:
    values = [s.get("on") for s in item.get("state", [])
              if isinstance(s, dict) and s.get("trait") == "OnOff" and type(s.get("on")) is bool]
    return values[0] if values and len(set(values)) == 1 else None


class HomeConsole:
    def __init__(self, transport=None, lights=light_registry, devices=device_registry):
        self.transport = transport or LocalHomeTransport(Path.home()/"google-home-bridge/state/gateway/rio.token")
        self.lights, self.devices = lights, devices

    def snapshot(self) -> dict:
        catalog = self.transport.catalog()
        bridge = catalog.get("bridge", {})
        ready = (bridge.get("available") is True and bridge.get("permission") == "GRANTED"
                 and bridge.get("bridge_state") == "READY")
        result = {"ready": ready, "source": "Google Home", "devices": [],
                  "pc_controls_ready": False, "physical_state_confirmed": False}
        if not ready:
            return result
        lights = devices = None
        try:
            lights, devices = self.lights(), self.devices()
            light_aliases = {b["alias"]: name for name, b in lights.targets.items()}
            for item in catalog.get("devices", []):
                name = item.get("name")
                if name in EXCLUDED_NAMES:
                    continue
                commands, kind, note = [], "observed", "상태 조회"
                light_name = light_aliases.get(item.get("alias"))
                if light_name is not None and light_name not in {"컴퓨터", "환풍기", "청소기", "Mi Robot Vacuum"}:
                    name = light_name
                    kind, note = "light", "전등"
                    for command in ("on", "off"):
                        plan = lights.plan(name, command == "on")
                        if lights.allow_execution and plan.get("ready_for_execution") is True:
                            commands.append(command)
                elif name in WEB_DEVICE_TARGETS and name in devices.targets:
                    kind, note = "device", "등록 기기"
                    for command in devices.targets[name]["allowed_commands"]:
                        plan = devices.plan(name, command)
                        if devices.configuration["enabled"] and plan.get("ready_for_execution") is True:
                            commands.append(command)
                elif name == "컴퓨터":
                    note = "자동 제어 연결 준비 중"
                elif name == "lens":
                    note = "연결된 기기의 용도 확인 필요"
                result["devices"].append({"name": display_name(name), "kind": kind,
                    "on": reported_power(item), "commands": commands, "note": note})
        finally:
            if lights is not None:
                lights.close()
            if devices is not None:
                devices.close()
        return result

    def execute(self, data: dict) -> dict:
        if (not isinstance(data, dict) or set(data) != {"kind", "target", "command", "request_id"}
                or not isinstance(data["request_id"], str) or not REQUEST_ID.fullmatch(data["request_id"])
                or not isinstance(data["target"], str) or not isinstance(data["command"], str)):
            return {"status": "REJECTED", "code": "INVALID_REQUEST"}
        name, command = data["target"], data["command"]
        registry = None
        try:
            if data["kind"] == "light" and command in {"on", "off"}:
                if name in {"컴퓨터", "환풍기", "청소기", "Mi Robot Vacuum", "lens"}:
                    return {"status": "REJECTED", "code": "UNREGISTERED_TARGET"}
                registry = self.lights()
                if name not in registry.targets:
                    return {"status": "REJECTED", "code": "UNREGISTERED_TARGET"}
                return registry.call("home_set_light", {"target": name, "on": command == "on",
                                                       "idempotency_key": data["request_id"]})
            if data["kind"] == "device" and name in WEB_DEVICE_TARGETS:
                registry = self.devices()
                return registry.execute(name, command, data["request_id"])
            return {"status": "REJECTED", "code": "UNREGISTERED_TARGET"}
        finally:
            if registry is not None:
                registry.close()

    def result(self, key: str) -> dict:
        if not REQUEST_ID.fullmatch(key):
            return {"status": "REJECTED", "code": "INVALID_REQUEST"}
        registry = self.lights()
        try:
            # Both home registries use the same ledger; unknown IDs never reach the gateway.
            return registry.call("home_get_result", {"idempotency_key": key})
        finally:
            registry.close()


def public_answer(answer: dict) -> dict:
    status = answer.get("status")
    return {"status": status if status in {"PREVIEW", "SUCCEEDED", "REJECTED", "DISPATCHED", "UNKNOWN"} else "UNKNOWN",
            "physical_state_confirmed": False}


class HomeWebServer(ThreadingHTTPServer):
    daemon_threads = True
    def __init__(self, port: int = 18445, backend=None):
        self.backend = backend or HomeConsole()
        self.web_token = secrets.token_urlsafe(32)
        super().__init__(("127.0.0.1", port), HomeWebHandler)


class HomeWebHandler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass  # No access logs containing cookies, identifiers or command paths.

    def setup(self):
        super().setup()
        self.connection.settimeout(8)

    def allowed_host(self):
        port = self.server.server_port
        return self.headers.get_all("Host", []) in ([f"127.0.0.1:{port}"], [f"localhost:{port}"])

    def authenticated(self):
        try:
            if len(self.headers.get_all("Cookie", [])) != 1:
                return False
            cookie = SimpleCookie(self.headers.get("Cookie", ""))
            value = cookie.get("rio_home_session")
            return value is not None and hmac.compare_digest(value.value, self.server.web_token)
        except Exception:
            return False

    def respond(self, status, body, content_type="application/json; charset=utf-8", session=False):
        if not isinstance(body, bytes):
            body = json.dumps(body, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'")
        if session:
            self.send_header("Set-Cookie", f"rio_home_session={self.server.web_token}; HttpOnly; SameSite=Strict; Path=/")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if not self.allowed_host():
            return self.respond(403, {"error": "LOCAL_HOST_REQUIRED"})
        files = {"/": ("index.html", "text/html; charset=utf-8"),
                 "/app.js": ("app.js", "text/javascript; charset=utf-8"),
                 "/style.css": ("style.css", "text/css; charset=utf-8")}
        if self.path in files:
            filename, kind = files[self.path]
            return self.respond(200, (STATIC/filename).read_bytes(), kind, session=self.path == "/")
        if not self.authenticated():
            return self.respond(403, {"error": "SESSION_REQUIRED"})
        try:
            if self.path == "/api/session":
                return self.respond(200, {"token": self.server.web_token})
            if self.path == "/api/home":
                return self.respond(200, self.server.backend.snapshot())
            if self.path.startswith("/api/results/") and REQUEST_ID.fullmatch(self.path.removeprefix("/api/results/")):
                return self.respond(200, public_answer(self.server.backend.result(self.path.removeprefix("/api/results/"))))
            return self.respond(404, {"error": "NOT_FOUND"})
        except Exception:
            return self.respond(503, {"error": "HOME_UNAVAILABLE"})

    def do_POST(self):
        port = self.server.server_port
        origin = self.headers.get_all("Origin", [])
        token = self.headers.get_all("X-Rio-Web-Token", [])
        if (not self.allowed_host() or not self.authenticated()
                or origin not in ([f"http://127.0.0.1:{port}"], [f"http://localhost:{port}"])
                or origin[0] != "http://" + self.headers["Host"]
                or len(token) != 1 or not hmac.compare_digest(token[0], self.server.web_token)):
            return self.respond(403, {"error": "REQUEST_NOT_AUTHORIZED"})
        if self.path != "/api/commands":
            return self.respond(404, {"error": "NOT_FOUND"})
        if (self.headers.get("Content-Type") != "application/json"
                or len(self.headers.get_all("Content-Length", [])) != 1
                or self.headers.get("Transfer-Encoding") is not None):
            return self.respond(400, {"error": "INVALID_REQUEST"})
        try:
            size = int(self.headers["Content-Length"])
            if not 0 < size <= 4096:
                raise ValueError()
            data = json.loads(self.rfile.read(size))
        except (ValueError, UnicodeError):
            return self.respond(400, {"error": "INVALID_REQUEST"})
        try:
            return self.respond(200, public_answer(self.server.backend.execute(data)))
        except Exception:
            # The request may already have reached the bridge. Never auto-retry it.
            return self.respond(200, {"status": "UNKNOWN", "physical_state_confirmed": False})


def run_home_web(port=18445):
    if type(port) is not int or not 1024 <= port <= 65535:
        raise ValueError("Invalid web port")
    with HomeWebServer(port) as server:
        print(f"Rio Home: http://127.0.0.1:{server.server_port}", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
