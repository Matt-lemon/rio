"""Run each supported suite in its own process and dependency environment."""
import argparse
from pathlib import Path
import subprocess
import sys


SUITES = {
    'root': 'tests',
    'dubbing': 'services/rio-dubbing/tests',
    'tools': 'services/rio-tools/tests',
    'browser': 'services/rio-browser-research/tests',
    'screen': 'services/rio-screen-translate/tests',
}


def command(root: Path, suite: str = 'root') -> list[str]:
    if suite not in SUITES:
        raise ValueError('UNKNOWN_CI_TEST_SUITE')
    gateway = root / 'integrations/google_home/nas/gateway.py'
    if suite == 'root' and not gateway.is_file():
        raise FileNotFoundError('REVIEWED_GATEWAY_SOURCE_REQUIRED_FOR_CI')
    return [sys.executable, '-m', 'pytest', '-q', '-ra', SUITES[suite]]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--suite', choices=SUITES, default='root')
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[2]
    result = subprocess.run(command(root, args.suite), cwd=root, check=False)
    return result.returncode


if __name__ == '__main__':
    raise SystemExit(main())
