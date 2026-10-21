"""The only component that may construct fixed Docker commands."""
import fcntl
import json
import os
from pathlib import Path
import re
import selectors
import subprocess
import time
import uuid
from core.tools.contracts import ToolError, canonical

DOCKER='/usr/bin/docker'
SOCKET='unix:///var/run/docker.sock'
OUTPUT_LIMIT=1024*1024


class DockerExecutor:
    def __init__(self, root, image_id):
        if not re.fullmatch(r'sha256:[0-9a-f]{64}',image_id):
            raise ToolError('IMAGE_NOT_PINNED')
        self.root=Path(root)
        self.image_id=image_id
        self.root.mkdir(mode=0o700,parents=True,exist_ok=True)
        if self.root.is_symlink() or self.root.stat().st_mode & 0o077:
            raise ToolError('EXECUTOR_STATE_PERMISSION')

    def command(self,name,lane):
        if lane not in {'runtime','builder'} or not re.fullmatch(r'rio-tools-[a-f0-9]{32}',name):
            raise ToolError('EXECUTION_PROFILE_FORBIDDEN')
        return [DOCKER,'--host',SOCKET,'run','--pull=never','--name',name,
                '--label','rio.tools.managed=true','--label','rio.tools.lane='+lane,
                '--network=none','--ipc=none','--read-only','--cap-drop=ALL',
                '--security-opt=no-new-privileges','--user=65532:65532',
                '--cpus=1','--memory=512m','--memory-swap=512m','--pids-limit=32',
                '--ulimit','nofile=64:64','--ulimit','fsize=67108864:67108864',
                '--tmpfs','/work:rw,nosuid,nodev,noexec,size=67108864,uid=65532,gid=65532,mode=0700',
                '--log-driver=none','--workdir=/work','-i',self.image_id]

    @staticmethod
    def cleanup(name):
        subprocess.run([DOCKER,'--host',SOCKET,'rm','-f',name],stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL,timeout=10,env={'PATH':'/usr/bin:/bin'})

    def execute(self,request,*,lane='runtime',cancel=None):
        data=canonical(request)
        if len(data)>2*1024*1024:
            raise ToolError('INPUT_LIMIT')
        name='rio-tools-'+uuid.uuid4().hex
        timeout=60 if lane=='builder' else 30
        started=time.monotonic()
        lock_path=self.root/'execution.lock'
        if lock_path.is_symlink():
            raise ToolError('STATE_SYMLINK')
        try:fd=os.open(lock_path,os.O_WRONLY|os.O_CREAT|os.O_NOFOLLOW,0o600)
        except OSError:raise ToolError('STATE_LOCK_UNAVAILABLE') from None
        with os.fdopen(fd,'a') as lock:
            while True:
                try:
                    fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB);break
                except BlockingIOError:
                    if cancel is not None and cancel.is_set():raise ToolError('CANCELLED')
                    if time.monotonic()-started>60:raise ToolError('QUEUE_TIME_LIMIT')
                    time.sleep(.1)
            started=time.monotonic()
            process=None
            out=bytearray(); error=bytearray(); selector=selectors.DefaultSelector()
            try:
                process=subprocess.Popen(self.command(name,lane),stdin=subprocess.PIPE,
                                         stdout=subprocess.PIPE,stderr=subprocess.PIPE,
                                         env={'PATH':'/usr/bin:/bin'})
                for stream,events,tag in [(process.stdin,selectors.EVENT_WRITE,'input'),
                                          (process.stdout,selectors.EVENT_READ,'output'),
                                          (process.stderr,selectors.EVENT_READ,'error')]:
                    os.set_blocking(stream.fileno(),False)
                    selector.register(stream,events,tag)
                offset=0
                while selector.get_map():
                    if time.monotonic()-started>timeout:
                        raise ToolError('TIME_LIMIT')
                    if cancel is not None and cancel.is_set():
                        raise ToolError('CANCELLED')
                    for key,_ in selector.select(.1):
                        stream=key.fileobj
                        if key.data=='input':
                            try:
                                offset+=os.write(stream.fileno(),data[offset:offset+65536])
                            except BrokenPipeError:
                                offset=len(data)
                            if offset==len(data):
                                selector.unregister(stream);stream.close()
                        else:
                            chunk=os.read(stream.fileno(),65536)
                            if not chunk:
                                selector.unregister(stream);stream.close();continue
                            target=out if key.data=='output' else error
                            if len(target)+len(chunk)>OUTPUT_LIMIT:
                                raise ToolError('OUTPUT_LIMIT')
                            target.extend(chunk)
                process.wait(timeout=2)
                try:
                    response=json.loads(out)
                except (ValueError,TypeError):
                    raise ToolError('INVALID_CONTAINER_RESULT') from None
                if process.returncode or not isinstance(response,dict) or response.get('ok') is not True:
                    raise ToolError('CONTAINER_TOOL_FAILED')
                return {'result':response['result'],'elapsed_seconds':round(time.monotonic()-started,3),
                        'image_id':self.image_id,'lane':lane,'usage':response.get('usage')}
            finally:
                selector.close()
                try:
                    self.cleanup(name)
                except (OSError,subprocess.TimeoutExpired):
                    # Preserve a failed cleanup as a failure, never as successful work.
                    raise ToolError('CONTAINER_CLEANUP_FAILED') from None
                finally:
                    if process is not None:
                        try:
                            if process.poll() is None:process.kill()
                            process.wait(timeout=5)
                        finally:
                            for stream in (process.stdin,process.stdout,process.stderr):
                                if stream is not None and not stream.closed:stream.close()

    def run_package(self,package,args,*,lane='runtime',cancel=None):
        c=package['contract']
        if c['image_id']!=self.image_id:
            raise ToolError('IMAGE_APPROVAL_MISMATCH')
        return self.execute({'mode':'package','kind':c['kind'],
                             'source':package['files'][c['entrypoint']],'input':args},
                            lane=lane,cancel=cancel)

    def pipeline(self,steps,text,*,cancel=None):
        allowed={'sort':{'-n','-r','-u'},'uniq':{'-c','-d','-u'},'wc':{'-l','-w','-c'}}
        if not isinstance(steps,list) or not 1<=len(steps)<=4 or not isinstance(text,str):
            raise ToolError('PIPELINE_NOT_ALLOWED')
        for step in steps:
            if not isinstance(step,dict) or set(step)!={'command','args'} or not isinstance(step['command'],str) or step['command'] not in allowed:
                raise ToolError('COMMAND_NOT_ALLOWED')
            args=step['args']
            if not isinstance(args,list) or len(args)>3 or any(not isinstance(a,str) or a not in allowed[step['command']] for a in args):
                raise ToolError('ARGUMENT_NOT_ALLOWED')
        return self.execute({'mode':'pipeline','steps':steps,'text':text},cancel=cancel)
