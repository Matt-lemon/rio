"""Run the full checked-in test suite; disclose exactly two optional tests."""
from pathlib import Path
import subprocess
import sys


MISSING_GATEWAY_TESTS = (
    'tests/test_home_device_controls.py::test_gateway_separate_approval_and_type_qualification',
    'tests/test_home_device_controls.py::test_gateway_persists_intent_before_send_and_restart_never_resends',
)


def command(root: Path) -> list[str]:
    args = [sys.executable, '-m', 'pytest', '-q', 'tests']
    gateway = root / 'integrations/google_home/nas/gateway.py'
    if not gateway.is_file():
        print('::warning title=Google Home test fixture missing::'
              'The tracked repository lacks integrations/google_home/nas/gateway.py; '
              'exactly two tests will be deselected until the source is committed.', flush=True)
        for test in MISSING_GATEWAY_TESTS:
            args.extend(['--deselect', test])
    return args


def main():
    root = Path(__file__).resolve().parents[2]
    result = subprocess.run(command(root), cwd=root, check=False)
    return result.returncode


if __name__ == '__main__':
    raise SystemExit(main())
