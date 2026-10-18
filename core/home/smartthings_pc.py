"""NAS-side PC wake requests, independent of the PC model and Google Home SDK.

Only the privately pinned PC's main/switch/on or off command is supported. An uncertain
POST is never retried; a persistent reservation also survives process restart.
"""
from __future__ import annotations

import ipaddress
import json
import os
from pathlib import Path
import re
import socket
import sqlite3
import stat
import time
import urllib.error
import urllib.request
import uuid


class PcError(Exception):
    """Fixed public error codes; never propagate HTTP bodies or credentials."""


def read_private(path: Path) -> str:
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
                 | getattr(os, "O_NONBLOCK", 0))
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or (os.name == "posix" and (
                info.st_mode & 0o077 or info.st_uid != os.getuid())):
            raise PcError("PRIVATE_FILE_REQUIRED")
        data = stream.read(32769)
    if len(data) > 32768:
        raise PcError("PRIVATE_FILE_TOO_LARGE")
    return data.decode("utf-8-sig")


def load_binding(path: Path) -> dict:
    value = json.loads(read_private(path))
    fields = {"schema_version", "enabled", "device_id", "label",
              "component", "confirmed_pc_virtual_switch", "pc_host"}
    if not isinstance(value, dict) or set(value) != fields:
        raise PcError("INVALID_BINDING")
    if type(value["schema_version"]) is not int or value["schema_version"] != 1:
        raise PcError("INVALID_BINDING")
    if value["enabled"] is not True:
        raise PcError("PC_CONTROL_DISABLED")
    if (value["label"] != "컴퓨터" or value["component"] != "main"
            or value["confirmed_pc_virtual_switch"] is not True):
        raise PcError("INVALID_BINDING")
    value["device_id"] = str(uuid.UUID(value["device_id"]))
    host = ipaddress.IPv4Address(value["pc_host"])
    if not host.is_private or host.is_loopback or host.is_link_local or host.is_unspecified:
        raise PcError("INVALID_PC_HOST")
    return value


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise PcError("REDIRECT_REFUSED")


class SmartThingsPcTransport:
    def __init__(self, binding: dict, token: str):
        if (not 16 <= len(token) <= 4096
                or any(ord(c) < 33 or ord(c) > 126 for c in token)):
            raise PcError("INVALID_CREDENTIAL")
        self.binding = binding
        self._token = token
        self._opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}), NoRedirect())

    def request(self, suffix: str = "", *, on: bool = False, command: str | None = None) -> dict:
        # No arbitrary URLs, capabilities, commands or caller-supplied targets.
        if command is not None and (command not in {"on", "off"} or on):
            raise PcError("INVALID_OPERATION")
        command = "on" if on else command
        if (suffix not in {"", "/status", "/commands"}
                or (suffix == "/commands") != (command is not None)):
            raise PcError("INVALID_OPERATION")
        body = {"commands": [{"component": "main", "capability": "switch",
                              "command": command, "arguments": []}]} if command else None
        request = urllib.request.Request(
            "https://api.smartthings.com/v1/devices/" + self.binding["device_id"] + suffix,
            data=json.dumps(body).encode() if body is not None else None,
            method="POST" if command else "GET",
            headers={"Authorization": "Bearer " + self._token,
                     "Content-Type": "application/json;charset=utf-8", "Accept": "application/json"})
        try:
            with self._opener.open(request, timeout=8) as response:
                data = response.read(1_000_001)
                if len(data) > 1_000_000:
                    raise PcError("INVALID_RESPONSE")
                value = json.loads(data)
                if not isinstance(value, dict):
                    raise PcError("INVALID_RESPONSE")
                return value
        except urllib.error.HTTPError as error:
            raise PcError("HTTP_" + str(error.code)) from None
        except (OSError, urllib.error.URLError, TimeoutError):
            raise PcError("CONNECTION_FAILED") from None
        except (UnicodeError, ValueError):
            raise PcError("INVALID_RESPONSE") from None

    def verify_target(self):
        device = self.request()
        if (device.get("deviceId") != self.binding["device_id"]
                or (device.get("label") or device.get("name")) != "컴퓨터"):
            raise PcError("TARGET_CHANGED")
        components = device.get("components")
        if not isinstance(components, list):
            raise PcError("TARGET_CHANGED")
        main = [c for c in components if isinstance(c, dict) and c.get("id") == "main"]
        if len(main) != 1 or not any(isinstance(c, dict) and c.get("id") == "switch"
                                   for c in main[0].get("capabilities", [])):
            raise PcError("MAIN_SWITCH_MISSING")

    def switch(self) -> dict:
        value = self.request("/status")
        try:
            switch = value["components"]["main"]["switch"]["switch"]
            if switch["value"] not in {"on", "off"}:
                raise ValueError()
            return {"value": switch["value"], "timestamp": switch.get("timestamp")}
        except (KeyError, TypeError, ValueError):
            raise PcError("SWITCH_STATE_UNKNOWN") from None

    def on(self) -> dict:
        return self.request("/commands", on=True)

    def off(self) -> dict:
        return self.request("/commands", command="off")


