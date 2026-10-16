#!/usr/bin/env python3
"""Reviewed root-only Core account migration; never uses Docker for privilege."""
from __future__ import annotations

import argparse
import grp
import io
import json
import os
from pathlib import Path
import pwd
import re
import sqlite3
import stat
import subprocess
import tarfile
import tempfile
import time

PROJECT=Path('/home/example/rio-nas-pc-integration-20261012')
OPERATOR='example'
CORE='rio-core'
ROOT_STATE=Path('/var/lib/rio-core-install')
POLICY=Path('/etc/rio-execution/peers.json')
CORE_UNIT='rio-execution-isolated.service'
BRIDGE_UNIT='rio-execution-bridge.service'
CORE_SOCKET='/run/rio-execution-core/service.sock'
BRIDGE_SOCKET='/run/rio-execution-bridge/service.sock'


def run(args,**kwargs):
    kwargs.setdefault('check',True);kwargs.setdefault('capture_output',True)
    return subprocess.run(args,**kwargs)


def as_operator(args,**kwargs):
    user=pwd.getpwnam(OPERATOR)
    environment=['HOME='+user.pw_dir,'USER='+OPERATOR,'LOGNAME='+OPERATOR,
        'PATH=/usr/bin:/bin','XDG_RUNTIME_DIR=/run/user/'+str(user.pw_uid),
        'DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/'+str(user.pw_uid)+'/bus']
    return run(['/usr/sbin/runuser','-u',OPERATOR,'--','/usr/bin/env','-i',*environment,'LANG=C.UTF-8','TZ=Asia/Seoul',*args],**kwargs)


def user_unit(*args,**kwargs):
    return as_operator(['/usr/bin/systemctl','--user',*args],**kwargs)


def system_unit(*args,**kwargs):return run(['/usr/bin/systemctl',*args],**kwargs)


def root_directory(path,mode=0o755):
    path=Path(path)
    if path.exists():
        info=path.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid!=0 or info.st_mode&0o022:
            raise ValueError('UNSAFE_ROOT_DIRECTORY')
    else:path.mkdir(mode=mode)
    return path


def root_file(path,data,mode=0o644):
    path=Path(path)
    if path.is_symlink():raise ValueError('SYMLINK_INSTALL_TARGET')
    if path.exists() and (path.stat().st_uid!=0 or path.stat().st_mode&0o022):raise ValueError('UNSAFE_INSTALL_TARGET')
    fd,name=tempfile.mkstemp(prefix='.rio-',dir=path.parent)
    try:
        with os.fdopen(fd,'wb') as stream:stream.write(data);stream.flush();os.fsync(stream.fileno())
        os.chmod(name,mode);os.replace(name,path)
    finally:Path(name).unlink(missing_ok=True)


