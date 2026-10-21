"""No Docker or shell access: the Core talks only to a private Unix socket."""
import json
import os
from pathlib import Path
import socket
import stat
from .contracts import ToolError, canonical


class ToolClient:
    def __init__(self, socket_path=None):
        self.path = Path(socket_path or os.getenv('RIO_TOOLS_SOCKET') or Path.home()/'.local/state/rio/tools/service.sock')

    def request(self, action, **arguments):
        if action not in {'list', 'describe', 'call', 'job', 'build', 'validation', 'cancel'}:
            raise ToolError('PUBLIC_ACTION_FORBIDDEN')
        info = self.path.lstat()
        if not stat.S_ISSOCK(info.st_mode) or info.st_mode & 0o077:
            raise ToolError('SOCKET_PERMISSION')
        payload = canonical({'action': action, **arguments})+b'\n'
        if len(payload) > 2*1024*1024:
            raise ToolError('PAYLOAD_LIMIT')
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(10)
            connection.connect(str(self.path))
            connection.sendall(payload)
            chunks = bytearray()
            while not chunks.endswith(b'\n'):
                part = connection.recv(65536)
                if not part or len(chunks)+len(part) > 2*1024*1024:
                    raise ToolError('INVALID_SERVICE_RESPONSE')
                chunks.extend(part)
        return json.loads(chunks)
