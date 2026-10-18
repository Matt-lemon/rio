"""All CLI adapter tests are hermetic: no Docker/HTTP or real credential reads."""
import hashlib
import json
from pathlib import Path
import subprocess

import pytest

from core.home.smartthings_cli import OfficialCliTransport
from core.home.smartthings_pc import PcController, PcError, WakeLedger

DEVICE = "12345678-1234-4234-8234-123456789abc"
BINDING = {"device_id": DEVICE, "label": "컴퓨터", "component": "main",
           "confirmed_pc_virtual_switch": True, "pc_host": "192.0.2.129"}


@pytest.fixture
def root(tmp_path):
    base = tmp_path / "private"
    base.mkdir(mode=0o700)
    for name in ("cli-config", "cli-cache", "cli-data", "cli-state"):
        (base / name).mkdir(mode=0o700)
    cred = base / "cli-data/@smartthings/cli"
    cred.mkdir(parents=True)
    cred.chmod(0o700)
    cred.parent.chmod(0o700)
    (cred / "credentials.json").write_text('{"placeholder":"FAKE_ONLY"}')
    (cred / "credentials.json").chmod(0o600)
    binary = base / "official-cli-2.1.2"
    binary.mkdir()
    (binary / "smartthings").write_bytes(b"fake cli executable")
    (binary / "smartthings").chmod(0o755)
    return base


def digest(root):
    return hashlib.sha256((root / "official-cli-2.1.2/smartthings").read_bytes()).hexdigest()


class FakeRunner:
    def __init__(self, *, exit_code=0):
        self.calls = []
        self.exit_code = exit_code

    def __call__(self, args, **kwargs):
        self.calls.append((args, kwargs))
        out = [s for s in args if s.startswith("type=bind,src=") and s.endswith(",dst=/out")]
        assert len(out) == 1
        folder = Path(out[0].split("src=", 1)[1].rsplit(",dst=/out", 1)[0])
        if "devices:status" in args:
            result = {"components":{"main":{"switch":{"switch":{"value":"off","timestamp":"t"}}}}}
        else:
            result = {"deviceId": DEVICE, "label": "컴퓨터",
                      "components":[{"id":"main","capabilities":[{"id":"switch"}]}]}
        (folder / "result.json").write_text(json.dumps(result))
        return subprocess.CompletedProcess(args, self.exit_code, stdout=b"SECRET!", stderr=b"NEVER_LOG")


def test_cli_read_only_observation_and_isolation(root, tmp_path):
    runner = FakeRunner()
    transport = OfficialCliTransport(BINDING, root=root, expected_sha256=digest(root), runner=runner)
    controller = PcController(BINDING, transport, tmp_path / "ledger.db", lambda _: False)
    value = controller.observe()
    assert value["switch"] == "off" and value["pc_ssh_reachable"] is False
    assert len(runner.calls) == 2
    args = runner.calls[0][0]
    for flag in ("--read-only", "--cap-drop", "--security-opt", "--pull", "never", "--log-driver"):
        assert flag in args
    assert args[-4:] == [DEVICE, "--json", "--output", "/out/result.json"]
    assert b"SECRET" not in str(value).encode()
    assert not list(root.glob(".rio-st-cli-*"))


def test_cli_readonly_never_sends_control_or_reserves(root, tmp_path):
    runner = FakeRunner()
    transport = OfficialCliTransport(BINDING, root=root, expected_sha256=digest(root), runner=runner)
    path = tmp_path / "ledger.db"
    controller = PcController(BINDING, transport, path, lambda _: False)
    value = controller.wake()
    assert value["result"] == "REJECTED" and value["commands_sent"] == 0
    assert value["code"] == "CLI_CONTROL_DISABLED"
    assert len(runner.calls) == 2
    with WakeLedger(path) as ledger:
        assert ledger.db.execute("SELECT COUNT(*) FROM wake_requests").fetchone()[0] == 0
    with pytest.raises(PcError, match="CLI_CONTROL_DISABLED"):
        transport.on()


def test_opt_in_command_is_unknown_without_backend_receipt_and_never_retried(root, tmp_path):
    runner = FakeRunner()
    transport = OfficialCliTransport(BINDING, root=root, expected_sha256=digest(root),
                                     control_enabled=True, runner=runner)
    path = tmp_path / "ledger.db"
    controller = PcController(BINDING, transport, path, lambda _: False)
    result = controller.wake()
    assert result["result"] == "UNKNOWN" and result["commands_sent"] == 1
    assert len(runner.calls) == 3
    assert runner.calls[-1][0][-3:] == [ "devices:commands", DEVICE, "main:switch:on"]
    with WakeLedger(path) as ledger:
        assert ledger.db.execute("SELECT state FROM wake_requests").fetchone()[0] == "UNKNOWN"
    result2 = controller.wake()
    assert result2["result"] == "PENDING_DO_NOT_RETRY"
    assert len(runner.calls) == 5  # two subsequent reads only, no extra command


