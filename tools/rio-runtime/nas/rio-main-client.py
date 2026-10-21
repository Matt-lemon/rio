#!/usr/bin/env python3
"""Attach a local terminal to the integrated Rio service; never resend input."""
from __future__ import annotations

import os
from pathlib import Path
import select
import socket
import stat
import sys


def main() -> int:
    if sys.argv[1:] not in ([], ['--voice'], ['--text-only']):
        print("사용법: rio-main [--voice | --text-only] (접속 후 요청 입력, /exit로 종료)", file=sys.stderr)
        return 2
    project = Path('/home/example/rio-nas-pc-integration-20261012')
    sys.path.insert(0, str(project))
    from core.execution.client import enabled
    if enabled():
        os.execv(sys.executable, [sys.executable, str(project/'main.py'), '--core',
                                '--voice' if sys.argv[1:] == ['--voice'] else '--text-only'])
    if sys.argv[1:] == ['--voice']:
        print("음성 접속은 실행 코어 연결이 필요해요. 기존 실행기로 자동 전환하지 않아요.", file=sys.stderr)
        return 1
    runtime = Path(os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}"))
    path = runtime / "rio-main.sock"
    try:
        directory = runtime.lstat()
        endpoint = path.lstat()
        if (not stat.S_ISDIR(directory.st_mode) or directory.st_uid != os.getuid()
                or directory.st_mode & 0o077 or not stat.S_ISSOCK(endpoint.st_mode)
                or endpoint.st_uid != os.getuid() or endpoint.st_mode & 0o077):
            raise OSError("Private Rio socket required")
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.connect(str(path))

            inputs = [connection, sys.stdin.fileno()]
            saw_output = False
            try:
                while True:
                    readable, _, _ = select.select(inputs, [], [])
                    if connection in readable:
                        chunk = connection.recv(65536)
                        if not chunk:
                            break
                        saw_output = True
                        sys.stdout.buffer.write(chunk)
                        sys.stdout.buffer.flush()
                    if sys.stdin.fileno() in readable:
                        chunk = os.read(sys.stdin.fileno(), 65536)
                        if chunk:
                            connection.sendall(chunk)
                        else:
                            connection.shutdown(socket.SHUT_WR)
                            inputs.remove(sys.stdin.fileno())
            except KeyboardInterrupt:
                # Closing the session ends this main.py process, never other services.
                pass
            if not saw_output:
                raise OSError("Rio session unavailable")
        return 0
    except (OSError, BrokenPipeError):
        print("리오 서비스에 연결하지 못했어요. 접속 중인 세션이나 서비스 상태를 확인해 주세요.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
