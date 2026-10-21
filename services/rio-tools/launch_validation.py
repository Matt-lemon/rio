"""Launch the independent main.py with a private, operator-prepared connection."""
import json
import os
from pathlib import Path
import stat
import sys

def main():
    path=Path.home()/'.config/rio/tools-validation-20261014/connection.json'
    info=path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid!=os.getuid() or info.st_mode & 0o077:
        raise SystemExit('Private validation connection file required.')
    config=json.loads(path.read_text())
    root=Path(__file__).resolve().parents[2]
    if root!=Path(config['worktree']).resolve():raise SystemExit('Validation worktree mismatch.')
    os.environ['RIO_TOOLS_SOCKET']=config['socket']
    os.chdir(root)
    os.execv(sys.executable,[sys.executable,str(root/'main.py'),'--tools-session'])

if __name__=='__main__':main()
