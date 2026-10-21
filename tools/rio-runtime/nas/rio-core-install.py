#!/usr/bin/env python3
"""Install only the new core; switch new connections without stopping old chats."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
from core.execution.client import CoreClient
from core.execution.store import private_directory
from core.execution.types import source_revision


def atomic_copy(content, target, mode):
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.is_symlink():
        raise ValueError('Refusing a symbolic link at installation target')
    fd, temporary = tempfile.mkstemp(prefix='.rio-install-', dir=target.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, target)
    finally:
        Path(temporary).unlink(missing_ok=True)


def unit(*arguments):
    subprocess.run(['/usr/bin/systemctl', '--user', *arguments], check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=15)


def install_file(source, target, backup, mode):
    if target.is_symlink():
        if target.resolve() != source.resolve():
            raise ValueError('Installation link does not point to the reviewed source')
        # The existing launcher is intentionally linked to this exact repository
        # helper. Preserve that user-managed link instead of replacing it.
        (backup/(target.name + '.symlink')).write_text(os.readlink(target))
        return
    if target.exists():
        shutil.copy2(target, backup/target.name)
    atomic_copy(source.read_bytes(), target, mode)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--install', action='store_true')
    selection = parser.add_mutually_exclusive_group()
    selection.add_argument('--use-core', action='store_true')
    selection.add_argument('--use-legacy', action='store_true')
    args = parser.parse_args()
    os.umask(0o077)
    if args.install:
        backup = private_directory(Path.home()/'.local/state/rio/core-install-backups'/str(time.time_ns()))
        templates = ROOT/'tools/rio-runtime/nas'
        files = [(templates/'rio-execution-core.service', Path.home()/'.config/systemd/user/rio-execution-core.service', 0o644),
                 (templates/'rio-main-client.py', Path.home()/'.local/bin/rio-main', 0o755)]
        for source, target, mode in files:
            if target.is_symlink() and target.resolve() != source.resolve():
                raise ValueError('Unexpected installation link; no files replaced')
        for source, target, mode in files:
            install_file(source, target, backup, mode)
        unit('daemon-reload')
        unit('enable', '--now', 'rio-execution-core.service')
        print('New core installed; existing conversations and services preserved.')
    if args.use_core:
        client = CoreClient()
        deadline = time.monotonic() + 8
        while True:
            try:
                health = client.request('health')
                if health.get('ready') and health.get('source_revision') == source_revision():
                    break
                raise ValueError('Running core differs from reviewed source; no hot restart performed')
            except OSError:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(.2)
        directory = private_directory(Path.home()/'.config/rio-execution')
        atomic_copy(json.dumps({'enabled': True, 'version': 1}).encode(), directory/'selection.json', 0o600)
        print('New conversations use the core. No existing conversation was disconnected.')
    if args.use_legacy:
        directory = private_directory(Path.home()/'.config/rio-execution')
        atomic_copy(json.dumps({'enabled': False, 'version': 1}).encode(), directory/'selection.json', 0o600)
        print('New conversations use the prior executor. Task/effect records and running core are preserved.')


if __name__ == '__main__':
    main()
