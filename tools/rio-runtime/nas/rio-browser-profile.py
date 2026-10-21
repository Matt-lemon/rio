#!/usr/bin/env python3
"""User launcher for Rio's separate, ordinary Edge profile.

No existing browser profile, cookies, login state or security flags are copied.
This launcher does not enable automation or remote debugging.
"""
from pathlib import Path
import argparse
import json
import os
import shutil
import subprocess
import sys

PROFILE = Path.home()/'.local/share/rio-browser/edge-profile'
HOME = 'https://www.google.com/'
EXTENSION = 'https://chromewebstore.google.com/detail/playwright-extension/mmlmfjhmonkocbjadbfplnigmagldckm'


def main():
    parser=argparse.ArgumentParser(description='Rio 전용 Edge 프로필')
    parser.add_argument('--prepare',action='store_true',help='Prepare the private directory without opening a window')
    parser.add_argument('--status',action='store_true',help='Show non-sensitive launcher configuration')
    parser.add_argument('--setup-playwright',action='store_true',help='Open the official extension store in the Rio profile for user installation')
    mode=parser.add_mutually_exclusive_group()
    mode.add_argument('--prepare-direct',action='store_true',help='Prepare extension-free modes without launching a browser')
    mode.add_argument('--managed',action='store_true',help='Start Playwright-owned dedicated Edge and select it after readiness')
    mode.add_argument('--cdp',action='store_true',help='Start a separate loopback-debug Edge and connect after identity checks')
    mode.add_argument('--use-extension',action='store_true',help='Select the existing extension connection again')
    args=parser.parse_args()
    selected='prepare' if args.prepare_direct else 'managed' if args.managed else 'cdp' if args.cdp else 'extension' if args.use_extension else None
    if selected:
        os.execv(sys.executable,[sys.executable,str(Path(__file__).with_name('rio-browser-direct.py')),selected])
    os.umask(0o077)
    executable=shutil.which('microsoft-edge') or shutil.which('microsoft-edge-stable')
    if not executable:raise SystemExit('Microsoft Edge를 찾을 수 없습니다.')
    if PROFILE.is_symlink():raise SystemExit('전용 프로필 경로가 심볼릭 링크입니다.')
    PROFILE.mkdir(parents=True,exist_ok=True,mode=0o700)
    if PROFILE.stat().st_uid!=os.getuid():raise SystemExit('전용 프로필 소유자가 다릅니다.')
    PROFILE.chmod(0o700)
    if args.prepare or args.status:
        print(json.dumps({'profile':str(PROFILE),'directory_prepared':True,'edge':executable,
                          'profile_initialized':(PROFILE/'Local State').exists(),
                          'automation_enabled_by_launcher':False},ensure_ascii=False))
        return
    subprocess.Popen([executable,'--user-data-dir='+str(PROFILE),'--new-window',EXTENSION if args.setup_playwright else HOME],
                     stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL,start_new_session=True)


if __name__=='__main__':main()
