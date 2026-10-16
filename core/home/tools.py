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
import stat
import time
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
            from .smartthings_pc import read_private
            if not ((path in {"/v1/status", "/v1/catalog"} and data is None)
                    or (path == "/v1/commands" and isinstance(data, dict))
                    or (re.fullmatch(r"/v1/jobs/[A-Za-z0-9_-]{8,100}", path) and data is None)):
                raise ValueError("Invalid gateway path")
            token = read_private(self.token_file).strip()
            if not 16 <= len(token) <= 4096 or any(ord(c) < 33 or ord(c) > 126 for c in token):
                raise ValueError("Invalid credential")
            request = urllib.request.Request("http://127.0.0.1:18444" + path,
                data=json.dumps(data).encode() if data is not None else None,
                headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"})
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirects())
            with opener.open(request, timeout=5) as response:
                raw = response.read(1_000_001)
                if len(raw) > 1_000_000:
                    raise ValueError("Gateway response too large")
                value = json.loads(raw)
                if not isinstance(value, dict):
                    raise ValueError("Invalid gateway response")
                return value
        except urllib.error.HTTPError as error:
            # Do not include response text, credentials, device IDs or URL in errors.
            return {"http_status": error.code, "code": "GATEWAY_REJECTED"}
        except Exception:
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
        parent = path.parent.lstat()
        if not stat.S_ISDIR(parent.st_mode) or (os.name == "posix" and (
                parent.st_mode & 0o077 or parent.st_uid != os.getuid())):
            raise ValueError("Private ledger directory required")
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
            os.close(fd)
        except FileExistsError:
            # Use descriptor metadata, without reading a potentially large DB.
            fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
            try:
                info = os.fstat(fd)
                if not stat.S_ISREG(info.st_mode) or (os.name == "posix" and (
                        info.st_mode & 0o077 or info.st_uid != os.getuid())):
                    raise ValueError("Private ledger required")
            finally:
                os.close(fd)
        self.db = sqlite3.connect(path)
        self.db.execute("CREATE TABLE IF NOT EXISTS light_commands (key TEXT PRIMARY KEY, digest TEXT, status TEXT, answer TEXT)")
        self.db.execute("CREATE TABLE IF NOT EXISTS human_command_reviews "
                        "(key TEXT PRIMARY KEY, observation TEXT NOT NULL, note TEXT NOT NULL, reviewed_at TEXT NOT NULL)")
        self.db.execute("CREATE TABLE IF NOT EXISTS command_result_refreshes "
                        "(key TEXT NOT NULL, previous_status TEXT NOT NULL, previous_answer TEXT NOT NULL, "
                        "new_status TEXT NOT NULL, refreshed_at TEXT NOT NULL)")
        self.db.execute("CREATE TABLE IF NOT EXISTS human_retry_authorizations "
                        "(next_key TEXT NOT NULL, prior_key TEXT NOT NULL, digest TEXT NOT NULL, "
                        "note TEXT NOT NULL, authorized_at TEXT NOT NULL, PRIMARY KEY(next_key,prior_key))")
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
            if self.db.execute("SELECT 1 FROM light_commands c WHERE c.status IN (?,?,?) "
                               "AND NOT (c.status='UNKNOWN' AND EXISTS "
                               "(SELECT 1 FROM human_command_reviews r WHERE r.key=c.key "
                               "AND r.observation IN ('USER_CONFIRMED_NOT_APPLIED','SUPERSEDED_BY_HUMAN_CONFIRMED_POWER'))) "
                               "AND NOT (c.status='UNKNOWN' AND EXISTS "
                               "(SELECT 1 FROM human_retry_authorizations a WHERE a.prior_key=c.key "
                               "AND a.next_key=? AND a.digest=?)) LIMIT 1", (*PENDING,key,digest)).fetchone():
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

    def refresh_dispatched(self, fetch_result):
        """Read durable gateway outcomes, never repeat a device command.

        Preserve UNKNOWN/human reviews. A failed/missing result cannot prove rejection.
        Network reads occur outside transactions; compare status again before updating.
        """
        pending=self.db.execute("SELECT key FROM light_commands WHERE status='DISPATCHED' LIMIT 8").fetchall()
        for (key,) in pending:
            try:
                response=fetch_result(key)
            except Exception:
                continue
            if (not isinstance(response,dict) or response.get('http_status')
                    or response.get('id',key)!=key or response.get('status') not in {'SUCCEEDED','REJECTED','UNKNOWN'}):
                continue
            answer=HomeToolRegistry.answer(response,key)
            self.db.execute('BEGIN IMMEDIATE')
            try:
                prior=self.db.execute("SELECT status,answer FROM light_commands WHERE key=?",(key,)).fetchone()
                if prior and prior[0]=='DISPATCHED':
                    self.db.execute("INSERT INTO command_result_refreshes VALUES(?,?,?,?,strftime('%Y-%m-%dT%H:%M:%fZ','now'))",
                                    (key,prior[0],prior[1],answer['status']))
                    self.db.execute('UPDATE light_commands SET status=?,answer=? WHERE key=?',
                                    (answer['status'],json.dumps(answer),key))
            except Exception:
                self.db.rollback();raise
            self.db.commit()

    def review_not_applied(self, key: str, note: str, *, gateway_result: dict, gateway_status: dict):
        """Operator-only review after a direct human report; never exposed as an LLM tool.

        Keep the UNKNOWN result and original idempotency key. Only a new explicit
        request can proceed; SDK state values alone must never call this method.
        """
        if (not isinstance(note, str) or not note.strip() or len(note) > 400
                or not isinstance(gateway_result, dict) or gateway_result.get('status') != 'UNKNOWN'
                or gateway_result.get('id', key) != key
                or not isinstance(gateway_status, dict) or gateway_status.get('available') is not True
                or gateway_status.get('permission') != 'GRANTED'
                or gateway_status.get('inflight') is not False):
            raise ValueError('A completed unknown request and explicit human observation are required')
        self.db.execute('BEGIN IMMEDIATE')
        try:
            row = self.db.execute('SELECT status FROM light_commands WHERE key=?', (key,)).fetchone()
            if not row or row[0] != 'UNKNOWN':
                raise ValueError('Only a terminal UNKNOWN request can be reviewed')
            self.db.execute("INSERT INTO human_command_reviews VALUES (?, 'USER_CONFIRMED_NOT_APPLIED', ?, "
                            "strftime('%Y-%m-%dT%H:%M:%fZ','now'))", (key, note.strip()))
        except Exception:
            self.db.rollback()
            raise
        self.db.commit()

    def authorize_power_retry(self, prior_key: str, next_key: str, payload: dict, note: str,
                              *, gateway_result: dict, gateway_status: dict):
        """Operator-only: bind a direct human retry instruction to one new power request.

        This is authorization, not a claim that the prior action did not happen.
        Preserve UNKNOWN; do not expose this method as a model tool or auto-retry.
        """
        if (not all(isinstance(k,str) and re.fullmatch(r'[A-Za-z0-9_-]{8,100}',k)
                    for k in (prior_key,next_key)) or prior_key==next_key
                or not isinstance(note,str) or not note.strip() or len(note)>400
                or not isinstance(payload,dict) or set(payload)!={'operation','alias','arguments'}
                or not isinstance(payload.get('alias'),str)
                or not re.fullmatch(r'dev_[0-9a-f]{24}',payload['alias'])):
            raise ValueError('An explicit one-request power retry authorization is required')
        args=payload['arguments']
        power=(payload['operation']=='set_power' and isinstance(args,dict)
               and set(args)=={'on'} and type(args['on']) is bool)
        power=power or (payload['operation']=='device_command' and isinstance(args,dict)
                       and set(args)=={'device_type','command'} and isinstance(args['device_type'],str)
                       and isinstance(args['command'],str) and args['command'] in {'on','off'})
        if (not power or not isinstance(gateway_result,dict) or gateway_result.get('http_status')
                or gateway_result.get('status')!='UNKNOWN' or gateway_result.get('id',prior_key)!=prior_key
                or not isinstance(gateway_status,dict) or gateway_status.get('available') is not True
                or gateway_status.get('bridge_state')!='READY' or gateway_status.get('permission')!='GRANTED'
                or gateway_status.get('inflight') is not False):
            raise ValueError('A terminal unknown power result and a ready idle gateway are required')
        digest=hashlib.sha256(json.dumps(payload,sort_keys=True,separators=(',',':')).encode()).hexdigest()
        self.db.execute('BEGIN IMMEDIATE')
        try:
            row=self.db.execute('SELECT digest,status FROM light_commands WHERE key=?',(prior_key,)).fetchone()
            if row!=(digest,'UNKNOWN') or self.known(next_key):
                raise ValueError('Retry must match the prior payload and use an unused request ID')
            self.db.execute("INSERT INTO human_retry_authorizations VALUES(?,?,?,?,strftime('%Y-%m-%dT%H:%M:%fZ','now'))",
                            (next_key,prior_key,digest,note.strip()))
        except Exception:
            self.db.rollback();raise
        self.db.commit()

    def review_power_superseded(self, prior_key: str, confirmed_key: str, payload: dict, note: str,
                                *, gateway_result: dict, gateway_status: dict):
        """Operator-only after a human confirms a newer exact power request worked.

        The earlier UNKNOWN remains unknown; a later confirmed desired state
        supersedes its blocking effect, without claiming it never happened.
        """
        if (not isinstance(note,str) or not note.strip() or len(note)>400
                or not isinstance(payload,dict) or set(payload)!={'operation','alias','arguments'}
                or payload.get('operation')!='device_command' or not isinstance(payload.get('arguments'),dict)
                or set(payload['arguments'])!={'device_type','command'}
                or not isinstance(payload['arguments']['command'],str)
                or payload['arguments']['command'] not in {'on','off'}
                or not isinstance(gateway_result,dict) or gateway_result.get('http_status')
                or gateway_result.get('status')!='SUCCEEDED' or gateway_result.get('id',confirmed_key)!=confirmed_key
                or not isinstance(gateway_status,dict) or gateway_status.get('available') is not True
                or gateway_status.get('bridge_state')!='READY' or gateway_status.get('permission')!='GRANTED'
                or gateway_status.get('inflight') is not False):
            raise ValueError('A newer successful power request and direct human confirmation are required')
        digest=hashlib.sha256(json.dumps(payload,sort_keys=True,separators=(',',':')).encode()).hexdigest()
        self.db.execute('BEGIN IMMEDIATE')
        try:
            old=self.db.execute('SELECT rowid,digest,status FROM light_commands WHERE key=?',(prior_key,)).fetchone()
            new=self.db.execute('SELECT rowid,digest,status FROM light_commands WHERE key=?',(confirmed_key,)).fetchone()
            latest=self.db.execute('SELECT MAX(rowid) FROM light_commands').fetchone()[0]
            if (not old or not new or old[1:]!=(digest,'UNKNOWN') or new[1:]!=(digest,'SUCCEEDED')
                    or old[0]>=new[0] or new[0]!=latest):
                raise ValueError('Only the latest successful exact power payload can supersede an older unknown')
            self.db.execute("INSERT INTO human_command_reviews VALUES(?,'SUPERSEDED_BY_HUMAN_CONFIRMED_POWER',?,strftime('%Y-%m-%dT%H:%M:%fZ','now'))",
                            (prior_key,'Confirmed request '+confirmed_key+': '+note.strip()))
        except Exception:
            self.db.rollback();raise
        self.db.commit()


