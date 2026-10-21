"""Read-only whole-home inventory. Discovery never authorizes a device write."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import re
import urllib.request

from .smartthings_pc import NoRedirect, read_private
from .tools import LocalHomeTransport

EXCLUDED_NAMES = frozenset({"환풍기", "청소기", "Mi Robot Vacuum"})
UUID = re.compile(r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}")


def display_name(value) -> str:
    if not isinstance(value, str) or not value.strip():
        return "이름 미확인 기기"
    name = value.strip()
    if len(name) > 80 or re.fullmatch(r"[A-Za-z0-9_=-]{45,}", name):
        return "이름 미확인 기기"
    return re.sub(r"[\x00-\x1f\x7f]", "", name)


class SmartThingsReader:
    def __init__(self, token_file: Path | None = None):
        self.token_file = token_file or Path.home() / ".config/rio/smartthings-token"

    def get(self, path: str) -> dict:
        if not (path == "/v1/devices" or re.fullmatch(r"/v1/devices\?[A-Za-z0-9%_.=&-]+", path)
                or re.fullmatch(r"/v1/devices/[0-9a-fA-F-]{36}/status", path)):
            raise ValueError("Invalid SmartThings query")
        token = read_private(self.token_file).strip()
        if not 16 <= len(token) <= 4096 or any(ord(c) < 33 or ord(c) > 126 for c in token):
            raise ValueError("Invalid private credential")
        request = urllib.request.Request("https://api.smartthings.com" + path,
            headers={"Authorization": "Bearer " + token, "Accept": "application/json"})
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
        with opener.open(request, timeout=6) as response:
            raw = response.read(1_000_001)
            if len(raw) > 1_000_000:
                raise ValueError("Response too large")
            value = json.loads(raw)
        if not isinstance(value, dict):
            raise ValueError("Invalid response")
        return value

    def catalog(self) -> list[dict]:
        items, path, seen = [], "/v1/devices", set()
        for _ in range(10):
            if path in seen:
                raise ValueError("Pagination loop")
            seen.add(path)
            page = self.get(path)
            rows = page.get("items")
            if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
                raise ValueError("Invalid catalog")
            items.extend(rows)
            if len(items) > 500:
                raise ValueError("Catalog too large")
            following = page.get("_links", {}).get("next", {}).get("href")
            if not following:
                return items
            prefix = "https://api.smartthings.com"
            if not isinstance(following, str) or not following.startswith(prefix + "/v1/devices?"):
                raise ValueError("Pagination outside fixed API")
            path = following[len(prefix):]
        raise ValueError("Pagination limit")

    def status(self, item: dict) -> dict:
        ident = item.get("deviceId")
        if not isinstance(ident, str) or not UUID.fullmatch(ident):
            raise ValueError("Invalid discovered device")
        return self.get("/v1/devices/" + ident + "/status")


class DeviceCatalog:
    def __init__(self, google=None, secondary=None):
        self.google = google or LocalHomeTransport(Path.home() / "google-home-bridge/state/gateway/rio.token")
        self.secondary = secondary or SmartThingsReader()

    def inspect(self) -> dict:
        result = {"devices": [], "providers": {}, "physical_state_confirmed": False}
        try:
            catalog = self.google.catalog()
            bridge = catalog.get("bridge", {})
            ready = (bridge.get("available") is True and bridge.get("permission") == "GRANTED"
                     and bridge.get("bridge_state") == "READY")
            result["providers"]["Google Home"] = "READY" if ready else "UNAVAILABLE"
            rows = catalog.get("devices", []) if ready else []
            if not isinstance(rows, list):
                raise ValueError("Invalid primary catalog")
            for item in rows:
                if not isinstance(item, dict):
                    continue
                name = display_name(item.get("name"))
                if name in EXCLUDED_NAMES:
                    continue
                types = [t.get("device_type") for t in item.get("type_candidates", [])
                         if isinstance(t, dict) and isinstance(t.get("device_type"), str)]
                result["devices"].append({"name": name, "room": display_name(item.get("room"))
                    if item.get("room") else "", "provider": "Google Home", "device_types": types,
                    "state": item.get("state", []), "capabilities": item.get("device_capabilities", []),
                    "enabled_power_commands": item.get("commands", {}),
                    "state_source": "SDK_SNAPSHOT_NOT_PHYSICAL_CONFIRMATION"})
        except Exception:
            result["providers"]["Google Home"] = "UNAVAILABLE"
        try:
            items = [item for item in self.secondary.catalog()
                     if display_name(item.get("label") or item.get("name")) not in EXCLUDED_NAMES]
            def observe(item):
                main = next((c for c in item.get("components", []) if c.get("id") == "main"), {})
                capabilities = [c.get("id") for c in main.get("capabilities", []) if isinstance(c, dict)]
                row = {"name": display_name(item.get("label") or item.get("name")),
                    "provider": "SmartThings", "device_types": [item.get("type")],
                    "capabilities": capabilities, "enabled_power_commands": {},
                    "state_source": "SMARTTHINGS_REPORT_NOT_PHYSICAL_CONFIRMATION", "state": {}}
                try:
                    state = self.secondary.status(item).get("components", {}).get("main", {})
                    # Values/timestamps for device state only; do not expose account,
                    # network, invitation, diagnostics, or manufacturer-specific data.
                    for cap, attr in (("switch", "switch"), ("motionSensor", "motion"),
                            ("illuminanceMeasurement", "illuminance"), ("battery", "battery"),
                            ("presenceSensor", "presence")):
                        if cap in capabilities:
                            value = state.get(cap, {}).get(attr, {})
                            if isinstance(value, dict):
                                row["state"][cap] = {k: value.get(k) for k in ("value", "unit", "timestamp")}
                    row["query_status"] = "READ"
                except Exception:
                    row["query_status"] = "UNAVAILABLE"
                return row
            with ThreadPoolExecutor(max_workers=4) as pool:
                result["devices"].extend(pool.map(observe, items))
            result["providers"]["SmartThings"] = "READ_ONLY"
        except Exception:
            result["providers"]["SmartThings"] = "UNAVAILABLE"
        return result


def handle_device_read(text: str, factory=None) -> str | None:
    inventory = text in {"/home devices", "/home 기기", "기기 목록 보여줘", "전체 기기 상태"}
    match = re.fullmatch(r"/home ([^\r\n]{1,80}) status", text)
    if not inventory and not match:
        return None
    try:
        value = (factory or DeviceCatalog)().inspect()
        requested = match[1].strip() if match else None
        # Display alias only; this does not bind identities across providers.
        requested = {"탁상등": "Mi LED 탁상 램프", "청소기": "Mi Robot Vacuum"}.get(requested, requested)
        rows = value["devices"] if inventory else [d for d in value["devices"] if d["name"] == requested]
        if not rows:
            return "현재 조회할 수 있는 기기를 찾지 못했어요. 연결·권한과 등록 이름을 확인해 주세요."
        lines = ["기기 보고 상태예요. 실제 상태는 별도 확인이 필요해요."]
        if value.get("providers", {}).get("Google Home") == "UNAVAILABLE":
            lines.append("Google Home 기기 목록은 현재 확인되지 않아 보조 상태만 표시해요.")
        for row in rows:
            lines.append(f"{row['name']} · {row['provider']} · {state_text(row)}")
        return "\n".join(lines)
    except Exception:
        return "기기 목록을 확인하지 못했어요. 기기 제어 명령은 보내지 않았어요."


def state_text(row: dict) -> str:
    if row.get("query_status") == "UNAVAILABLE":
        return "상태 조회 실패"
    connections = [cap.get("connectivity") for cap in row.get("capabilities", [])
                   if isinstance(cap, dict) and cap.get("device_type") != "RootNodeDevice"
                   and cap.get("connectivity") in {"ONLINE", "PARTIALLY_ONLINE", "OFFLINE", "UNKNOWN"}]
    # An offline SDK device can still retain attribute values from earlier reads.
    if connections and all(value == "OFFLINE" for value in connections):
        return "오프라인 · 현재 동작 상태 미확인"
    state, parts = row.get("state"), []
    if isinstance(state, list):
        for item in state:
            if not isinstance(item, dict):
                continue
            if item.get("trait") == "OnOff" and type(item.get("on")) is bool:
                parts.append("전원 켜짐" if item["on"] else "전원 꺼짐")
            elif item.get("trait") == "Volume":
                volume = item.get("volume_percent")
                if type(volume) is int and 0 <= volume <= 100:
                    parts.append(f"음량 {volume}%")
                if type(item.get("muted")) is bool:
                    parts.append("음소거" if item["muted"] else "음소거 해제")
            elif item.get("trait") == "MediaPlayback":
                value = item.get("playback_state")
                label = {"Playing": "재생 중", "Paused": "일시 정지", "NotPlaying": "재생 안 함"}.get(value) if isinstance(value, str) else None
                if label:
                    parts.append(label)
            elif item.get("trait") == "OccupancySensing":
                parts.append("움직임 센서 상태 조회됨")
            elif (item.get("trait") == "RvcOperationalState"
                  and type(item.get("operational_state_raw")) is int):
                parts.append("청소기 동작 상태 조회됨")
    elif isinstance(state, dict):
        for cap, names in (("switch", {"on": "전원 켜짐", "off": "전원 꺼짐"}),
                           ("motionSensor", {"active": "움직임 감지", "inactive": "움직임 없음"}),
                           ("presenceSensor", {"present": "기기 재실 보고 있음", "not present": "기기 재실 보고 없음"})):
            item = state.get(cap)
            if isinstance(item, dict) and isinstance(item.get("value"), str) and item["value"] in names:
                parts.append(names[item["value"]])
        for cap, label in (("battery", "배터리"), ("illuminanceMeasurement", "조도")):
            item = state.get(cap)
            if isinstance(item, dict) and type(item.get("value")) in (int, float):
                unit = "%" if cap == "battery" else " lx" if item.get("unit") == "lux" else ""
                parts.append(f"{label} {item['value']}{unit}")
    return " · ".join(dict.fromkeys(parts)) or "조회 가능한 상태 없음"
