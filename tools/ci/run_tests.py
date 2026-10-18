"""Run full integration tests; never hide missing gateway contract coverage."""
from pathlib import Path
import subprocess
import sys


def command(root: Path) -> list[str]:
    gateway = root / 'integrations/google_home/nas/gateway.py'
    if not gateway.is_file():
        raise FileNotFoundError('REVIEWED_GATEWAY_SOURCE_REQUIRED_FOR_CI')
    return [sys.executable, '-m', 'pytest', '-q', 'tests']


def main():
    root = Path(__file__).resolve().parents[2]
    result = subprocess.run(command(root), cwd=root, check=False)
    return result.returncode


if __name__ == '__main__':
    raise SystemExit(main())