class WakeLedger:
    """One unresolved request per pinned target, including interrupted dispatch."""
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        parent = path.parent.lstat()
        if not stat.S_ISDIR(parent.st_mode) or (os.name == "posix" and (
                parent.st_mode & 0o077 or parent.st_uid != os.getuid())):
            raise PcError("PRIVATE_LEDGER_REQUIRED")
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL
                         | getattr(os, "O_NOFOLLOW", 0), 0o600)
            os.close(fd)
        except FileExistsError:
            fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
                         | getattr(os, "O_NONBLOCK", 0))
            try:
                info = os.fstat(fd)
                if not stat.S_ISREG(info.st_mode) or (os.name == "posix" and (
                        info.st_mode & 0o077 or info.st_uid != os.getuid())):
                    raise PcError("PRIVATE_LEDGER_REQUIRED")
            finally:
                os.close(fd)
        self.db = sqlite3.connect(path, timeout=5)
        self.db.execute("CREATE TABLE IF NOT EXISTS wake_requests "
                        "(id TEXT PRIMARY KEY, target TEXT, state TEXT, created REAL)")
        self.db.execute("CREATE TABLE IF NOT EXISTS shutdown_requests "
                        "(id TEXT PRIMARY KEY, target TEXT, state TEXT, created REAL)")
        self.db.commit()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.db.close()

    def reserve(self, target: str, request_key: str | None = None, *, allow_observed=False) -> str | None:
        return self._reserve(target, "wake_requests", request_key,allow_observed)

    def reserve_shutdown(self, target: str, request_key: str | None = None, *, allow_observed=False) -> str | None:
        return self._reserve(target, "shutdown_requests", request_key,allow_observed)

    def _reserve(self, target: str, table: str, request_key: str | None = None, allow_observed=False) -> str | None:
        if request_key is not None and not re.fullmatch(r'[A-Za-z0-9_-]{8,100}', request_key):
            raise ValueError('INVALID_REQUEST_ID')
        self.db.execute("BEGIN IMMEDIATE")
        try:
            if request_key is not None and any(self.db.execute(f'SELECT 1 FROM {t} WHERE id=?', (request_key,)).fetchone()
                                               for t in ('wake_requests', 'shutdown_requests')):
                return None
            for pending_table in ("wake_requests", "shutdown_requests"):
                rows=self.db.execute(f"SELECT id,state FROM {pending_table} WHERE target=? "
                                     "AND state IN ('DISPATCHING','ACCEPTED','UNKNOWN')", (target,)).fetchall()
                for prior,state in rows:
                    if allow_observed is True and state=='UNKNOWN':
                        from .dispatch_guard import DispatchGuard
                        if DispatchGuard._secondary_observed(self.db,prior,target):continue
                    return None
            key = request_key or str(uuid.uuid4())
            self.db.execute(f"INSERT INTO {table} VALUES (?,?,?,?)",
                            (key, target, "DISPATCHING", time.time()))
            return key
        finally:
            self.db.commit()

    def record(self, key: str, state: str):
        for table in ("wake_requests", "shutdown_requests"):
            self.db.execute(f"UPDATE {table} SET state=? WHERE id=?", (state, key))
        self.db.commit()

    def reconcile(self, target: str):
        self.db.execute("UPDATE wake_requests SET state='OBSERVED_REACHABLE' WHERE target=? "
                        "AND state IN ('DISPATCHING','ACCEPTED','UNKNOWN')", (target,))
        self.db.commit()

    def reconcile_shutdown(self, target: str):
        self.db.execute("UPDATE shutdown_requests SET state='OBSERVED_OFF_UNREACHABLE' WHERE target=? "
                        "AND state IN ('DISPATCHING','ACCEPTED','UNKNOWN')", (target,))
        self.db.commit()


