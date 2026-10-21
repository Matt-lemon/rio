"""Opt-in official SmartThings CLI OAuth transport for the operator-side bridge.

Credentials remain under the operator account. This module invokes the pinned,
already-installed official binary in an isolated, bounded Docker container.
No implicit login, device discovery as target selection, or retries are allowed.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import tempfile
import uuid

from .smartthings_pc import PcError, SmartThingsPcTransport


class OfficialCliTransport(SmartThingsPcTransport):
    def __init__(self, binding: dict, *, root: Path, expected_sha256: str, control_enabled=False,
                 runner=subprocess.run):
        self.binding = binding
        self.root = Path(root)
        self.control_enabled = control_enabled is True
        self.runner = runner
        if not self.root.is_absolute() or not re.fullmatch(r"[a-f0-9]{64}", expected_sha256):
            raise PcError("CLI_CONFIG_INVALID")
        self._private_directory(self.root)
        for item in ("cli-config", "cli-cache", "cli-data", "cli-state"):
            self._private_directory(self.root / item)
        self._private_directory(self.root / "cli-data/@smartthings")
        self._private_directory(self.root / "cli-data/@smartthings/cli")
        credentials = self.root / "cli-data/@smartthings/cli/credentials.json"
        try:
            info = credentials.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
                raise PcError("CLI_CREDENTIALS_UNSAFE")
        except OSError:
            raise PcError("CLI_CREDENTIALS_UNSAFE") from None
        self.binary = self.root / "official-cli-2.1.2/smartthings"
        try:
            info = self.binary.lstat()
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                    or info.st_mode & 0o022 or not info.st_mode & 0o111):
                raise PcError("CLI_BINARY_UNSAFE")
            hashed = hashlib.sha256()
            with self.binary.open("rb") as binary_stream:
                for block in iter(lambda: binary_stream.read(1024 * 1024), b""):
                    hashed.update(block)
            if hashed.hexdigest() != expected_sha256:
                raise PcError("CLI_BINARY_CHANGED")
        except OSError:
            raise PcError("CLI_BINARY_UNSAFE") from None

    @staticmethod
    def _private_directory(path):
        try:
            info = path.lstat()
            if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
                raise PcError("CLI_DIRECTORY_UNSAFE")
        except OSError:
            raise PcError("CLI_DIRECTORY_UNSAFE") from None

    def request(self, suffix="", *, on=False, command=None):
        if command is not None and (command not in {"on", "off"} or on):
            raise PcError("INVALID_OPERATION")
        operation = "on" if on else command
        if suffix not in {"", "/status", "/commands"} or (suffix == "/commands") != (operation is not None):
            raise PcError("INVALID_OPERATION")
        if operation is not None and not self.control_enabled:
            raise PcError("CLI_CONTROL_DISABLED")
        identity = self.binding["device_id"]
        if suffix == "":
            cli_args = ["devices", identity, "--json", "--output", "/out/result.json"]
        elif suffix == "/status":
            cli_args = ["devices:status", identity, "--json", "--output", "/out/result.json"]
        else:
            cli_args = ["devices:commands", identity, f"main:switch:{operation}"]

        # JSON output is a private temporary file, not log text or stdout.
        with tempfile.TemporaryDirectory(prefix=".rio-st-cli-", dir=self.root) as folder:
            name = "rio-st-cli-" + uuid.uuid4().hex[:16]
            args = ["docker", "run", "--rm", "--name", name, "--pull", "never", "--read-only",
                    "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
                    "--user", f"{os.getuid()}:{os.getgid()}", "--memory", "512m",
                    "--cpus", "0.5", "--pids-limit", "128",
                    "--tmpfs", "/tmp:rw,nosuid,nodev,size=64m",
                    "--log-driver", "none"]
            def mount(src, dest, readonly=False):
                args.extend(["--mount", f"type=bind,src={src},dst={dest}" +
                             (",readonly" if readonly else "")])
            mount(self.binary, "/opt/smartthings", True)
            for src, dest in (("cli-config", "/config"), ("cli-cache", "/cache"),
                              ("cli-data", "/data"), ("cli-state", "/state")):
                mount(self.root / src, dest)
            mount(folder, "/out")
            for key, value in (("XDG_CONFIG_HOME", "/config"), ("XDG_CACHE_HOME", "/cache"),
                               ("XDG_DATA_HOME", "/data"), ("XDG_STATE_HOME", "/state"),
                               ("SMARTTHINGS_PROFILE", "trial")):
                args.extend(["--env", key + "=" + value])
            args += ["--workdir", "/tmp", "--entrypoint", "/opt/smartthings",
                     "python:3.14-slim", *cli_args]
            try:
                completed = self.runner(args, input=b"", stdout=subprocess.PIPE,
                                        stderr=subprocess.PIPE, timeout=30, check=False)
            except subprocess.TimeoutExpired:
                # CLI completion/dispatch is indeterminate; the durable PC ledger
                # blocks any automatic retry after a control request.
                try:
                    self.runner(["docker", "rm", "-f", name], stdin=subprocess.DEVNULL,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                   timeout=5, check=False)
                except (OSError, subprocess.TimeoutExpired):
                    pass
                raise PcError("CLI_TIMEOUT") from None
            except (OSError, ValueError):
                raise PcError("CLI_UNAVAILABLE") from None
            if completed.returncode != 0:
                raise PcError("CLI_RUN_FAILED")
            if operation is not None:
                # CLI exit 0 does not establish an explicit API ACCEPTED receipt.
                # The existing durable request ledger prevents blind retries.
                return {"results": [{"status": "UNKNOWN"}]}
            path = Path(folder) / "result.json"
            try:
                if path.is_symlink() or not path.is_file() or path.stat().st_size > 1_000_000:
                    raise PcError("CLI_INVALID_RESPONSE")
                result = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(result, dict):
                    raise PcError("CLI_INVALID_RESPONSE")
                return result
            except (OSError, ValueError, UnicodeError):
                raise PcError("CLI_INVALID_RESPONSE") from None


def from_environment(binding):
    root = os.environ.get("RIO_ST_CLI_ROOT")
    digest = os.environ.get("RIO_ST_CLI_SHA256", "")
    if not root:
        raise PcError("CLI_CONFIG_INVALID")
    return OfficialCliTransport(binding, root=Path(root), expected_sha256=digest,
                                control_enabled=os.environ.get("RIO_ST_CLI_CONTROL_ENABLED") == "1")