def collect_command_result(transport, ledger, answer:dict, timeout_s:float=20)->dict:
    """Read a known dispatched request to completion without repeating its write."""
    if answer.get('status')!='DISPATCHED' or timeout_s<=0:
        return answer
    key=answer.get('request_id')
    if not isinstance(key,str) or not ledger.known(key):
        return answer
    deadline=time.monotonic()+min(timeout_s,20)
    while time.monotonic()<deadline:
        try:
            response=transport.result(key)
        except Exception:
            return answer
        if (not isinstance(response,dict) or response.get('http_status')
                or response.get('id',key)!=key
                or response.get('status') not in {'DISPATCHED','SUCCEEDED','REJECTED','UNKNOWN'}):
            return answer
        if response['status']!='DISPATCHED':
            ledger.refresh_dispatched(lambda requested: response if requested==key else {})
            row=ledger.db.execute('SELECT answer FROM light_commands WHERE key=?',(key,)).fetchone()
            return json.loads(row[0]) if row else answer
        time.sleep(min(.25,max(0,deadline-time.monotonic())))
    return answer

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
    text = re.sub(r"^(?:리오야|리오)[,，]?\s+", "", text.strip())
    match = re.fullmatch(r"(?:(\S+)\s+)?(?:불|조명|전등)\s*(켜|꺼)\s*(?:줘|주세요)[.!]?", text)
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
        details=response.get('result') if isinstance(response.get('result'),dict) else {}
        code=response.get('code',details.get('code','GATEWAY_'+status))
        if not isinstance(code,str) or not re.fullmatch(r'[A-Z0-9_]{1,80}',code):code='GATEWAY_'+status
        answer={"status": status, "request_id": key,"physical_state_confirmed": False,"code":code}
        number=details.get('sdk_error_code')
        if type(number) is int and -10000<=number<=10000:answer['sdk_error_code']=number
        # Public numeric diagnostics only; never forward SDK messages or sub-error
        # map keys, which can contain private device identifiers.
        unwrapped=details.get('sdk_unwrapped_error_code')
        if type(unwrapped) is int and 0<=unwrapped<=2147483647:
            answer['sdk_unwrapped_error_code']=unwrapped
        for field in ('sdk_error_domain','sdk_unwrapped_error_domain'):
            value=details.get(field)
            if isinstance(value,str) and re.fullmatch(r'[A-Z][A-Z0-9_]{0,79}',value):
                answer[field]=value
        sub_codes=details.get('sdk_sub_error_codes')
        if (isinstance(sub_codes,list) and len(sub_codes)<=16
                and all(type(n) is int and 0<=n<=2147483647 for n in sub_codes)):
            answer['sdk_sub_error_codes']=list(dict.fromkeys(sub_codes))
        if details.get('sdk_sub_error_codes_truncated') is True:
            answer['sdk_sub_error_codes_truncated']=True
        return answer

    def wait_result(self, answer:dict, timeout_s:float=20)->dict:
        if answer.get('status')!='DISPATCHED':
            return answer
        return collect_command_result(self.transport,self.ledger,answer,timeout_s)

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
        if not self.ledger.known(key):
            self.ledger.refresh_dispatched(self.transport.result)
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
        if answer.get('sdk_unwrapped_error_code')==5:
            return "Google Home SDK가 제어 대상을 찾지 못했다고 응답했어(NOT_FOUND, 5). 실제 상태 확인 전에는 중복 실행하지 않을게."
        return "조명 요청의 결과를 확인하지 못했어. 중복 실행하지 않을게."
    if status == "REJECTED" and answer.get('code') in {
            'SESSION_CHANGED_BEFORE_SEND','PERMISSION_CHANGED_BEFORE_SEND',
            'COMMAND_EXPIRED_BEFORE_SEND','SDK_PREFLIGHT_FAILED'}:
        return "기기와 연결 상태를 다시 확인하지 못해 실제 조명 명령은 보내지 않았어."
    return "지금은 조명 요청을 실행할 수 없어."
