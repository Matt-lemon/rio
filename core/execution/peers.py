"""Root-owned identity policy for the optional account-separated runtime."""
from __future__ import annotations

import json
import os
from pathlib import Path
import socket
import stat
import struct
import subprocess

from .types import encoded

POLICY=Path('/etc/rio-execution/peers.json')
CORE_SOCKET='/run/rio-execution-core/service.sock'
BRIDGE_SOCKET='/run/rio-execution-bridge/service.sock'


def validate_policy(value):
    required={'version','core_uid','operator_uid','core_socket','bridge_socket','operator_project','release'}
    if not isinstance(value,dict) or set(value)!=required or type(value['version']) is not int or value['version']!=1:
        raise ValueError('INVALID_PEER_POLICY')
    if any(type(value[k]) is not int or value[k]<=0 for k in ('core_uid','operator_uid')) or value['core_uid']==value['operator_uid']:
        raise ValueError('SEPARATE_ACCOUNTS_REQUIRED')
    if value['core_socket']!=CORE_SOCKET or value['bridge_socket']!=BRIDGE_SOCKET:
        raise ValueError('FIXED_PEER_ENDPOINT_REQUIRED')
    if not isinstance(value['operator_project'],str) or not Path(value['operator_project']).is_absolute():
        raise ValueError('INVALID_OPERATOR_PROJECT')
    if not isinstance(value['release'],str) or not value['release'].startswith('/opt/rio-core/releases/') or '..' in Path(value['release']).parts:
        raise ValueError('ROOT_OWNED_RELEASE_REQUIRED')
    return value


def load_policy(path=POLICY):
    path=Path(path)
    for parent in path.parents:
        info=parent.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid!=0 or info.st_mode&0o022:
            raise ValueError('ROOT_OWNED_PEER_POLICY_REQUIRED')
    fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
    with os.fdopen(fd,'rb') as stream:
        info=os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid!=0 or info.st_mode&0o022 or info.st_size>8192:
            raise ValueError('ROOT_OWNED_PEER_POLICY_REQUIRED')
        return validate_policy(json.load(stream))


def check_socket(path, owner):
    path=Path(path);info=path.lstat();parent=path.parent.lstat()
    if not stat.S_ISDIR(parent.st_mode) or parent.st_uid!=owner or parent.st_mode&0o022:
        raise ValueError('PEER_DIRECTORY_UNSAFE')
    if not stat.S_ISSOCK(info.st_mode) or info.st_uid!=owner or stat.S_IMODE(info.st_mode) not in {0o600,0o660}:
        raise ValueError('PEER_SOCKET_UNSAFE')


def peer_uid(connection):
    if not hasattr(socket,'SO_PEERCRED'):raise ValueError('PEER_CREDENTIALS_REQUIRED')
    return struct.unpack('3i',connection.getsockopt(socket.SOL_SOCKET,socket.SO_PEERCRED,12))[1]


def grant_socket(path,uid):
    check_socket(path,os.getuid())
    if type(uid) is not int or uid<=0 or uid==os.getuid():raise ValueError('SEPARATE_PEER_REQUIRED')
    subprocess.run(['/usr/bin/setfacl','-m','u:'+str(uid)+':rw','--',str(path)],check=True,
                   stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=3)


class PeerClient:
    def __init__(self,path,owner,timeout=75):
        self.path,self.owner,self.timeout=Path(path),owner,timeout

    def request(self,op,**arguments):
        check_socket(self.path,self.owner)
        payload=(encoded({'op':op,**arguments})+'\n').encode()
        if len(payload)>65536:raise ValueError('REQUEST_LIMIT')
        with socket.socket(socket.AF_UNIX,socket.SOCK_STREAM) as connection:
            connection.settimeout(self.timeout);connection.connect(str(self.path))
            if peer_uid(connection)!=self.owner:raise ValueError('PEER_OWNER_CHANGED')
            connection.sendall(payload);raw=bytearray()
            while not raw.endswith(b'\n'):
                block=connection.recv(65536)
                if not block or len(raw)+len(block)>524288:raise ValueError('RESPONSE_LIMIT')
                raw.extend(block)
        value=json.loads(raw)
        if value.get('ok') is not True:raise ValueError(value.get('code','PEER_UNAVAILABLE'))
        return value['result']
