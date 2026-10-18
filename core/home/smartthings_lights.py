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
        components = device.get('components')
        if not isinstance(components,list):
            raise PcError('MAIN_SWITCH_MISSING')
        main = [c for c in components if isinstance(c, dict) and c.get("id") == "main"]
        capabilities = main[0].get('capabilities') if len(main)==1 else None
        switch = [c for c in capabilities if isinstance(c,dict) and c.get('id')=='switch'] if isinstance(capabilities,list) else []
        if len(main) != 1 or len(switch)!=1 or type(switch[0].get('version')) is not int or switch[0]['version']!=1:
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

    def set_power(self, on, request_key=None):
        if type(on) is not bool:
            raise PcError("INVALID_COMMAND")
        if not self.allow_execution or self.binding["confirmed_same_physical_light"] is not True:
            return {"result": "BACKUP_DISABLED", "commands_sent": 0}
        from .smartthings_direct import DirectLight
        self.transport.control_enabled = self.allow_execution
        controller=DirectLight(self.binding['label'],self.binding,self.transport,self.ledger_path,
            self.allow_execution,primary_ledger_path=self.primary_ledger_path,
            revalidate=getattr(self,'revalidate',None))
        result=controller.set_power(on,request_key=request_key)
        if result.get('code')=='PRIMARY_UNRESOLVED':result['result']='PRIMARY_PENDING'
        elif result['result']=='PENDING_DO_NOT_RETRY':result['result']='UNKNOWN'
        if result.get('api_acceptance_verified') is True and result['result']=='UNKNOWN':result['result']='ACCEPTED'
        return result


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
    controller=LightBackup(binding,LightTransport(binding,token),
        Path(os.getenv('RIO_HOME_BACKUP_LEDGER',str(Path.home()/'.local/state/rio/home-backup/commands.sqlite3'))),
        Path(os.getenv('RIO_HOME_LEDGER',str(Path.home()/'.local/state/rio/home/example'))),
        os.getenv('RIO_HOME_BACKUP_EXECUTION_ENABLED','false').lower() in {'true','1','yes'})
    original=dict(binding)
    def revalidate():
        latest=json.loads(read_private(Path(os.getenv('RIO_HOME_SECONDARY_TARGET_FILE',str(Path.home()/'.config/rio/smartthings-lights.json')))))
        return latest.get('targets',{}).get(target)==original and os.getenv('RIO_HOME_BACKUP_EXECUTION_ENABLED','false').lower() in {'true','1','yes'}
    controller.revalidate=revalidate
    return controller


def handle_secondary_command(text, factory=None, *, request_key=None):
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
        value=controller.set_power(match[3]=='on',request_key=request_key)
        if value['result']=='ACCEPTED':return '스마트싱스 보조 요청을 보냈어요. 실제 전등 상태는 아직 확인하지 않았어요.'
        if value['result']=='ALREADY_REPORTED':return '스마트싱스가 이미 요청한 상태로 보고해 명령을 보내지 않았어요.'
        if value['result'] in {'UNKNOWN','PRIMARY_PENDING'}:return '앞선 조명 요청의 결과가 불확실해 보조 경로로 재전송하지 않아요.'
        return '스마트싱스 보조 제어는 아직 잠겨 있어요.'
    except Exception:
        return '스마트싱스 보조 대상 설정이나 연결을 확인하지 못했어요. 자동 재전송하지 않아요.'
