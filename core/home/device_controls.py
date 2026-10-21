"""Explicit non-light bindings; the primary bridge never falls back on a write."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import uuid

from .smartthings_pc import read_private
from .tools import CommandLedger, HomeToolRegistry, LocalHomeTransport, collect_command_result

FAMILIES = {
    "smart_plug": {"OnOffPluginUnitDevice"},
    "air_conditioner_power": {"OnOffPluginUnitDevice", "RoomAirConditionerDevice"},
    "tv": {"GoogleTVDevice"},
    "soundbar": {"GoogleSoundbarDevice", "GoogleStreamingSoundbarDevice", "SpeakerDevice"},
}
COMMANDS = frozenset({"on", "off", "mute", "unmute", "play", "pause", "stop"})
RESERVED_NAMES = frozenset({"컴퓨터", "환풍기", "청소기", "Mi Robot Vacuum", "거실", "주방", "샤워실", "세면대", "탁상등"})


def validate_device_targets(value: dict) -> dict:
    if (not isinstance(value, dict) or set(value) != {"schema_version", "enabled", "targets"}
            or type(value["schema_version"]) is not int or value["schema_version"] != 1
            or type(value["enabled"]) is not bool or not isinstance(value["targets"], dict)):
        raise ValueError("Invalid device configuration")
    aliases = set()
    for name, binding in value["targets"].items():
        if (not isinstance(name, str) or not name or name != name.strip() or len(name) > 40
                or name in RESERVED_NAMES or not isinstance(binding, dict)
                or set(binding) != {"alias", "device_type", "family", "expected_name", "allowed_commands"}):
            raise ValueError("Invalid device binding")
        alias, family, kind, commands = (binding[k] for k in ("alias", "family", "device_type", "allowed_commands"))
        if (not isinstance(alias, str) or not re.fullmatch(r"dev_[0-9a-f]{24}", alias)
                or alias in aliases or not isinstance(family, str) or family not in FAMILIES
                or kind not in FAMILIES[family] or not isinstance(commands, list) or not commands
                or any(not isinstance(c, str) or c not in COMMANDS for c in commands)
                or len(set(commands)) != len(commands)
                or not isinstance(binding["expected_name"], str) or not binding["expected_name"].strip()
                or binding["expected_name"] in RESERVED_NAMES):
            raise ValueError("Invalid device binding")
        if family in {"smart_plug", "air_conditioner_power"} and set(commands) - {"on", "off"}:
            raise ValueError("Unsupported family command")
        aliases.add(alias)
    return value


class DeviceControlRegistry:
    def __init__(self, transport, configuration: dict, ledger_path: Path):
        self.transport = transport
        self.configuration = validate_device_targets(configuration)
        self.targets = self.configuration["targets"]
        self.ledger_path = ledger_path
        self._ledger = None

    @property
    def ledger(self):
        if self._ledger is None:
            self._ledger = CommandLedger(self.ledger_path)
        return self._ledger

    def close(self):
        if self._ledger is not None:
            self._ledger.close()

    def plan(self, target: str, command: str) -> dict:
        binding = self.targets.get(target)
        if not binding or command not in COMMANDS or command not in binding["allowed_commands"]:
            return {"status": "REJECTED", "code": "UNREGISTERED_DEVICE_COMMAND"}
        catalog = self.transport.catalog()
        bridge = catalog.get("bridge", {}) if isinstance(catalog, dict) else {}
        if not (bridge.get("available") is True and bridge.get("permission") == "GRANTED"
                and bridge.get("bridge_state") == "READY"):
            return {"status": "REJECTED", "code": "BRIDGE_NOT_READY"}
        matches = [d for d in catalog.get("devices", []) if d.get("alias") == binding["alias"]]
        if len(matches) != 1 or matches[0].get("name") != binding["expected_name"]:
            return {"status": "REJECTED", "code": "DEVICE_IDENTITY_CHANGED"}
        item = matches[0]
        candidates = [c for c in item.get("device_capabilities", [])
                      if c.get("device_type") == binding["device_type"]]
        connection = ("power_connectivity" if command in {"on", "off"} else
                      "media_connectivity" if command in {"play", "pause", "stop"} else "volume_connectivity")
        if (len(candidates) == 1 and (connection in candidates[0] or command in {"play", "pause", "stop"})
                and candidates[0].get(connection) not in {"ONLINE", "PARTIALLY_ONLINE"}):
            return {"status": "REJECTED", "code": "DEVICE_OFFLINE_OR_UNKNOWN"}
        current = item.get("device_control_binding", {})
        if (len(candidates) != 1 or candidates[0].get("supported_commands", {}).get(command) is not True
                or current.get("status") != "SELECTED_EXPLICIT_TYPE"
                or current.get("device_type") != binding["device_type"]
                or current.get("allowed_commands", {}).get(command) is not True):
            return {"status": "REJECTED", "code": "DEVICE_COMMAND_NOT_VERIFIED"}
        return {"status": "PREVIEW", "target": target, "command": command,
                "ready_for_execution": bridge.get("controls_enabled") is True,
                "enabled": self.configuration["enabled"]}

    def execute(self, target: str, command: str, key: str | None = None) -> dict:
        if key is not None and (not isinstance(key, str) or not re.fullmatch(r"[A-Za-z0-9_-]{8,100}", key)):
            return {"status": "REJECTED", "code": "INVALID_IDEMPOTENCY_KEY"}
        plan = self.plan(target, command)
        if plan["status"] != "PREVIEW" or not self.configuration["enabled"]:
            return plan
        if not plan["ready_for_execution"]:
            return {"status": "REJECTED", "code": "CONTROLS_LOCKED"}
        binding = self.targets[target]
        key = key or uuid.uuid4().hex
        payload = {"operation": "device_command", "alias": binding["alias"],
                   "arguments": {"device_type": binding["device_type"], "command": command}}
        from .dispatch_guard import dispatch_primary
        def preflight():
            check=self.plan(target,command)
            return self.configuration['enabled'] and check.get('status')=='PREVIEW' and check.get('ready_for_execution') is True
        return dispatch_primary(self,key,payload,preflight,
            lambda:self.transport.request('/v1/commands',{**payload,'idempotency_key':key,'ttl_s':20}))

    def wait_result(self, answer: dict, timeout_s: float = 20) -> dict:
        """Collect the final result with bounded reads; never resend a command."""
        if answer.get('status')!='DISPATCHED' or timeout_s<=0:
            return answer
        return collect_command_result(self.transport,self.ledger,answer,timeout_s)



def registry_from_environment() -> DeviceControlRegistry:
    path = Path(os.getenv("RIO_HOME_DEVICE_TARGET_FILE", str(Path.home()/".config/rio/google-home-devices.json")))
    configuration = json.loads(read_private(path)) if path.exists() else {"schema_version": 1, "enabled": False, "targets": {}}
    token = Path(os.getenv("RIO_HOME_TOKEN_FILE", str(Path.home()/"google-home-bridge/state/gateway/rio.token")))
    ledger = Path(os.getenv("RIO_HOME_LEDGER", str(Path.home()/".local/state/rio/home/example")))
    registry=DeviceControlRegistry(LocalHomeTransport(token), configuration, ledger)
    registry.revalidate=lambda: json.loads(read_private(path))==configuration
    return registry


def parse_device_request(text: str) -> tuple[str, str] | None:
    # PC uses its dedicated wake/shutdown guards; lights keep their existing binding.
    match = re.fullmatch(r"/home (lens|에어컨|TV|사운드바) (on|off|mute|unmute|play|pause|stop)", text)
    if match:
        return match[1], match[2]
    text = re.sub(r"^(?:리오야|리오)[,，]?\s+", "", text.strip())
    match = re.fullmatch(r"(lens|에어컨|TV|사운드바)\s+(켜|꺼)\s*(?:줘|주세요)[.!]?", text)
    if match:
        return match[1], "on" if match[2] == "켜" else "off"
    match = re.fullmatch(r"(TV|사운드바)\s+음소거\s*(해제|해)\s*(?:줘|주세요)[.!]?", text)
    if match:
        return match[1], "unmute" if match[2] == "해제" else "mute"
    match = re.fullmatch(r"(TV|사운드바)\s+(재생|일시\s*정지|정지)\s*(?:해\s*)?(?:줘|주세요)[.!]?", text)
    if match:
        return match[1], {"재생": "play", "일시정지": "pause", "정지": "stop"}[re.sub(r"\s+", "", match[2])]
    return None


def handle_device_command(text: str, factory=None, *, request_key=None) -> str | None:
    request = parse_device_request(text)
    if request is None:
        return None
    registry = None
    try:
        registry = (factory or registry_from_environment)()
        answer = registry.wait_result(registry.execute(*request,key=request_key))
        message = {
            "PREVIEW": "기기 연결 준비 상태예요. 실제 제어는 아직 활성화되지 않았어요.",
            "SUCCEEDED": "기기 제어 요청은 완료됐어요. 실제 상태는 아직 확인이 필요해요.",
            "DISPATCHED": "기기 제어 요청을 보냈어요. 결과를 확인 중이에요.",
            "UNKNOWN": "기기 제어 결과를 확인하지 못했어요. 중복 실행하지 않을게요.",
        }.get(answer.get("status"), "이 기기의 요청 기능이나 연결을 아직 확인하지 못해 실행하지 않았어요.")
        if answer.get('status')=='REJECTED':
            message={
                'PREVIOUS_RESULT_UNRESOLVED':'이전 기기 요청의 결과가 아직 미확정이라 새 요청을 보내지 않았어요. 이전 요청 결과 확인이 필요해요.',
                'BRIDGE_NOT_READY':'태블릿 브리지 연결이나 Google Home 권한을 확인하지 못해 실행하지 않았어요.',
                'DEVICE_IDENTITY_CHANGED':'현재 목록의 기기와 등록된 연결이 일치하지 않아 실행하지 않았어요.',
                'DEVICE_OFFLINE_OR_UNKNOWN':'요청한 기능의 연결 상태가 오프라인이거나 미확인이라 실행하지 않았어요.',
                'DEVICE_COMMAND_NOT_VERIFIED':'현재 기기가 이 동작을 지원하고 허용하는지 확인하지 못해 실행하지 않았어요.',
                'UNREGISTERED_DEVICE_COMMAND':'이 기기에는 요청한 동작이 등록되어 있지 않아 실행하지 않았어요.',
                'CONTROLS_LOCKED':'기기 제어가 잠겨 있어 실행하지 않았어요.',
                'SESSION_CHANGED_BEFORE_SEND':'연결이 바뀌어 기기 명령을 보내지 않았어요. 연결 상태 확인이 필요해요.',
                'PERMISSION_CHANGED_BEFORE_SEND':'권한 상태가 바뀌어 기기 명령을 보내지 않았어요.',
                'COMMAND_EXPIRED_BEFORE_SEND':'상태를 확인하는 동안 요청 시간이 지나 기기 명령을 보내지 않았어요.',
                'SDK_PREFLIGHT_FAILED':'Google Home에서 기기 정보를 다시 확인하지 못해 실제 제어 명령은 보내지 않았어요.',
            }.get(answer.get('code'),message)
        if answer.get('status')=='UNKNOWN' and answer.get('sdk_error_code')==19:
            message='Google Home SDK가 명령 실패(19)를 보고했어요. 실제 기기 동작은 미확정이며 자동으로 다시 보내지 않아요.'
            if answer.get('sdk_unwrapped_error_code')==5:
                message='Google Home SDK가 요청 대상을 찾지 못했다고 응답했어요(NOT_FOUND, 5). 실제 동작을 확인하기 전에는 다시 보내지 않을게요.'
        if answer.get("request_id"):
            message += " /home result " + answer["request_id"] + "로 결과를 확인할 수 있어요."
        return message
    except Exception:
        return "기기 연결 설정을 확인하지 못했어요. 다른 경로로 자동 재전송하지 않아요."
    finally:
        if registry is not None:
            registry.close()
