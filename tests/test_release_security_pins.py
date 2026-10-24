"""Prevent regressions in reviewed release dependency and CI pinning."""
from pathlib import Path
import re


ROOT = Path(__file__).resolve().parents[1]


def test_preview_wheel_locks_require_artifact_hashes():
    for name in ('requirements-preview.lock',
                 'services/rio-tools/requirements-preview.lock'):
        lines = (ROOT / name).read_text().splitlines()
        reqs = [line for line in lines if line.strip() and not line.startswith('#')]
        assert reqs
        assert all(re.fullmatch(r'[A-Za-z0-9_.-]+==[A-Za-z0-9_.+-]+ '
                                r'--hash=sha256:[0-9a-f]{64}', line) for line in reqs)
    runner = (ROOT / 'tools/ci/release-preview.sh').read_text()
    assert runner.count('--require-hashes --only-binary=:all:') == 2


def test_workflows_reference_immutable_actions_not_mutable_tags():
    for workflow in (ROOT / '.github/workflows/ci.yml',
                     ROOT / '.github/workflows/nix-preview.yml'):
        for line in workflow.read_text().splitlines():
            if '- uses:' in line and 'actions/' in line:
                assert re.search(r'actions/[a-z-]+@[0-9a-f]{40}(?:\s|$)', line)


def test_nix_security_floor_cannot_be_removed_silently():
    source = (ROOT / 'flake.nix').read_text()
    for floor in ('ps.anyio.version "4.14.2"',
                  'ps.certifi.version "2026.10.19"',
                  'ps.urllib3.version "2.8.0"',
                  'pkgs.cacert.version "3.131"'):
        assert floor in source
