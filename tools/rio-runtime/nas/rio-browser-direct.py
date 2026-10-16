#!/usr/bin/env python3
"""Human-invoked setup/launcher for dedicated extension-free Rio browsers."""
import argparse
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import time
import tempfile

PROJECT=Path(__file__).resolve().parents[3]
sys.path.insert(0,str(PROJECT))
from core.browser_transport import (STATE,BASE,MODES,CDP_PORT,profile,endpoint,
    profile_label,private_directory,cdp_identity,process_start,write_selection,selected_mode)

RUNTIME=BASE/'venv/bin/python'
HOME='https://www.google.com/'


def prepare():
    private_directory(STATE)
    BASE.mkdir(parents=True,exist_ok=True,mode=0o700)
    if BASE.is_symlink() or BASE.stat().st_uid!=os.getuid():raise ValueError('리오 브라우저 폴더의 소유자를 확인해 주세요.')
    for mode in ('managed','cdp'):private_directory(profile(mode))
    return {'selected':selected_mode(),'prepared':['managed','cdp'],
            'profiles':{m:str(profile(m)) for m in ('managed','cdp')},
            'browsers_launched':0,'existing_profile_copied':False,'live_verified':False}


def cdp_browser():
    prepare()
    try:cdp_identity();return
    except (OSError,ValueError,KeyError):pass
    if os.path.lexists(profile('cdp')/'SingletonLock'):
        raise ValueError('디버깅 전용 프로필이 사용 중이에요. 그 창만 닫은 뒤 실행해 주세요.')
    with socket.socket() as check:check.bind(('127.0.0.1',CDP_PORT))
    edge=shutil.which('microsoft-edge') or shutil.which('microsoft-edge-stable')
    if not edge:raise ValueError('Microsoft Edge를 찾지 못했어요.')
    subprocess.Popen([edge,'--user-data-dir='+str(profile('cdp')),
                      '--remote-debugging-address=127.0.0.1','--remote-debugging-port='+str(CDP_PORT),
                      '--new-window',HOME],stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL,start_new_session=True)
    receipt=STATE/'cdp-launch.json'
    for _ in range(40):
        try:
            pid=int(os.readlink(profile('cdp')/'SingletonLock').rsplit('-',1)[1])
            value={'pid':pid,'start':process_start(pid),'profile':str(profile('cdp')),'port':CDP_PORT}
            fd,name=tempfile.mkstemp(prefix='cdp-launch-',dir=STATE);tmp=Path(name)
            with os.fdopen(fd,'w') as out:json.dump(value,out)
            os.replace(tmp,receipt)
            cdp_identity();return
        except (OSError,ValueError,KeyError):time.sleep(.25)
    raise ValueError('디버깅 브라우저의 시작이나 로컬 연결을 확인하지 못했어요. 기존 브라우저는 변경하지 않았어요.')


def start(mode):
    prepare()
    if not RUNTIME.is_file():raise ValueError('리오 브라우저 실행 환경을 찾지 못했어요.')
    if mode=='cdp':cdp_browser()
    unit='rio-browser-'+mode
    active=subprocess.run(['systemctl','--user','is-active',unit+'.service'],capture_output=True,text=True).stdout.strip()=='active'
    if not active:
        if endpoint(mode).exists():raise ValueError('이 방식의 이전 소켓이 남아 있어 자동으로 덮어쓰지 않았어요.')
        if mode=='managed' and os.path.lexists(profile(mode)/'SingletonLock'):
            raise ValueError('전용 자동화 프로필이 이미 열려 있어요. 그 창만 닫은 뒤 실행해 주세요.')
        command=['systemd-run','--user','--unit='+unit,'--collect','--property=UMask=0077',
                 '--property=WorkingDirectory='+str(PROJECT),
                 '--setenv=PYTHONPATH=services/rio-browser-research','--setenv=RIO_BROWSER_TRANSPORT='+mode]
        runtime='/run/user/'+str(os.getuid())
        session={'DISPLAY':os.getenv('DISPLAY',':0'),'XDG_RUNTIME_DIR':runtime,
                 'DBUS_SESSION_BUS_ADDRESS':os.getenv('DBUS_SESSION_BUS_ADDRESS','unix:path='+runtime+'/bus'),
                 'LANG':os.getenv('LANG','C.UTF-8')}
        if os.getenv('WAYLAND_DISPLAY'):session['WAYLAND_DISPLAY']=os.environ['WAYLAND_DISPLAY']
        elif Path(runtime+'/wayland-0').exists():session['WAYLAND_DISPLAY']='wayland-0'
        command.extend('--setenv='+k+'='+v for k,v in session.items())
        command.extend([str(RUNTIME),'-m','rio_browser_research.service'])
        subprocess.run(command,check=True,capture_output=True,text=True)
    from core.research_browser import request
    previous=os.environ.get('RIO_BROWSER_TRANSPORT');os.environ['RIO_BROWSER_TRANSPORT']=mode
    try:
        for _ in range(30):
            status=request('status')
            if status.get('ok') and status.get('profile')==profile_label(mode):
                write_selection(mode)
                return {'selected':mode,'status':status['status'],'profile':status['profile']}
            time.sleep(.5)
    finally:
        if previous is None:os.environ.pop('RIO_BROWSER_TRANSPORT',None)
        else:os.environ['RIO_BROWSER_TRANSPORT']=previous
    raise ValueError('새 연결의 준비를 확인하지 못해 기존 선택을 유지했어요. 전용 창의 검색 화면을 확인해 주세요.')


def main():
    p=argparse.ArgumentParser(description='리오 전용 브라우저 직접 연결')
    p.add_argument('mode',choices=['prepare','managed','cdp','extension','status'])
    args=p.parse_args();os.umask(0o077)
    try:
        if args.mode=='prepare':answer=prepare()
        elif args.mode=='status':answer={'selected':selected_mode(),'prepared_profiles':{m:profile(m).is_dir() for m in MODES}}
        elif args.mode=='extension':
            from core.research_browser import request
            previous=os.environ.get('RIO_BROWSER_TRANSPORT');os.environ['RIO_BROWSER_TRANSPORT']='extension'
            try:status=request('status')
            finally:
                if previous is None:os.environ.pop('RIO_BROWSER_TRANSPORT',None)
                else:os.environ['RIO_BROWSER_TRANSPORT']=previous
            if not status.get('ok'):raise ValueError('기존 확장 연결 상태를 확인하지 못했어요.')
            write_selection('extension');answer={'selected':'extension'}
        else:answer=start(args.mode)
        print(json.dumps(answer,ensure_ascii=False))
    except (OSError,ValueError,subprocess.SubprocessError) as error:
        print(json.dumps({'ok':False,'error':str(error) if isinstance(error,ValueError) else type(error).__name__},ensure_ascii=False))
        return 1
    return 0


if __name__=='__main__':raise SystemExit(main())
