"""Light tools for the isolated NAS gateway, disabled for writes by default.

Targets are explicit private bindings, never inferred from a plug's name.
All device writes go through the existing gateway's policy/session/TTL checks.
An uncertain write is recorded durably and never automatically resent.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
from typing import Any
import urllib.error
import urllib.request
import uuid


DEVICE_TYPES = frozenset({"OnOffLightDevice", "DimmableLightDevice", "ColorTemperatureLightDevice",
                          "ExtendedColorLightDevice", "OnOffPluginUnitDevice"})
PENDING = ("DISPATCHING", "DISPATCHED", "UNKNOWN")


class NoRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, new_url):
        return None


class LocalHomeTransport:
    """Fixed loopback transport; secrets do not enter tool arguments/results."""

    def __init__(self, token_file: Path):
        self.token_file = token_file

    def request(self, path: str, data: dict | None = None) -> dict:
        try:
            token = self.token_file.read_text(encoding="utf-8").strip()
            request = urllib.request.Request("http://127.0.0.1:18444" + path,
                data=json.dumps(data).encode() if data is not None else None,
                headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"})
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirects())
            with opener.open(request, timeout=5) as response:
                value = json.load(response)
                if not isinstance(value, dict):
                    raise ValueError("Invalid gateway response")
                return value
        except urllib.error.HTTPError as error:
            # Do not include response text, credentials, device IDs or URL in errors.
            return {"http_status": error.code, "code": "GATEWAY_REJECTED"}
        except (OSError, ValueError, TimeoutError):
            return {"code": "GATEWAY_UNREACHABLE", "available": False, "controls_enabled": False}

    def status(self) -> dict:
        return self.request("/v1/status")

    def catalog(self) -> dict:
        return self.request("/v1/catalog")

    def set_power(self, alias: str, on: bool, key: str) -> dict:
        return self.request("/v1/commands", {"operation": "set_power", "alias": alias,
                                            "arguments": {"on": on}, "idempotency_key": key, "ttl_s": 20})

    def result(self, key: str) -> dict:
        return self.request("/v1/jobs/" + key)


class CommandLedger:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.db = sqlite3.connect(path)
        os.chmod(path, 0o600)
        self.db.execute("CREATE TABLE IF NOT EXISTS light_commands (key TEXT PRIMARY KEY, digest TEXT, status TEXT, answer TEXT)")
        self.db.commit()

    def close(self):
        self.db.close()

    def reserve(self, key: str, payload: dict) -> dict | None:
        digest = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        self.db.execute("BEGIN IMMEDIATE")
        try:
            prior = self.db.execute("SELECT digest,answer FROM light_commands WHERE key=?", (key,)).fetchone()
            if prior:
                return json.loads(prior[1]) if prior[0] == digest else {"status": "REJECTED", "code": "IDEMPOTENCY_CONFLICT"}
            if self.db.execute("SELECT 1 FROM light_commands WHERE status IN (?,?,?) LIMIT 1", PENDING).fetchone():
                return {"status": "REJECTED", "code": "PREVIOUS_RESULT_UNRESOLVED"}
            answer = {"status": "UNKNOWN", "code": "EXECUTION_UNCERTAIN_DO_NOT_RETRY", "request_id": key}
            self.db.execute("INSERT INTO light_commands VALUES (?,?,?,?)", (key, digest, "DISPATCHING", json.dumps(answer)))
            return None
        finally:
            self.db.commit()

    def record(self, key: str, answer: dict):
        self.db.execute("UPDATE light_commands SET status=?,answer=? WHERE key=?",
                        (answer["status"], json.dumps(answer), key))
        self.db.commit()

    def known(self, key: str) -> bool:
        return self.db.execute("SELECT 1 FROM light_commands WHERE key=?", (key,)).fetchone() is not None


def validate_targets(value: Any) -> dict:
    if not isinstance(value, dict) or set(value) != {"schema_version", "targets"} or type(value["schema_version"]) is not int or value["schema_version"] != 1:
        raise ValueError("Invalid target configuration")
    targets = value["targets"]
    if not isinstance(targets, dict):
        raise ValueError("Invalid targets")
    aliases = set()
    for name, binding in targets.items():
        if not isinstance(name, str) or not name.strip() or len(name) > 40 or name != name.strip():
            raise ValueError("Invalid target name")
        if not isinstance(binding, dict) or set(binding) != {"alias", "device_type", "confirmed_physical_light"}:
            raise ValueError("Invalid binding")
        if not isinstance(binding["alias"], str) or not re.fullmatch(r"dev_[0-9a-f]{24}", binding["alias"]):
            raise ValueError("Invalid private device alias")
        if binding["alias"] in aliases:
            raise ValueError("Two names must not silently address the same device")
        aliases.add(binding["alias"])
        if not isinstance(binding["device_type"], str) or binding["device_type"] not in DEVICE_TYPES or binding["confirmed_physical_light"] is not True:
            raise ValueError("Binding is not an explicitly confirmed lamp")
    return targets


def parse_light_request(text: str) -> dict | None:
    """A narrow direct-request route, not keyword matching or LLM inference."""
    text = re.sub(r"^리오\s*[,，]?\s+", "", text.strip())
    match = re.fullmatch(r"(?:(\S+)\s+)?(?:불|조명)\s*(켜|꺼)\s*(?:줘|주세요)[.!?]?", text)
    if not match:
        return None
    return {"target": match[1], "on": match[2] == "켜"}


class HomeToolRegistry:
    def __init__(self, transport, targets: dict, ledger_path: Path, allow_execution: bool = False):
        self.transport = transport
        self.targets = validate_targets({"schema_version": 1, "targets": targets})
        self.ledger_path = ledger_path
        self.allow_execution = allow_execution is True
        self._ledger = None

    @property
    def ledger(self):
        if self._ledger is None:
            self._ledger = CommandLedger(self.ledger_path)
        return self._ledger

    def close(self):
        if self._ledger is not None:
            self._ledger.close()

    def schemas(self) -> list[dict]:
        return [{"name": "home_get_status", "parameters": {"type": "object", "properties": {}, "additionalProperties": False}},
                {"name": "home_set_light", "parameters": {"type": "object", "properties": {
                    "target": {"type": "string", "enum": list(self.targets)}, "on": {"type": "boolean"},
                    "idempotency_key": {"type": "string", "pattern": "^[A-Za-z0-9_-]{8,100}$"}},
                    "required": ["target", "on"], "additionalProperties": False}},
                {"name": "home_get_result", "parameters": {"type": "object", "properties": {
                    "idempotency_key": {"type": "string", "pattern": "^[A-Za-z0-9_-]{8,100}$"}},
                    "required": ["idempotency_key"], "additionalProperties": False}}]

    def plan(self, target: str, on: bool) -> dict:
        if type(on) is not bool:
            return {"status": "REJECTED", "code": "INVALID_ON"}
        if not isinstance(target, str) or target not in self.targets:
            return {"status": "NEED_INPUT", "code": "UNKNOWN_OR_MISSING_TARGET", "choices": list(self.targets)}
        try:
            response = self.transport.catalog()
        except (OSError, TimeoutError, ValueError):
            response = {}
        if not isinstance(response, dict):
            return {"status": "REJECTED", "code": "BRIDGE_NOT_READY"}
        bridge = response.get("bridge", {})
        if not isinstance(bridge, dict) or bridge.get("available") is not True or bridge.get("permission") != "GRANTED" or bridge.get("bridge_state") != "READY":
            return {"status": "REJECTED", "code": "BRIDGE_NOT_READY"}
        binding = self.targets[target]
        devices = response.get("devices", [])
        matches = [d for d in devices if isinstance(d, dict) and d.get("alias") == binding["alias"]] if isinstance(devices, list) else []
        if len(matches) != 1:
            return {"status": "REJECTED", "code": "TARGET_NOT_UNIQUE_IN_CURRENT_CATALOG"}
        item = matches[0]
        blockers = []
        if bridge.get("controls_enabled") is not True:
            blockers.append("CONTROLS_LOCKED")
        control = item.get("control_binding", {})
        if not isinstance(control, dict) or control.get("status") != "SELECTED_EXPLICIT_TYPE" or control.get("device_type") != binding["device_type"]:
            blockers.append("BINDING_NOT_VERIFIED")
        commands = item.get("commands", {})
        if item.get("lighting") is not True or not isinstance(commands, dict) or commands.get("on" if on else "off") is not True:
            blockers.append("COMMAND_NOT_SUPPORTED")
        return {"status": "PREVIEW", "target": target, "on": on,
                "ready_for_execution": not blockers, "blockers": blockers}

    @staticmethod
    def answer(response: dict, key: str) -> dict:
        if not isinstance(response, dict) or response.get("id", key) != key:
            return {"status": "UNKNOWN", "request_id": key, "physical_state_confirmed": False,
                    "code": "UNVERIFIED_GATEWAY_RESPONSE"}
        status = response.get("status")
        if response.get("http_status") is not None or status not in {"DISPATCHED", "SUCCEEDED", "REJECTED", "UNKNOWN"}:
            status = "REJECTED" if response.get("http_status") in {400, 401, 403, 404, 409, 422, 503} else "UNKNOWN"
        return {"status": status, "request_id": key,
                "physical_state_confirmed": False, "code": response.get("code", "GATEWAY_" + status)}

    def call(self, name: str, args: dict) -> dict:
        allowed = {"home_get_status": set(), "home_set_light": {"target", "on", "idempotency_key"},
                   "home_get_result": {"idempotency_key"}}
        if name not in allowed or not isinstance(args, dict) or set(args) - allowed[name]:
            return {"status": "REJECTED", "code": "INVALID_TOOL_ARGUMENTS"}
        if name == "home_get_status":
            try:
                status = self.transport.status()
            except (OSError, TimeoutError, ValueError):
                status = {}
            if not isinstance(status, dict):
                status = {}
            return {k: status.get(k) for k in ("bridge_state", "available", "permission", "controls_enabled", "device_count")}
        key = args.get("idempotency_key")
        if key is not None and (not isinstance(key, str) or not re.fullmatch(r"[A-Za-z0-9_-]{8,100}", key)):
            return {"status": "REJECTED", "code": "INVALID_IDEMPOTENCY_KEY"}
        if name == "home_get_result":
            if key is None or not self.ledger_path.exists() or not self.ledger.known(key):
                return {"status": "REJECTED", "code": "UNKNOWN_REQUEST"}
            try:
                response = self.transport.result(key)
            except (OSError, TimeoutError, ValueError):
                response = {}
            answer = self.answer(response, key)
            # A failed GET cannot prove a previous write was rejected.
            if not isinstance(response, dict) or response.get("http_status") or response.get("status") not in {"DISPATCHED", "SUCCEEDED", "REJECTED", "UNKNOWN"}:
                answer["status"] = "UNKNOWN"
            self.ledger.record(key, answer)
            return answer
        plan = self.plan(args.get("target"), args.get("on"))
        if plan["status"] != "PREVIEW":
            return plan
        if not self.allow_execution:
            return plan
        if not plan["ready_for_execution"]:
            return {"status": "REJECTED", "code": "EXECUTION_NOT_READY", "blockers": plan["blockers"]}
        key = key or uuid.uuid4().hex
        binding = self.targets[args["target"]]
        payload = {"alias": binding["alias"], "on": args["on"]}
        prior = self.ledger.reserve(key, payload)
        if prior is not None:
            return prior
        try:
            response = self.transport.set_power(binding["alias"], args["on"], key)
        except (OSError, TimeoutError, ValueError):
            response = {}
        answer = self.answer(response, key)
        self.ledger.record(key, answer)
        return answer


def user_message(answer: dict) -> str:
    status = answer.get("status")
    if status == "NEED_INPUT":
        return "어느 조명을 말하는지 알려줘. 대상: " + ", ".join(answer.get("choices", []))
    if status == "PREVIEW":
        return "요청은 알아들었어. 아직 실제 조명 제어는 활성화되지 않았어."
    if status == "SUCCEEDED":
        return "제어 요청은 완료됐어. 실제 불 상태는 아직 확인하지 못했어."
    if status == "DISPATCHED":
        return "조명 요청을 보냈어. 결과를 확인 중이야."
    if status == "UNKNOWN":
        return "조명 요청의 결과를 확인하지 못했어. 중복 실행하지 않을게."
    return "지금은 조명 요청을 실행할 수 없어."
