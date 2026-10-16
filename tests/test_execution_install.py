import importlib.util
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