def snapshot_database(source,target,uid,gid):
    source=Path(source);target=Path(target)
    info=source.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_mode&0o077:raise ValueError('PRIVATE_DATABASE_REQUIRED')
    if target.is_symlink():raise ValueError('DATABASE_SYMLINK')
    src=sqlite3.connect('file:'+str(source)+'?mode=ro',uri=True);dst=sqlite3.connect(target)
    try:src.backup(dst)
    finally:dst.close();src.close()
    os.chmod(target,0o600);os.chown(target,uid,gid)
    # SQLite can create WAL sidecars during a privileged offline snapshot.
    # Leave each database usable by its actual account, without following links.
    for database,owner,group in ((source,info.st_uid,info.st_gid),(target,uid,gid)):
        for suffix in ('-wal','-shm'):
            side=Path(str(database)+suffix)
            try:fd=os.open(side,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
            except FileNotFoundError:continue
            try:
                meta=os.fstat(fd)
                if not stat.S_ISREG(meta.st_mode) or meta.st_nlink!=1:raise ValueError('UNSAFE_DATABASE_SIDECAR')
                if meta.st_uid==0:
                    os.fchown(fd,owner,group);os.fchmod(fd,0o600)
            finally:os.close(fd)


def operator_python(code,*,release=PROJECT,data=None):
    return as_operator(['/usr/bin/env','PYTHONPATH='+str(release),'/usr/bin/python3','-c',code],input=data,cwd=release)


def idle_check():
    op=pwd.getpwnam(OPERATOR)
    for path in Path('/proc').iterdir():
        if not path.name.isdigit():continue
        try:
            if path.stat().st_uid!=op.pw_uid:continue
            args=(path/'cmdline').read_bytes().split(b'\0')
            if b'--core' in args and any(a==b'main.py' or a.endswith(b'/main.py') for a in args):
                raise ValueError('CLOSE_CORE_CLIENT_FIRST')
        except (FileNotFoundError,PermissionError):continue
    marker=ROOT_STATE/'current.json'
    isolated=marker.exists() and json.loads(marker.read_text()).get('status')=='ISOLATED_CORE_ACTIVE'
    path=Path('/var/lib/rio-core/core.sqlite3') if isolated else Path(op.pw_dir)/'.local/state/rio/core/core.sqlite3'
    db=sqlite3.connect('file:'+str(path)+'?mode=ro',uri=True)
    try:
        if db.execute("SELECT COUNT(*) FROM tasks WHERE state IN ('QUEUED','RUNNING')").fetchone()[0]:raise ValueError('WAIT_FOR_ACTIVE_TASKS')
        if db.execute("SELECT COUNT(*) FROM calls WHERE state='IN_FLIGHT'").fetchone()[0]:raise ValueError('WAIT_FOR_DISPATCH')
    finally:db.close()


def source_release(commit):
    if not re.fullmatch('[0-9a-f]{40}',commit):raise ValueError('FULL_REVIEWED_COMMIT_REQUIRED')
    head=as_operator(['/usr/bin/git','-C',str(PROJECT),'rev-parse','HEAD']).stdout.decode().strip()
    if head!=commit:raise ValueError('REVIEWED_COMMIT_CHANGED')
    files=as_operator(['/usr/bin/git','-C',str(PROJECT),'ls-tree','-r','--name-only',commit]).stdout.decode().splitlines()
    files=[p for p in files if (p.startswith('core/') and p.endswith('.py')) or ('/' not in p and p.endswith('.py')) or p in ('config/identity.yaml','config/identity.nas.yaml')]
    changed=as_operator(['/usr/bin/git','-C',str(PROJECT),'diff','--name-only',commit,'--',*files]).stdout
    if changed.strip():raise ValueError('REVIEWED_CORE_SOURCE_CHANGED')
    data=as_operator(['/usr/bin/git','-C',str(PROJECT),'archive','--format=tar',commit,'--',*files]).stdout
    if len(data)>32*1024*1024:raise ValueError('SOURCE_LIMIT')
    parent=root_directory('/opt/rio-core');releases=root_directory(parent/'releases')
    target=releases/commit
    if target.exists():raise ValueError('RELEASE_ALREADY_EXISTS')
    target.mkdir(mode=0o755)
    with tarfile.open(fileobj=io.BytesIO(data)) as archive:
        for member in archive:
            parts=Path(member.name).parts
            if member.name.startswith('/') or '..' in parts or not (member.isfile() or member.isdir()):raise ValueError('UNSAFE_ARCHIVE_MEMBER')
            destination=target/member.name
            if member.isdir():destination.mkdir(parents=True,exist_ok=True,mode=0o755)
            else:
                destination.parent.mkdir(parents=True,exist_ok=True,mode=0o755)
                destination.write_bytes(archive.extractfile(member).read());destination.chmod(0o644)
    root_file(target/'RELEASE.json',json.dumps({'commit':commit,'files':len(files)}).encode())
    return target


def units(release,op,core):
    bridge=f'''[Unit]
Description=Rio operator adapters and model bridge (no generic shell/Builder RPC)
After=network.target
[Service]
Type=exec
User={OPERATOR}
Group={op.pw_gid}
WorkingDirectory={release}
Environment=HOME={op.pw_dir}
Environment=PYTHONDONTWRITEBYTECODE=1
Environment=RIO_TOOLS_SOCKET={op.pw_dir}/.local/state/rio/tools-llm-20261014/service.sock
Environment=XDG_RUNTIME_DIR=/run/user/{op.pw_uid}
Environment=DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/{op.pw_uid}/bus
ExecStart=/usr/bin/python3 -m core.execution.bridge
RuntimeDirectory=rio-execution-bridge
RuntimeDirectoryMode=0711
UMask=0077
NoNewPrivileges=yes
CapabilityBoundingSet=
PrivateTmp=yes
ProtectSystem=full
ReadOnlyPaths=/opt/rio-core
Restart=on-failure
RestartSec=5
TimeoutStopSec=10
[Install]
WantedBy=multi-user.target
'''
    isolated=f'''[Unit]
Description=Rio execution Core in a separate account without Docker access
Requires={BRIDGE_UNIT}
After={BRIDGE_UNIT}
[Service]
Type=exec
User={CORE}
Group={core.pw_gid}
WorkingDirectory={release}
Environment=HOME={core.pw_dir}
Environment=PYTHONDONTWRITEBYTECODE=1
Environment=PYTHONNOUSERSITE=1
ExecStart=/usr/bin/python3 -s -m core.execution.isolated
RuntimeDirectory=rio-execution-core
RuntimeDirectoryMode=0711
StateDirectory=rio-core
StateDirectoryMode=0700
UMask=0077
NoNewPrivileges=yes
CapabilityBoundingSet=
PrivateTmp=yes
PrivateDevices=yes
ProtectSystem=strict
ProtectHome=yes
ReadWritePaths={core.pw_dir} /run/rio-execution-core
InaccessiblePaths=-/var/run/docker.sock -/run/docker.sock -/run/containerd -/run/user/{op.pw_uid}
RestrictAddressFamilies=AF_UNIX
RestrictSUIDSGID=yes
LockPersonality=yes
Restart=on-failure
RestartSec=5
TimeoutStopSec=10
[Install]
WantedBy=multi-user.target
'''
    return {BRIDGE_UNIT:bridge,CORE_UNIT:isolated}


SMOKE=r'''
from core.execution.client import CoreClient
from core.execution.types import source_revision
import json,time
c=CoreClient('/run/rio-execution-core/service.sock')
end=time.monotonic()+25
while True:
 try:h=c.request('health');assert h['ready'];break
 except (OSError,ValueError):
  if time.monotonic()>end:raise
  time.sleep(.2)
assert not h['docker_socket_access'] and h['isolation_audit']['docker_connect']!='ALLOWED'
assert h['source_revision']==source_revision()
s=c.request('session.open')['session']
out={'health':h,'tasks':[]}
for request in ('/core status','/tools call srt_inspect '+json.dumps({'text':'1\n00:00:00,000 --> 00:00:01,000\nHello Rio\n'})):
 t=c.request('task.submit',session=s,request=request);deadline=time.monotonic()+35
 while True:
  value=c.request('task.status',session=s,id=t['id'])
  if value['state'] not in ('QUEUED','RUNNING'):break
  if time.monotonic()>deadline:raise RuntimeError('SMOKE_TASK_TIMEOUT')
  time.sleep(.2)
 assert value['state']=='COMPLETED',value['state']
 out['tasks'].append({'id':t['id'],'state':value['state']})
events=c.request('task.events',session=s,after=0)
for e in events:
 if e['kind']=='TOOL_RESULT' and e['payload']['name']=='registered.srt_inspect':
  r=e['payload']['result'];assert r['confirmation']=='VALIDATED_OUTPUT'
  assert r['evidence']['package_hash']=='dc42b1830aec8838a7e111e7219c1b223b835a14d4ff728a3541cdce0d0a8e15'
  assert r['evidence']['output']['image_id']=='sha256:55054f75a10b9d3cb7ab56a4fd38ddcbb3f72361eaea6414a8fe33db8dabdc08'
  out['srt']=r
assert 'srt' in out
print(json.dumps(out))
'''


def save_operator_report(value):
    code="""from pathlib import Path
import os,sys
os.umask(0o077)
p=Path.home()/'.local/state/rio/core-account-migration'
p.mkdir(parents=True,exist_ok=True,mode=0o700)
(p/'result.json').write_bytes(sys.stdin.buffer.read())
"""
    operator_python(code,data=json.dumps(value,ensure_ascii=False,indent=2).encode())


def select_isolated(enabled):
    code="""from pathlib import Path
import json,os,tempfile,sys
p=Path.home()/'.config/rio-execution/selection.json'
v=json.loads(sys.stdin.buffer.read());fd,name=tempfile.mkstemp(prefix='.selection-',dir=p.parent)
with os.fdopen(fd,'w') as f:json.dump(v,f);f.flush();os.fsync(f.fileno())
os.replace(name,p)
"""
    operator_python(code,data=json.dumps({'version':1,'enabled':True,'transport':'isolated' if enabled else 'user'}).encode())


def rollback(marker,*,live=None):
    op=pwd.getpwnam(OPERATOR);backup=Path(marker['backup']);release=Path(marker['release'])
    # Preserve the newest Core history; never restore an old pre-effect database.
    for name in (CORE_UNIT,BRIDGE_UNIT):
        system_unit('disable','--now',name,check=False)
    live=Path(live or '/var/lib/rio-core/core.sqlite3')
    if live.exists():
        exported=backup/'rollback-latest.sqlite3';snapshot_database(live,exported,0,0)
        code="""from pathlib import Path
import os,sys,sqlite3,tempfile
root=Path.home()/'.local/state/rio/core';fd,name=tempfile.mkstemp(prefix='.migration-',dir=root)
with os.fdopen(fd,'wb') as f:f.write(sys.stdin.buffer.read())
a=sqlite3.connect(name);b=sqlite3.connect(root/'core.sqlite3')
a.backup(b);b.close();a.close();os.unlink(name)
"""
        operator_python(code,release=release,data=exported.read_bytes())
    old=json.loads((backup/'selection-before.json').read_text())
    operator_python("from pathlib import Path;import sys;(Path.home()/'.config/rio-execution/selection.json').write_bytes(sys.stdin.buffer.read())",data=json.dumps(old).encode())
    if marker['user_core_enabled']:user_unit('enable','rio-execution-core.service')
    user_unit('start','rio-execution-core.service')
    marker['status']='ROLLED_BACK_HISTORY_PRESERVED';root_file(ROOT_STATE/'current.json',json.dumps(marker).encode(),0o600)
    save_operator_report(marker)


def install(commit):
    if (ROOT_STATE/'current.json').exists():raise ValueError('MIGRATION_ALREADY_RECORDED')
    op=pwd.getpwnam(OPERATOR)
    if op.pw_uid==0:raise ValueError('NONROOT_OPERATOR_REQUIRED')
    for binary in ('/usr/bin/setfacl','/usr/sbin/runuser','/usr/bin/systemctl'):
        if not Path(binary).is_file():raise ValueError('MISSING_SYSTEM_PREREQUISITE')
    if POLICY.exists():raise ValueError('EXISTING_PEER_POLICY_REQUIRES_REVIEW')
    idle_check()
    context=json.loads(operator_python('from core.execution.bridge import operator_context;import json;print(json.dumps(operator_context()))').stdout)
    release=source_release(commit)
    try:core=pwd.getpwnam(CORE)
    except KeyError:
        run(['/usr/sbin/useradd','--system','--user-group','--home-dir','/var/lib/rio-core','--shell','/usr/sbin/nologin',CORE]);core=pwd.getpwnam(CORE)
    if core.pw_uid==0 or core.pw_dir!='/var/lib/rio-core' or core.pw_shell not in ('/usr/sbin/nologin','/sbin/nologin'):
        raise ValueError('UNEXPECTED_CORE_ACCOUNT')
    if grp.getgrnam('docker').gr_gid in os.getgrouplist(CORE,core.pw_gid):raise ValueError('CORE_DOCKER_GROUP_FORBIDDEN')
    if core.pw_gid!=grp.getgrnam(CORE).gr_gid or set(os.getgrouplist(CORE,core.pw_gid))!={core.pw_gid} or core.pw_gid in {0,op.pw_gid}:
        raise ValueError('CORE_GROUPS_MUST_BE_SEPARATE')
    home=Path(core.pw_dir)
    if home.exists() and any(home.iterdir()):raise ValueError('CORE_STATE_ALREADY_EXISTS')
    home.mkdir(mode=0o700,exist_ok=True);os.chown(home,core.pw_uid,core.pw_gid);home.chmod(0o700)
    inspected=run(['/usr/sbin/runuser','-u',CORE,'--','/usr/bin/env','-i','HOME='+core.pw_dir,'USER='+CORE,
                   'PATH=/usr/bin:/bin','PYTHONDONTWRITEBYTECODE=1','/usr/bin/python3','-s','-m','core.execution.isolation_audit'],cwd=release)
    account_audit=json.loads(inspected.stdout)
    if account_audit['docker_file_access'] or account_audit['docker_connect']!='DENIED':raise ValueError('ACCOUNT_DOCKER_DENIAL_NOT_PROVEN')
    root_directory(ROOT_STATE,0o700);backups=root_directory('/var/backups/rio-core',0o700)
    backup=backups/str(time.time_ns());backup.mkdir(mode=0o700)
    selection=Path(op.pw_dir)/'.config/rio-execution/selection.json'
    root_file(backup/'selection-before.json',selection.read_bytes(),0o600)
    enabled=user_unit('is-enabled','rio-execution-core.service',check=False).stdout.decode().strip()=='enabled'
    marker={'status':'INSTALLING','backup':str(backup),'release':str(release),'commit':commit,'user_core_enabled':enabled,'core_uid':core.pw_uid,'operator_uid':op.pw_uid,'account_audit':account_audit,'operator_context_before':context}
    root_file(ROOT_STATE/'current.json',json.dumps(marker).encode(),0o600)
    stopped=False
    try:
        # Re-check after preparation. No active terminal is forcibly detached.
        idle_check();user_unit('stop','rio-execution-core.service');stopped=True
        source=Path(op.pw_dir)/'.local/state/rio/core/core.sqlite3'
        snapshot_database(source,backup/'before.sqlite3',0,0)
        snapshot_database(source,home/'core.sqlite3',core.pw_uid,core.pw_gid)
        root_directory(POLICY.parent)
        policy={'version':1,'core_uid':core.pw_uid,'operator_uid':op.pw_uid,'core_socket':CORE_SOCKET,'bridge_socket':BRIDGE_SOCKET,'operator_project':str(PROJECT),'release':str(release)}
        root_file(POLICY,json.dumps(policy,indent=2).encode())
        for name,text in units(release,op,core).items():
            path=Path('/etc/systemd/system')/name
            if path.exists():raise ValueError('EXISTING_SYSTEM_UNIT_REQUIRES_REVIEW')
            root_file(path,text.encode())
        system_unit('daemon-reload');system_unit('start',BRIDGE_UNIT,CORE_UNIT)
        result=json.loads(operator_python(SMOKE,release=release).stdout)
        assert result['health']['process_uid']==core.pw_uid
        assert result['health']['isolation_audit']['operator_context']==context
        marker['smoke']=result
        user_unit('disable','rio-execution-core.service')
        system_unit('enable',BRIDGE_UNIT,CORE_UNIT)
        select_isolated(True)
        marker['status']='ISOLATED_CORE_ACTIVE'
        root_file(ROOT_STATE/'current.json',json.dumps(marker).encode(),0o600);save_operator_report(marker)
        print(json.dumps({'status':marker['status'],'core_uid':core.pw_uid,'docker_connect':account_audit['docker_connect'],'srt':'VALIDATED_OUTPUT','report':str(Path(op.pw_dir)/'.local/state/rio/core-account-migration/result.json')}))
    except BaseException:
        if stopped:rollback(marker)
        raise


def main():
    p=argparse.ArgumentParser(description='Core account migration; no PC or device commands')
    mode=p.add_mutually_exclusive_group(required=True);mode.add_argument('--install',action='store_true');mode.add_argument('--rollback',action='store_true')
    p.add_argument('--commit')
    a=p.parse_args()
    if os.getuid()!=0:raise SystemExit('관리자 인증이 필요합니다. 비밀번호는 채팅에 보내지 마세요.')
    if a.install:
        if not a.commit:raise ValueError('REVIEWED_COMMIT_REQUIRED')
        install(a.commit)
    else:
        idle_check();rollback(json.loads((ROOT_STATE/'current.json').read_text()))


if __name__=='__main__':main()