def pc_reachable(host: str) -> bool:
    try:
        with socket.create_connection((host, 22), timeout=1):
            return True
    except OSError:
        return False


def dubbing_shutdown_readiness() -> str:
    """Refuse shutdown during known dubbing work or an unavailable worker check."""
    try:
        from rio_dubbing_client import DubbingClient
        health = DubbingClient().health()
        worker = health.get("worker", {})
        if not isinstance(worker, dict) or health.get("ok") is not True:
            return "UNVERIFIED"
        if worker.get("active_job_id") or worker.get("state") in {"queued", "running"}:
            return "BUSY"
        return "IDLE" if worker.get("state") == "idle" else "UNVERIFIED"
    except Exception:
        return "UNVERIFIED"


class PcController:
    def __init__(self, binding: dict, transport, ledger_path: Path, probe=pc_reachable,
                 shutdown_readiness=dubbing_shutdown_readiness):
        self.binding, self.transport, self.ledger_path, self.probe = binding, transport, ledger_path, probe
        self.shutdown_readiness = shutdown_readiness

    def observe(self) -> dict:
        self.transport.verify_target()
        switch = self.transport.switch()
        reachable = self.probe(self.binding["pc_host"])
        # The virtual switch can report off while the configured PC responds.
        # Resolve the wake reservation from independent connection evidence;
        # preserve and report the original switch value rather than rewriting it.
        if reachable:
            with WakeLedger(self.ledger_path) as ledger:
                ledger.reconcile(self.binding["device_id"])
        elif switch["value"] == "off":
            with WakeLedger(self.ledger_path) as ledger:
                ledger.reconcile_shutdown(self.binding["device_id"])
        return {"switch": switch["value"], "switch_timestamp": switch["timestamp"],
                "pc_ssh_reachable": reachable, "models_ready": "NOT_CHECKED"}

    def wake(self, request_key: str | None = None) -> dict:
        observation = self.observe()
        if observation["pc_ssh_reachable"]:
            return {**observation, "result": "ALREADY_REACHABLE", "commands_sent": 0}
        if observation["switch"] == "on":
            return {**observation, "result": "ON_NOT_REACHABLE", "commands_sent": 0}
        if getattr(self.transport, "control_enabled", None) is False:
            return {**observation, "result": "REJECTED", "code": "CLI_CONTROL_DISABLED", "commands_sent": 0}
        with WakeLedger(self.ledger_path) as ledger:
            key = ledger.reserve(self.binding["device_id"], request_key)
            if key is None:
                return {**observation, "result": "PENDING_DO_NOT_RETRY", "commands_sent": 0}
            try:
                response = self.transport.on()
                rows = response.get("results")
                accepted = (isinstance(rows, list) and len(rows) == 1 and isinstance(rows[0], dict)
                            and rows[0].get("status") == "ACCEPTED")
                state = "ACCEPTED" if accepted else "UNKNOWN"
                ledger.record(key, state)
                return {**observation, "result": state, "commands_sent": 1}
            except Exception as error:
                # A network/5xx/redirect/parser failure after dispatch has an
                # unknown outcome. Never repeat a possibly delivered request.
                code = str(error) if isinstance(error, PcError) else "UNEXPECTED_ERROR"
                rejected = code in {"HTTP_400", "HTTP_401", "HTTP_403", "HTTP_404", "HTTP_422", "HTTP_429"}
                ledger.record(key, "REJECTED" if rejected else "UNKNOWN")
                return {**observation, "result": "REJECTED" if rejected else "UNKNOWN",
                        "code": code, "commands_sent": 1}

    def shutdown(self, request_key: str | None = None) -> dict:
        observation = self.observe()
        if observation["switch"] == "off" and not observation["pc_ssh_reachable"]:
            return {**observation, "result": "OFF_NOT_REACHABLE", "commands_sent": 0}
        readiness = self.shutdown_readiness()
        if readiness != "IDLE":
            return {**observation, "result": "SHUTDOWN_" + ("BUSY" if readiness == "BUSY" else "UNVERIFIED"),
                    "commands_sent": 0}
        if getattr(self.transport, "control_enabled", None) is False:
            return {**observation, "result": "REJECTED", "code": "CLI_CONTROL_DISABLED", "commands_sent": 0}
        with WakeLedger(self.ledger_path) as ledger:
            key = ledger.reserve_shutdown(self.binding["device_id"], request_key)
            if key is None:
                return {**observation, "result": "PENDING_DO_NOT_RETRY", "commands_sent": 0}
            try:
                response = self.transport.off()
                rows = response.get("results")
                accepted = (isinstance(rows, list) and len(rows) == 1 and isinstance(rows[0], dict)
                            and rows[0].get("status") == "ACCEPTED")
                state = "ACCEPTED" if accepted else "UNKNOWN"
                ledger.record(key, state)
                return {**observation, "result": state, "commands_sent": 1}
            except Exception as error:
                code = str(error) if isinstance(error, PcError) else "UNEXPECTED_ERROR"
                rejected = code in {"HTTP_400", "HTTP_401", "HTTP_403", "HTTP_404", "HTTP_422", "HTTP_429"}
                ledger.record(key, "REJECTED" if rejected else "UNKNOWN")
                return {**observation, "result": "REJECTED" if rejected else "UNKNOWN",
                        "code": code, "commands_sent": 1}


