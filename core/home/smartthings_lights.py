"""Explicit SmartThings observation/backup; ordinary light requests stay on Google Home."""
from __future__ import annotations
import json
import os
from pathlib import Path
import re
import uuid

from .smartthings_pc import PcError, SmartThingsPcTransport, WakeLedger, read_private
from .tools import CommandLedger


class LightTransport(SmartThingsPcTransport):
    def verify_target(self):
        device = self.request()
        if (device.get("deviceId") != self.binding["device_id"]
                or (device.get("label") or device.get("name")) != self.binding["label"]
                or device.get("type") != self.binding["device_type"]):
            raise PcError("TARGET_CHANGED")
        main = [c for c in device.get("components", []) if isinstance(c, dict) and c.get("id") == "main"]
        if len(main) != 1 or not any(c.get("id") == "switch" for c in main[0].get("capabilities", []) if isinstance(c, dict)):
            raise PcError("MAIN_SWITCH_MISSING")


class LightBackup:
    def __init__(self, binding, transport, ledger_path, primary_ledger_path, allow_execution=False):
        self.binding, self.transport = binding, transport
        self.ledger_path, self.primary_ledger_path = ledger_path, primary_ledger_path
        self.allow_execution = allow_execution is True

    def observe(self):
        self.transport.verify_target()
        state = self.transport.switch()
        return {"switch": state["value"], "timestamp": state["timestamp"],
                "binding_verified": self.binding["confirmed_same_physical_light"], "physical_state_confirmed": False}

    def set_power(self, on):
        if type(on) is not bool:
            raise PcError("INVALID_COMMAND")
        if not self.allow_execution or self.binding["confirmed_same_physical_light"] is not True:
            return {"result": "BACKUP_DISABLED", "commands_sent": 0}
        if self.primary_ledger_path.exists():
            ledger = CommandLedger(self.primary_ledger_path)
            try:
                if ledger.db.execute("SELECT 1 FROM light_commands WHERE status IN ('DISPATCHING','DISPATCHED','UNKNOWN') LIMIT 1").fetchone():
                    return {"result": "PRIMARY_PENDING", "commands_sent": 0}
            finally:
                ledger.close()
        state = self.observe()
        with WakeLedger(self.ledger_path) as ledger:
            if state["switch"] == "on":
                ledger.reconcile(self.binding["device_id"])
            else:
                ledger.reconcile_shutdown(self.binding["device_id"])
            if state["switch"] == ("on" if on else "off"):
                return {**state, "result": "ALREADY_REPORTED", "commands_sent": 0}
            key = ledger.reserve(self.binding["device_id"]) if on else ledger.reserve_shutdown(self.binding["device_id"])
            if key is None:
                return {**state, "result": "UNKNOWN", "commands_sent": 0}
            try:
                response = self.transport.on() if on else self.transport.off()
                accepted = (isinstance(response.get("results"), list) and len(response["results"]) == 1
                    and isinstance(response["results"][0], dict) and response["results"][0].get("status") == "ACCEPTED")
                result = "ACCEPTED" if accepted else "UNKNOWN"
            except Exception:
                result = "UNKNOWN"
            ledger.record(key, result)
            return {**state, "result": result, "commands_sent": 1}


def backup_from_environment(target):
    cfg = json.loads(read_private(Path(os.getenv("RIO_HOME_SECONDARY_TARGET_FILE", str(Path.home()/'.config/rio/smartthings-lights.json')))))
    if not isinstance(cfg, dict) or set(cfg) != {"schema_version", "targets"} or type(cfg['schema_version']) is not int or cfg['schema_version'] != 1:
        raise PcError("INVALID_BINDING")
    binding = dict(cfg["targets"][target])
    if (set(binding) != {"device_id", "label", "component", "device_type", "confirmed_same_physical_light"}
            or binding["label"] != target or target not in {"거실", "주방", "샤워실", "세면대"}
            or binding["component"] != "main" or binding["device_type"] not in {"ZIGBEE", "EDGE_CHILD"}
            or type(binding["confirmed_same_physical_light"]) is not bool):
        raise PcError("INVALID_BINDING")
    binding['device_id'] = str(uuid.UUID(binding['device_id']))
    token=read_private(Path(os.getenv('RIO_SMARTTHINGS_TOKEN_FILE',str(Path.home()/'.config/rio/smartthings-token')))).strip()
    return LightBackup(binding,LightTransport(binding,token),
        Path(os.getenv('RIO_HOME_BACKUP_LEDGER',str(Path.home()/'.local/state/rio/home-backup/commands.sqlite3'))),
        Path(os.getenv('RIO_HOME_LEDGER',str(Path.home()/'.local/state/rio/home/example'))),
        os.getenv('RIO_HOME_BACKUP_EXECUTION_ENABLED','false').lower() in {'true','1','yes'})


def handle_secondary_command(text, factory=None):
    match=re.fullmatch(r'/home (secondary|backup) (\S+) (status|on|off)',text)
    if not match:
        return None
    if match[1]=='secondary' and match[3]!='status':
        return '상태 검증 경로는 조회만 지원해요. 보조 제어는 별도 backup 명령을 사용해 주세요.'
    try:
        controller=(factory or backup_from_environment)(match[2])
        if match[3]=='status':
            value=controller.observe()
            return '스마트싱스 보고값: '+('켜짐' if value['switch']=='on' else '꺼짐')+'. '+(
                'Google Home의 같은 조명으로 연결 확인됐어요. 실제 불 상태는 별도 확인이 필요해요.' if value['binding_verified']
                else 'Google Home 조명과 같은 대상인지는 아직 미확정이에요. 보조 제어는 잠겨 있어요.')
        value=controller.set_power(match[3]=='on')
        if value['result']=='ACCEPTED':return '스마트싱스 보조 요청을 보냈어요. 실제 전등 상태는 아직 확인하지 않았어요.'
        if value['result']=='ALREADY_REPORTED':return '스마트싱스가 이미 요청한 상태로 보고해 명령을 보내지 않았어요.'
        if value['result'] in {'UNKNOWN','PRIMARY_PENDING'}:return '앞선 조명 요청의 결과가 불확실해 보조 경로로 재전송하지 않아요.'
        return '스마트싱스 보조 제어는 아직 잠겨 있어요.'
    except Exception:
        return '스마트싱스 보조 대상 설정이나 연결을 확인하지 못했어요. 자동 재전송하지 않아요.'
