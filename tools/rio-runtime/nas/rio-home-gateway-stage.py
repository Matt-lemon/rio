#!/usr/bin/env python3
"""Prepare a reviewed gateway source and unit proposal. Never install/restart.

All output goes under private release/state directories. Credentials, policy,
jobs, installed source and actual systemd configuration remain untouched.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess


SOURCE = 'integrations/google_home/nas/gateway.py'


def run(args, **kwargs):
    return subprocess.run(args, capture_output=True, check=True, **kwargs)


def private_directory(path):
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    info=path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid!=os.getuid() or info.st_mode & 0o077:
        raise ValueError('PRIVATE_STAGING_DIRECTORY_REQUIRED')
    return path


def write_new(path, data, mode=0o600):
    fd=os.open(path, os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW, mode)
    with os.fdopen(fd, 'wb') as stream:
        stream.write(data);stream.flush();os.fsync(stream.fileno())


def prepare(commit):
    if not re.fullmatch(r'[0-9a-f]{40}', commit):
        raise ValueError('FULL_REVIEWED_COMMIT_REQUIRED')
    repo=Path(__file__).resolve().parents[3]
    head=run(['git','-C',str(repo),'rev-parse','HEAD'], text=True).stdout.strip()
    if head!=commit or run(['git','-C',str(repo),'status','--porcelain'], text=True).stdout:
        raise ValueError('REVIEWED_CHECKOUT_CHANGED')
    home=Path.home()
    if not re.fullmatch(r'/[A-Za-z0-9_./-]+', str(home)):
        raise ValueError('UNSUPPORTED_SYSTEMD_HOME_PATH')
    data=run(['git','-C',str(repo),'show',commit+':'+SOURCE]).stdout
    compile(data, SOURCE, 'exec')  # Syntax only; never imports the gateway.
    hashed=hashlib.sha256(data).hexdigest()
    installed=home/'google-home-bridge/nas-service/gateway.py'
    if not installed.is_file() or installed.is_symlink():
        raise ValueError('INSTALLED_GATEWAY_SOURCE_UNAVAILABLE')
    interpreter=home/'google-home-bridge/nas-service/.venv/bin/python'
    version=run([str(interpreter),'-c',
        "import importlib.metadata; print(importlib.metadata.version('websockets'))"], text=True).stdout.strip()
    if version!='15.0.1':
        raise ValueError('GATEWAY_DEPENDENCY_REVIEW_REQUIRED')
    existing_dropin=home/'.config/systemd/user/rio-home-gateway.service.d/60-reviewed-gateway.conf'
    if existing_dropin.exists() or existing_dropin.is_symlink():
        raise ValueError('REVIEWED_DROPIN_ALREADY_EXISTS')
    release_parent=private_directory(home/'.local/share/rio-home-gateway/releases')
    release=release_parent/commit
    release.mkdir(mode=0o700, exist_ok=False)
    source=release/'gateway.py'
    write_new(source,data,0o400)
    state_parent=private_directory(home/'.local/state/rio/gateway-prepared')
    state=state_parent/commit
    state.mkdir(mode=0o700, exist_ok=False)
    proposal=('[Service]\nExecStart=\nExecStart='+str(interpreter)+' '+str(source)+'\n')
    write_new(state/'60-reviewed-gateway.conf',proposal.encode())
    original=home/'.config/systemd/user/rio-home-gateway.service'
    combined=state/'rio-home-gateway.service'
    write_new(combined,original.read_bytes()+b'\n'+proposal.encode())
    check=run(['systemd-analyze','--user','--man=no','verify',str(combined)])
    # Retain checker output privately. It contains no credential arguments.
    write_new(state/'unit-verify.txt',check.stdout+check.stderr)
    receipt={'commit':commit,'gateway_sha256':hashed,'staged_source':str(source),
        'installed_source_sha256':hashlib.sha256(installed.read_bytes()).hexdigest(),
        'websockets_version':version,'proposed_dropin':str(state/'60-reviewed-gateway.conf'),
        'destination_dropin':str(existing_dropin),'unit_syntax_verified':True,
        'restart_requires_explicit_approval':True,'systemd_changed':False,'service_restarted':False,
        'policy_changed':False,'installed_source_changed':False,
        'state_directory':str(home/'google-home-bridge/state/gateway')}
    write_new(state/'receipt.json',(json.dumps(receipt,indent=2)+'\n').encode())
    return {'prepared':True,'receipt':str(state/'receipt.json'),'service_restarted':False}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--commit',required=True)
    args=parser.parse_args()
    os.umask(0o077)
    try:
        print(json.dumps(prepare(args.commit)))
    except (ValueError, OSError, subprocess.CalledProcessError) as error:
        # No exception dump with possibly private command output.
        code=str(error) if isinstance(error,ValueError) else type(error).__name__
        print(json.dumps({'prepared':False,'code':code,'service_restarted':False}))
        raise SystemExit(1)


if __name__=='__main__': main()
