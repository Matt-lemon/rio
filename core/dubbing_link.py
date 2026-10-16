"""Supervise only the fixed NAS->PC SSH link; never wake/start a PC service."""
from __future__ import annotations

import argparse
import errno
import fcntl
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import tempfile
import time

PEER = '192.0.2.129'
PORT = 18770
STATE = Path.home()/'.local/state/rio/dubbing-link'


def classify_ssh(code, stderr):
    if code == 0:
        return 'REMOTE_CLOSED'
    if 'Permission denied' in stderr:
        return 'SSH_AUTH_REJECTED'
    if 'Host key verification failed' in stderr or 'REMOTE HOST IDENTIFICATION HAS CHANGED' in stderr:
        return 'SSH_HOST_KEY_REJECTED'
    if 'Address already in use' in stderr or 'cannot listen to port' in stderr:
        return 'LOCAL_PORT_OCCUPIED'
    if 'No route to host' in stderr:
        return 'PEER_NO_ROUTE'
    if 'Connection timed out' in stderr:
        return 'PEER_TIMEOUT'
    if 'Connection refused' in stderr:
        return 'SSH_NOT_LISTENING'
    return 'SSH_LINK_LOST'


def private_root(root):
    root=Path(root)
    if any(p.is_symlink() for p in [root,*root.parents]):
        raise ValueError('STATE_SYMLINK')
    root.mkdir(parents=True,exist_ok=True,mode=0o700)
    st=root.stat()
    if st.st_uid!=os.getuid() or st.st_mode & 0o077:
        raise ValueError('PRIVATE_LINK_STATE_REQUIRED')
    return root


def save(root,value):
    fd,path=tempfile.mkstemp(prefix='.status-',dir=root)
    try:
        with os.fdopen(fd,'w') as f:
            json.dump(value,f,ensure_ascii=False);f.flush();os.fsync(f.fileno())
        os.replace(path,root/'status.json')
    finally:
        Path(path).unlink(missing_ok=True)


class Backend:
    def __init__(self,root):
        self.root=root
        self.stderr=None

    def peer(self):
        try:
            with socket.create_connection((PEER,22),timeout=3):pass
            return True,'TCP_REACHABLE'
        except OSError as e:
            code='PEER_NO_ROUTE' if e.errno in {errno.EHOSTUNREACH,errno.ENETUNREACH} else 'PEER_TIMEOUT' if isinstance(e,TimeoutError) else 'SSH_NOT_LISTENING' if e.errno==errno.ECONNREFUSED else 'PEER_UNREACHABLE'
            return False,code

    def occupied(self):
        try:
            with socket.create_connection(('127.0.0.1',PORT),timeout=.3):pass
            return True
        except OSError:return False

    def health(self):
        from rio_dubbing_client import DubbingClient
        try:
            client=DubbingClient()
            if client.base_url!='http://127.0.0.1:18770':
                return 'CONFIGURATION_ERROR','UNEXPECTED_API_ENDPOINT'
            raw=client._request('GET','/health',accept_unhealthy=True,timeout=3)
            if not isinstance(raw,dict) or raw.get('service')!='rio-dubbing':
                return 'API_UNVERIFIED','UNEXPECTED_API_SERVICE'
            return ('READY','API_READY') if raw.get('ok') is True else ('API_UNHEALTHY','API_WORKER_NOT_READY')
        except Exception:return 'WAITING_FOR_API','API_UNAVAILABLE'

    def start(self):
        # Fixed endpoint/key/forward, strict host verification, no remote command.
        self.stderr=tempfile.TemporaryFile(dir=self.root)
        return subprocess.Popen(['/usr/bin/ssh','-F','/dev/null','-N','-T','-i',str(Path.home()/'.ssh/id_ed25519_rio_to_windows'),
            '-o','IdentitiesOnly=yes','-o','BatchMode=yes','-o','StrictHostKeyChecking=yes',
            '-o','ExitOnForwardFailure=yes','-o','ConnectTimeout=10','-o','ServerAliveInterval=15','-o','ServerAliveCountMax=3',
            '-L','127.0.0.1:18770:127.0.0.1:8770','operator@'+PEER],stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=self.stderr)

    def diagnostic(self,code):
        self.stderr.seek(0,os.SEEK_END);size=self.stderr.tell();self.stderr.seek(max(0,size-16384))
        raw=self.stderr.read().decode('utf-8',errors='replace');self.stderr.close();self.stderr=None
        return classify_ssh(code,raw)