def controller_from_environment() -> PcController:
    binding = load_binding(Path(os.getenv("RIO_PC_TARGET_FILE", str(Path.home() / ".config/rio/smartthings-pc.json"))))
    provider = os.getenv("RIO_PC_TRANSPORT")
    private_selection = Path.home() / ".config/rio/pc-auth-provider.json"
    selection = None
    if provider is None and private_selection.exists():
        try:
            selection = json.loads(read_private(private_selection))
        except (ValueError, UnicodeError):
            raise PcError("INVALID_AUTH_PROVIDER") from None
        if (not isinstance(selection, dict) or set(selection) !=
                {"schema_version", "provider", "cli_root", "cli_sha256"} or
                type(selection["schema_version"]) is not int or
                selection["schema_version"] != 1 or selection["provider"] != "oauth-cli" or
                not isinstance(selection["cli_root"], str) or
                not isinstance(selection["cli_sha256"], str)):
            raise PcError("INVALID_AUTH_PROVIDER")
        provider = "oauth-cli"
    if provider is None:
        provider = "pat"
    if provider == "oauth-cli":
        from .smartthings_cli import OfficialCliTransport, from_environment
        if selection is not None:
            transport = OfficialCliTransport(binding, root=Path(selection["cli_root"]),
                                             expected_sha256=selection["cli_sha256"])
        else:
            transport = from_environment(binding)
    elif provider == "pat":
        token = read_private(Path(os.getenv("RIO_SMARTTHINGS_TOKEN_FILE", str(Path.home() / ".config/rio/smartthings-token")))).strip()
        transport = SmartThingsPcTransport(binding, token)
    else:
        raise PcError("INVALID_AUTH_PROVIDER")
    ledger = Path(os.getenv("RIO_PC_WAKE_LEDGER", str(Path.home() / ".local/state/rio/pc-power/commands.sqlite3")))
    return PcController(binding, transport, ledger)


