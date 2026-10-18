"""Explicit Google Home lighting route for main.py; no implicit backup writes."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re

from .smartthings_pc import read_private
from .tools import HomeToolRegistry, LocalHomeTransport, parse_light_request, user_message, validate_targets


def registry_from_environment() -> HomeToolRegistry:
    target_file = Path(os.getenv("RIO_HOME_TARGET_FILE", str(Path.home() / ".config/rio/google-home-lights.json")))
    target_text=read_private(target_file) if target_file.exists() else None
    targets = validate_targets(json.loads(target_text)) if target_text is not None else {}
    snapshots={target_file:target_text}
    token_file = Path(os.getenv("RIO_HOME_TOKEN_FILE", str(Path.home() / "google-home-bridge/state/gateway/rio.token")))
    ledger = Path(os.getenv("RIO_HOME_LEDGER", str(Path.home() / ".local/state/rio/home/example")))
    flag = os.getenv("RIO_HOME_EXECUTION_ENABLED")
    allow = flag is not None and flag.lower() in {"true", "1", "yes"}
    if flag is None:
        policy_file = Path(os.getenv("RIO_HOME_POLICY_FILE", str(Path.home()/".config/rio/google-home-policy.json")))
        if policy_file.exists():
            policy_text=read_private(policy_file)
            snapshots[policy_file]=policy_text
            policy = json.loads(policy_text)
            if (not isinstance(policy, dict) or set(policy) != {"schema_version", "enabled", "allowed_aliases"}
                    or type(policy["schema_version"]) is not int or policy["schema_version"] != 1
                    or type(policy["enabled"]) is not bool or not isinstance(policy["allowed_aliases"], list)
                    or any(not isinstance(alias, str) for alias in policy["allowed_aliases"])
                    or len(set(policy["allowed_aliases"])) != len(policy["allowed_aliases"])):
                raise ValueError("Invalid private home policy")
            aliases = {binding["alias"] for binding in targets.values()}
            if not set(policy["allowed_aliases"]) <= aliases:
                raise ValueError("Policy refers to an unregistered device")
            allow = policy["enabled"]
            if allow:
                targets = {name: binding for name, binding in targets.items()
                           if binding["alias"] in policy["allowed_aliases"]}
    registry=HomeToolRegistry(LocalHomeTransport(token_file), targets, ledger, allow_execution=allow)
    registry.revalidate=lambda: os.getenv('RIO_HOME_EXECUTION_ENABLED')==flag and all(
        (read_private(p) if p.exists() else None)==text for p,text in snapshots.items())
    return registry


def handle_home_command(text: str, factory=None, *, request_key=None) -> str | None:
    text = text.strip()
    # Provider-explicit commands must precede generic '/home <label> status' parsing.
    if text.startswith('/home smartthings '):
        from .smartthings_direct import handle_direct_command
        return handle_direct_command(text,request_key=request_key)
    from .device_catalog import handle_device_read
    inventory_reply = handle_device_read(text)
    if inventory_reply is not None:
        return inventory_reply
    from .device_controls import handle_device_command
    device_reply = handle_device_command(text,request_key=request_key)
    if device_reply is not None:
        return device_reply
    if text.startswith('/home secondary ') or text.startswith('/home backup '):
        from .smartthings_lights import handle_secondary_command
        reply = handle_secondary_command(text,request_key=request_key)
        return reply or '보조 명령: /home secondary 거실 status · /home backup 거실 on · /home backup 거실 off'
    status = text.lower() in {"/home", "/home status", "조명 상태", "전등 상태"}
    result = re.fullmatch(r"/home result ([A-Za-z0-9_-]{8,100})", text)
    direct = re.fullmatch(r"/home (\S+) (on|off)", text)
    intent = {"target": direct[1], "on": direct[2] == "on"} if direct else parse_light_request(text)
    if not (status or result or intent is not None):
        if text.lower().startswith("/home "):
            return "조명 명령: 거실 불 켜줘 · 거실 불 꺼줘 · /home status · /home 거실 on · /home 거실 off"
        return None
    registry = None
    try:
        registry = (factory or registry_from_environment)()
        if status:
            value = registry.call("home_get_status", {})
            ready = value.get("available") is True and value.get("permission") == "GRANTED" and value.get("bridge_state") == "READY"
            return ("Google Home 연결은 정상이에요. " if ready else "Google Home 연결·권한을 확인해야 해요. ") + (
                "제어가 허용된 조명만 요청할 수 있어요." if value.get("controls_enabled") is True else "조명 제어는 아직 잠겨 있어요.")
        if result:
            answer = registry.call("home_get_result", {"idempotency_key": result[1]})
        else:
            if not registry.targets:
                return "Google Home에 실제 조명 대상을 먼저 연결해야 해요. 현재 등록된 조명이 없어 제어하지 않았어요."
            if request_key is not None:intent={**intent,'idempotency_key':request_key}
            answer = registry.call("home_set_light", intent)
            if answer.get("status")=="DISPATCHED":
                answer=registry.wait_result(answer)
        reply = user_message(answer)
        if answer.get("request_id"):
            reply += " /home result " + answer["request_id"] + "로 결과를 확인할 수 있어요."
        return reply
    except Exception:
        return "Google Home 조명 설정이나 연결을 확인하지 못했어요. 다른 경로로 자동 재전송하지 않아요."
    finally:
        if registry is not None:
            registry.close()