class Supervisor:
    def __init__(self,backend,clock=time.monotonic):
        self.backend=backend;self.clock=clock;self.child=None
        self.next_check=0;self.failures=0;self.fatal=False
        self.snapshot={'state':'STARTING','reason':'NOT_OBSERVED','peer_power':'NOT_OBSERVED','automatic_pc_start':False,'automatic_api_start':False}

    def publish(self,state,reason,delay):
        self.next_check=self.clock()+delay
        self.snapshot={**self.snapshot,'state':state,'reason':reason,'checked_at':time.time(),
                       'retry_after_seconds':delay,'ssh_pid':self.child.pid if self.child else None}
        return True

    def tick(self):
        if self.fatal or self.clock()<self.next_check:return False
        if self.child is not None:
            code=self.child.poll()
            if code is None:
                if not self.backend.occupied():return self.publish('CONNECTING','SSH_CONNECTING',1)
                state,reason=self.backend.health()
                if state=='READY':self.failures=0
                if state=='CONFIGURATION_ERROR':
                    self.fatal=True;return self.publish(state,reason,0)
                return self.publish(state,reason,15 if state=='READY' else 30)
            reason=self.backend.diagnostic(code);self.child=None
            if reason in {'SSH_AUTH_REJECTED','SSH_HOST_KEY_REJECTED','LOCAL_PORT_OCCUPIED'}:
                self.fatal=True;return self.publish('CONFIGURATION_ERROR',reason,0)
            self.failures+=1
            return self.publish('WAITING_FOR_PEER',reason,min(300,60*2**min(self.failures-1,3)))
        available,reason=self.backend.peer()
        if not available:
            self.failures+=1
            return self.publish('WAITING_FOR_PEER',reason,min(300,60*2**min(self.failures-1,3)))
        if self.backend.occupied():
            self.fatal=True;return self.publish('CONFIGURATION_ERROR','LOCAL_PORT_OCCUPIED',0)
        self.child=self.backend.start()
        return self.publish('CONNECTING','SSH_STARTING',1)

    def close(self):
        if self.child is not None and self.child.poll() is None:
            self.child.terminate()
            try:self.child.wait(timeout=3)
            except subprocess.TimeoutExpired:self.child.kill();self.child.wait(timeout=3)


def read_status(root=STATE):
    path=Path(root)/'status.json'
    info=path.lstat()
    if path.is_symlink() or info.st_uid!=os.getuid() or info.st_mode & 0o077 or info.st_size>8192:
        raise ValueError('PRIVATE_LINK_STATUS_REQUIRED')
    value=json.loads(path.read_text())
    if not isinstance(value,dict) or type(value.get('checked_at')) not in {int,float}:
        raise ValueError('INVALID_LINK_STATUS')
    return {**value,'stale':not 0<=time.time()-value['checked_at']<=370}


def probe(backend):
    peer,reason=backend.peer()
    if not peer:return {'state':'WAITING_FOR_PEER','reason':reason,'peer_power':'NOT_OBSERVED','mutations':0}
    if not backend.occupied():return {'state':'WAITING_FOR_LINK','reason':'NO_LOCAL_FORWARD','peer_power':'NOT_OBSERVED','mutations':0}
    state,reason=backend.health()
    return {'state':state,'reason':reason,'peer_power':'NOT_OBSERVED','mutations':0}


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--probe',action='store_true');parser.add_argument('--status',action='store_true')
    args=parser.parse_args();os.umask(0o077);root=private_root(STATE)
    if args.status:
        print(json.dumps(read_status(root),ensure_ascii=False));return 0
    backend=Backend(root)
    if args.probe:print(json.dumps(probe(backend),ensure_ascii=False));return 0
    fd=os.open(root/'supervisor.lock',os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW,0o600)
    fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
    controller=Supervisor(backend);stop=False
    def finish(sig,frame):
        nonlocal stop
        stop=True
    for sig in (signal.SIGTERM,signal.SIGINT):signal.signal(sig,finish)
    last=None
    try:
        while not stop:
            if controller.tick():
                save(root,controller.snapshot)
                key=(controller.snapshot['state'],controller.snapshot['reason'])
                if key!=last:print(json.dumps({'state':key[0],'reason':key[1]},ensure_ascii=False),flush=True);last=key
            if controller.fatal:return 78
            time.sleep(.2)
    finally:
        controller.close()
        if not controller.fatal:
            save(root,{**controller.snapshot,'state':'STOPPED','reason':'SUPERVISOR_STOPPED','checked_at':time.time(),'ssh_pid':None})
        os.close(fd)
    return 0


if __name__=='__main__':raise SystemExit(main())
