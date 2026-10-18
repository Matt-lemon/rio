"""Compile tracked Python source in memory without executing any module."""
from pathlib import Path
import subprocess

root = Path(__file__).resolve().parents[2]
source_files = subprocess.run(['git', 'ls-files', '-z', '--', '*.py'], cwd=root,
                              stdout=subprocess.PIPE, check=True).stdout.split(b'\0')
checked = 0
for raw in source_files:
    if not raw:
        continue
    file = root / raw.decode('utf-8')
    compile(file.read_bytes(), str(file), 'exec')
    checked += 1
print('COMPILE_OK', checked)
