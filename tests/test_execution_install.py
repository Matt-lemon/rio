import importlib.util
import stat
from pathlib import Path

import pytest


@pytest.fixture
def installer():
    path = Path(__file__).resolve().parents[1]/'tools/rio-runtime/nas/rio-core-install.py'
    spec = importlib.util.spec_from_file_location('rio_core_installer_test', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_install_preserves_link_to_the_exact_reviewed_launcher(tmp_path, installer):
    source = tmp_path/'source.py'
    source.write_text('reviewed source')
    target = tmp_path/'rio-main'
    target.symlink_to(source)
    backup = tmp_path/'backup'
    backup.mkdir()
    installer.install_file(source, target, backup, 0o755)
    assert target.is_symlink()
    assert target.read_text() == 'reviewed source'
    assert (backup/'rio-main.symlink').exists()


def test_unknown_link_is_rejected_without_touching_its_target(tmp_path, installer):
    source = tmp_path/'source.py'
    source.write_text('new source')
    other = tmp_path/'other.py'
    other.write_text('preserve')
    target = tmp_path/'rio-main'
    target.symlink_to(other)
    with pytest.raises(ValueError):
        installer.install_file(source, target, tmp_path, 0o755)
    assert other.read_text() == 'preserve'
    assert target.is_symlink()


def test_selection_uses_own_private_root_without_changing_shared_config(tmp_path, installer, monkeypatch):
    config = tmp_path/'.config/rio'
    config.mkdir(parents=True)
    config.chmod(0o775)
    monkeypatch.setattr(installer.Path, 'home', lambda: tmp_path)
    monkeypatch.setattr(installer.sys, 'argv', ['installer', '--use-legacy'])
    monkeypatch.setattr(installer.os, 'umask', lambda value: None)
    installer.main()
    selection = tmp_path/'.config/rio-execution/selection.json'
    assert stat.S_IMODE(config.stat().st_mode) == 0o775
    assert stat.S_IMODE(selection.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(selection.stat().st_mode) == 0o600
    from core.execution.client import enabled
    monkeypatch.delenv('RIO_EXECUTION_CORE', raising=False)
    assert enabled() is False
    selection.write_text('{"enabled":true}')
    assert enabled() is True