def test_bad_cli_hash_and_credentials_permissions_fail_closed(root):
    runner = FakeRunner()
    with pytest.raises(PcError, match="CLI_BINARY_CHANGED"):
        OfficialCliTransport(BINDING, root=root, expected_sha256="0"*64, runner=runner)
    creds = root / "cli-data/@smartthings/cli/credentials.json"
    creds.chmod(0o644)
    with pytest.raises(PcError, match="CLI_CREDENTIALS_UNSAFE"):
        OfficialCliTransport(BINDING, root=root, expected_sha256=digest(root), runner=runner)
    assert runner.calls == []


def test_cli_failure_never_reports_success_or_secrets(root, tmp_path):
    runner = FakeRunner(exit_code=1)
    transport = OfficialCliTransport(BINDING, root=root, expected_sha256=digest(root), runner=runner)
    with pytest.raises(PcError, match="^CLI_RUN_FAILED$") as error:
        transport.verify_target()
    assert "SECRET" not in str(error.value)


def test_cli_commands_are_strict_and_control_gate_is_explicit(root):
    runner = FakeRunner()
    transport = OfficialCliTransport(BINDING, root=root, expected_sha256=digest(root), runner=runner)
    for kwargs in ({"suffix":"/foobar"}, {"suffix":"/commands", "command":"reset"},
                   {"suffix":"/commands"}, {"suffix":"/status", "on":True}):
        with pytest.raises(PcError):
            transport.request(**kwargs)
    assert runner.calls == []


def test_private_operator_selection_uses_cli_without_pat(root, tmp_path, monkeypatch):
    import core.home.smartthings_pc as pc
    monkeypatch.setenv('HOME', str(tmp_path))
    monkeypatch.delenv('RIO_PC_TRANSPORT', raising=False)
    monkeypatch.setattr(pc, 'load_binding', lambda path: BINDING)
    settings = tmp_path / '.config/rio'
    settings.mkdir(parents=True)
    settings.chmod(0o700)
    chosen = settings / 'pc-auth-provider.json'
    chosen.write_text(json.dumps({'schema_version': 1, 'provider': 'oauth-cli',
                                 'cli_root': str(root), 'cli_sha256': digest(root)}))
    chosen.chmod(0o600)
    instance = pc.controller_from_environment()
    assert isinstance(instance.transport, OfficialCliTransport)
    assert instance.transport.control_enabled is False


def test_operator_selection_rejects_extra_fields_or_exposed_file(root, tmp_path, monkeypatch):
    import core.home.smartthings_pc as pc
    monkeypatch.setenv('HOME', str(tmp_path))
    monkeypatch.delenv('RIO_PC_TRANSPORT', raising=False)
    monkeypatch.setattr(pc, 'load_binding', lambda path: BINDING)
    settings = tmp_path / '.config/rio'
    settings.mkdir(parents=True)
    settings.chmod(0o700)
    chosen = settings / 'pc-auth-provider.json'
    profile = {'schema_version': 1, 'provider': 'oauth-cli',
               'cli_root': str(root), 'cli_sha256': digest(root)}
    chosen.write_text(json.dumps(dict(profile, accidental_control=True)))
    chosen.chmod(0o600)
    with pytest.raises(PcError, match='INVALID_AUTH_PROVIDER'):
        pc.controller_from_environment()
    chosen.write_text(json.dumps(profile))
    chosen.chmod(0o644)
    with pytest.raises(PcError, match='PRIVATE_FILE_REQUIRED'):
        pc.controller_from_environment()


def test_timeout_cleanup_uses_injected_runner_and_never_retries_command(root):
    calls=[]
    def runner(args,**kwargs):
        calls.append(args)
        if args[:2]==['docker','run']:
            raise subprocess.TimeoutExpired(args,30)
        return subprocess.CompletedProcess(args,0)
    transport=OfficialCliTransport(BINDING,root=root,expected_sha256=digest(root),control_enabled=True,runner=runner)
    with pytest.raises(PcError,match='CLI_TIMEOUT'):
        transport.request('/commands',command='on')
    assert len(calls)==2
    assert calls[0][-3:]==['devices:commands',DEVICE,'main:switch:on']
    assert calls[1][:3]==['docker','rm','-f']
