"""Private local IPC. A failed submission is never rerouted to the old executor."""
from __future__ import annotations

import json
import os
from pathlib import Path
import socket
import stat
import struct

from .types import encoded


def enabled():
    value = os.getenv('RIO_EXECUTION_CORE')
    if value is not None:
        return value.lower() in {'1', 'true', 'yes', 'on'}
    path = Path.home()/'.config/rio-execution/selection.json'
    try:
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077 or info.st_size > 4096:
            return False
        return json.loads(path.read_text()).get('enabled') is True
    except (OSError, ValueError, AttributeError):
        return False


class CoreClient:
    def __init__(self, path=None):
        selected=None
        if path is None and 'RIO_CORE_SOCKET' not in os.environ:
            selection=Path.home()/'.config/rio-execution/selection.json'
            try:
                info=selection.lstat()
                if stat.S_ISREG(info.st_mode) and info.st_uid==os.getuid() and not info.st_mode&0o077 and info.st_size<=4096:
                    selected=json.loads(selection.read_text()).get('transport')
            except (OSError,ValueError,AttributeError):pass
        from .peers import CORE_SOCKET,PeerClient,load_policy
        self.path = Path(path or os.getenv('RIO_CORE_SOCKET',CORE_SOCKET if selected=='isolated' else str(Path.home()/'.local/state/rio/core/service.sock')))
        self.peer=None
        if str(self.path)==CORE_SOCKET:
            policy=load_policy()
            if os.getuid()!=policy['operator_uid']:raise ValueError('OPERATOR_ACCOUNT_REQUIRED')
            self.peer=PeerClient(self.path,policy['core_uid'],timeout=8)

    def request(self, op, **args):
        if self.peer is not None:return self.peer.request(op,**args)
        info = self.path.lstat()
        if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600:
            raise ValueError('PRIVATE_CORE_REQUIRED')
        data = (encoded({'op': op, **args}) + '\n').encode()
        if len(data) > 65536:
            raise ValueError('REQUEST_LIMIT')
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(8)
            connection.connect(str(self.path))
            if hasattr(socket, 'SO_PEERCRED'):
                _, uid, _ = struct.unpack('3i', connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
                if uid != os.getuid():
                    raise ValueError('CORE_OWNER_CHANGED')
            connection.sendall(data)
            raw = bytearray()
            while not raw.endswith(b'\n'):
                block = connection.recv(65536)
                if not block or len(raw) + len(block) > 524288:
                    raise ValueError('RESPONSE_LIMIT')
                raw.extend(block)
        result = json.loads(raw)
        if result.get('ok') is not True:
            raise ValueError(result.get('code', 'CORE_UNAVAILABLE'))
        return result['result']