def parse_pc_request(text: str) -> str | None:
    text = re.sub(r"^(?:리오야|리오)[,，]?\s+", "", text.strip(), flags=re.I)
    if re.fullmatch(r"(?:컴퓨터|pc)\s+켜\s*(?:줘|주세요)[.!]?", text, re.I) or text.lower() == "/pc on":
        return "on"
    if (re.fullmatch(r"(?:컴퓨터|pc)\s+꺼\s*(?:줘|주세요)[.!]?", text, re.I)
            or re.fullmatch(r"(?:컴퓨터|pc)\s+종료해\s*(?:줘|주세요)[.!]?", text, re.I)
            or text.lower() == "/pc off"):
        return "off"
    if (re.fullmatch(r"(?:컴퓨터|pc)\s+상태(?:\s+알려\s*줘)?[.!?]?", text, re.I)
            or re.fullmatch(r"(?:컴퓨터|pc)\s+켜져\s*있어\?", text, re.I)
            or text.lower() in {"/pc status", "/pc"}):
        return "status"
    if text.lower().startswith("/pc "):
        return "help"
    return None


def result_message(value: dict, action: str) -> str:
    result = value.get("result")
    if result == "SHUTDOWN_BUSY":
        return "더빙 작업이 진행 중이라 컴퓨터 종료 요청을 보내지 않았어요."
    if result == "SHUTDOWN_UNVERIFIED":
        return "더빙 작업 상태를 확인하지 못해 컴퓨터 종료 요청을 보내지 않았어요."
    if result == "OFF_NOT_REACHABLE":
        return "스마트싱스는 꺼짐이고 PC 연결도 응답하지 않아 종료 명령을 보내지 않았어요. 실제 종료 확인은 별도로 필요해요."
    if result == "ACCEPTED":
        if action == "off":
            return "컴퓨터 정상 종료 요청을 보냈어요. 실제 종료 완료는 아직 확인하지 않았어요. /pc status로 확인할 수 있어요."
        return "컴퓨터 켜기 요청을 보냈어요. 실제 부팅 완료는 아직 확인하지 않았어요. /pc status로 확인할 수 있어요."
    if result in {"UNKNOWN", "PENDING_DO_NOT_RETRY"}:
        return "앞선 전원 요청의 결과가 아직 불확실해요. 중복 전송하지 않고 상태만 확인해 주세요."
    if result == "REJECTED":
        if value.get("code") == "CLI_CONTROL_DISABLED":
            return "공식 CLI 읽기 전용 모드여서 PC 전원 명령을 보내지 않았어요."
        return "스마트싱스가 전원 요청을 거부했어요. 인증과 제어 권한을 확인해 주세요."
    if value.get("pc_ssh_reachable"):
        if value.get("switch") == "off":
            return "PC의 SSH 연결은 응답하지만 스마트싱스는 꺼짐으로 보고해요. 상태가 서로 달라요. 모델 서버 준비 여부는 별도 확인이 필요해요."
        return "PC의 SSH 연결이 응답해요. 대화·TTS·더빙 서버 준비 여부는 별도 확인이 필요해요."
    if value.get("switch") == "on":
        return "스마트싱스는 켜짐이지만 PC 연결은 아직 응답하지 않아요. 켜기 명령을 다시 보내지는 않았어요."
    return "스마트싱스는 꺼짐이고 PC 연결도 응답하지 않아요."


def handle_pc_command(text: str, factory=None) -> str | None:
    action = parse_pc_request(text)
    if action is None:
        return None
    if action == "help":
        return "PC 명령: 컴퓨터 켜줘 · 컴퓨터 꺼줘 · 컴퓨터 상태 · /pc on · /pc off · /pc status"
    try:
        controller = (factory or controller_from_environment)()
        value = controller.wake() if action == "on" else controller.shutdown() if action == "off" else controller.observe()
        return result_message(value, action)
    except Exception:
        # No exception text: paths, remote bodies and credentials stay private.
        return "PC 제어 설정이나 스마트싱스 연결을 확인할 수 없어요. 불확실한 요청은 자동으로 다시 보내지 않아요."
