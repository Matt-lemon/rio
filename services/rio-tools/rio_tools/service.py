from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
import re
import signal
import socket
import stat
import socketserver
import threading
import time
from core.tools.contracts import LOW_RISK, ToolError, canonical, digest, validate_value
from .executor import DockerExecutor
from .registry import Registry
from .srt import package as srt_package
from .validator import Validator
from .builder import Builder, SharedModel
from .adapters import AdapterCatalog
from .builtins import pipeline


class ToolService:
    def __init__(self,registry,executor,model=None):
        self.registry=registry;self.executor=executor
        self.adapters=AdapterCatalog(registry)
        self.pipeline=pipeline(executor.image_id)
        self.running={};self.threads={};self.running_lock=threading.RLock();self.stopping=False
        self.pending=threading.BoundedSemaphore(16)
        self.builder=Builder(registry,executor,model)
        # A prior process may have dispatched a change. Never replay it automatically.
        with registry.transaction():
            registry.db.execute("UPDATE jobs SET status='UNKNOWN',updated=? WHERE status IN ('RUNNING','QUEUED')",(time.time(),))

    def build_srt(self):
        try:
            prior=self.registry.describe('srt_inspect')
            return {'status':'REUSED','package_hash':prior['hash'],'version':prior['version']}
        except ToolError as e:
            if e.code!='TOOL_NOT_REGISTERED':raise
        key=self.registry.candidate(srt_package(self.executor.image_id))
        report=Validator(self.registry,self.executor).validate(key)
        if not all(report[k] for k in ('static_pass','fixed_tests_pass','isolation_pass')):
            return {'status':'QUARANTINED','package_hash':key,'report':report}
        self.registry.activate(key)
        return {'status':'ACTIVE','package_hash':key,'provenance':'reviewed-template-v1','report':report}

    def submit(self,name,args,request_id,expected_hash=None):
        if self.stopping:raise ToolError('SERVICE_STOPPING')
        if not isinstance(request_id,str) or not re.fullmatch(r'[A-Za-z0-9_-]{8,100}',request_id):
            raise ToolError('INVALID_REQUEST_ID')
        request_hash=digest({'name':name,'args':args})
        # Duplicate requests return the original outcome, even after tool version changes.
        with self.registry.lock:
            old=self.registry.db.execute('SELECT * FROM jobs WHERE id=?',(request_id,)).fetchone()
        if old:
            if old['request_hash']!=request_hash:raise ToolError('REQUEST_ID_CONFLICT')
            if expected_hash is not None and old['package_hash']!=expected_hash:raise ToolError('REQUEST_VERSION_CONFLICT')
            return self.job(request_id)
        try:item=self.pipeline if name=='pipeline_run' else self.registry.describe(name)
        except ToolError as e:
            if e.code!='TOOL_NOT_REGISTERED':raise
            item=self.adapters.describe(name)
        c=item['contract'];validate_value(c['input_schema'],args)
        if expected_hash is not None and item['hash']!=expected_hash:
            raise ToolError('TOOL_VERSION_CHANGED')
        if c['kind']!='adapter' and not set(c['permissions'])<=LOW_RISK:
            raise ToolError('GENERATED_PROFILE_PERMISSION_UNSUPPORTED')
        if c['kind'] not in {'adapter','pipeline'}:self.registry.load_package(item['hash'])
        if not self.pending.acquire(blocking=False):raise ToolError('RUNTIME_QUEUE_FULL')
        with self.registry.transaction():
            try:
                self.registry.db.execute('INSERT INTO jobs VALUES(?,?,?,?,?,?,?,?,?)',
                    (request_id,c['kind'] if c['kind'] in {'adapter','pipeline'} else 'runtime',request_hash,item['hash'],'QUEUED',canonical(args).decode(),None,time.time(),time.time()))
            except Exception:
                # SQLite cross-process reservation conflict must not dispatch twice.
                self.pending.release();raise ToolError('REQUEST_RESERVATION_CONFLICT') from None
        cancel=threading.Event()
        with self.running_lock:
            self.running[request_id]=cancel
        worker=threading.Thread(target=self._execute,args=(request_id,cancel),daemon=True)
        with self.running_lock:self.threads[request_id]=worker
        worker.start()
        return {'request_id':request_id,'status':'QUEUED','package_hash':item['hash']}

    def _execute(self,request_id,cancel):
        with self.registry.transaction():
            row=self.registry.db.execute('SELECT * FROM jobs WHERE id=?',(request_id,)).fetchone()
            self.registry.db.execute('UPDATE jobs SET status=?,updated=? WHERE id=?',('RUNNING',time.time(),request_id))
        try:
            if row['kind']=='pipeline':
                if row['package_hash']!=self.pipeline['hash']:raise ToolError('IMAGE_APPROVAL_MISMATCH')
                args=json.loads(row['input'])
                result=self.executor.pipeline(args['steps'],args['text'],cancel=cancel)
                validate_value(self.pipeline['contract']['output_schema'],result['result'],'INVALID_OUTPUT')
                status='SUCCEEDED'
            elif row['kind']=='adapter':
                with self.registry.lock:
                    adapter=self.registry.db.execute('SELECT name FROM adapters WHERE hash=?',(row['package_hash'],)).fetchone()
                if not adapter:raise ToolError('UNKNOWN_ADAPTER')
                result={'result':self.adapters.call(adapter['name'],json.loads(row['input']),request_id,
                                                   expected_hash=row['package_hash'])}
                # Do not turn legacy UNKNOWN/ACCEPTED/DISPATCHED into physical success.
                legacy=result['result']
                legacy_state=legacy.get('status',legacy.get('result')) if isinstance(legacy,dict) else None
                status='UNKNOWN' if legacy_state in {'UNKNOWN','ACCEPTED','DISPATCHED','PENDING_DO_NOT_RETRY'} else 'RETURNED'
            else:
                package=self.registry.load_package(row['package_hash'])
                result=self.executor.run_package(package,json.loads(row['input']),cancel=cancel)
                validate_value(package['contract']['output_schema'],result['result'],'INVALID_OUTPUT')
                status='SUCCEEDED'
        except ToolError as e:
            result={'code':e.code};status='CANCELLED' if e.code=='CANCELLED' else 'FAILED'
            if row['kind']=='adapter' and e.code=='INVALID_ADAPTER_OUTPUT':status='UNKNOWN'
            self.registry.record_error(row['package_hash'])
            if e.code in {'PACKAGE_HASH_MISMATCH','PACKAGE_READ_FAILED','IMAGE_APPROVAL_MISMATCH'}:
                self.registry.suspend(row['package_hash'],'QUARANTINED')
        except Exception:
            result={'code':'UNEXPECTED_EXECUTION_ERROR'};status='UNKNOWN'
        with self.registry.transaction():
            self.registry.db.execute('UPDATE jobs SET status=?,result=?,input=NULL,updated=? WHERE id=?',
                (status,canonical(result).decode(),time.time(),request_id))
        with self.running_lock:
            self.running.pop(request_id,None);self.threads.pop(request_id,None)
        self.pending.release()

    def close(self):
        self.stopping=True
        with self.running_lock:
            for cancel in self.running.values():cancel.set()
            workers=list(self.threads.values())
        builder_done=self.builder.close()
        deadline=time.monotonic()+5
        for worker in workers:worker.join(timeout=max(0,deadline-time.monotonic()))
        return builder_done and all(not worker.is_alive() for worker in workers)

    def job(self,request_id):
        with self.registry.lock:
            row=self.registry.db.execute('SELECT * FROM jobs WHERE id=?',(request_id,)).fetchone()
        if not row:return self.builder.status(request_id)
        return {'request_id':row['id'],'status':row['status'],'package_hash':row['package_hash'],
                'result':json.loads(row['result']) if row['result'] else None}

    def cancel(self,request_id):
        with self.running_lock:
            if request_id in self.running:self.running[request_id].set()
        return self.job(request_id)

    def dispatch(self,request):
        action=request.get('action')
        fields={'capabilities':{'action'},'list':{'action','search'},'describe':{'action','name'},
                'call':{'action','name','args','request_id','expected_hash'},'job':{'action','request_id'},
                'build':{'action','capability','request_id'},'validation':{'action','name'},
                'cancel':{'action','request_id'}}
        if action not in fields or set(request)-fields[action]:
            raise ToolError('PUBLIC_ACTION_FORBIDDEN')
        if action=='capabilities':return {'expected_hash':True,'automatic_build':False}
        if action=='list':
            search=request.get('search','')
            if not isinstance(search,str) or len(search)>200:raise ToolError('INVALID_SEARCH')
            return self.registry.listing(search)+[x for x in self.adapters.listing()+[self.pipeline] if search.casefold() in x['name'].casefold()]
        if action=='describe':
            if request['name']=='pipeline_run':return self.pipeline
            try:return self.registry.describe(request['name'])
            except ToolError as e:
                if e.code!='TOOL_NOT_REGISTERED':raise
                return self.adapters.describe(request['name'])
        if action=='call':return self.submit(request['name'],request['args'],request['request_id'],request.get('expected_hash'))
        if action=='job':return self.job(request['request_id'])
        if action=='cancel':return self.cancel(request['request_id'])
        if action=='build':
            return self.builder.request(request['request_id'],request['capability'])
        item=self.registry.describe(request['name'],active_only=False)
        with self.registry.lock:
            rows=self.registry.db.execute('SELECT report FROM validations WHERE hash=? ORDER BY at',(item['hash'],)).fetchall()
        return [json.loads(row['report']) for row in rows]


def serve(service,path):
    class Handler(socketserver.StreamRequestHandler):
        def handle(self):
            self.request.settimeout(10)
            try:
                raw=self.rfile.readline(2*1024*1024+1)
                if len(raw)>2*1024*1024 or not raw.endswith(b'\n'):raise ToolError('PAYLOAD_LIMIT')
                response={'ok':True,'result':service.dispatch(json.loads(raw))}
            except ToolError as e:response={'ok':False,'code':e.code}
            except Exception:response={'ok':False,'code':'INVALID_REQUEST'}
            self.wfile.write(canonical(response)+b'\n')
    path=Path(path)
    if path.exists() or path.is_symlink():
        info=path.lstat()
        if not stat.S_ISSOCK(info.st_mode) or info.st_uid!=os.getuid() or info.st_mode & 0o077:
            raise ToolError('SOCKET_ALREADY_EXISTS')
        with socket.socket(socket.AF_UNIX,socket.SOCK_STREAM) as check:
            check.settimeout(1)
            try:check.connect(str(path))
            except ConnectionRefusedError:path.unlink()
            else:raise ToolError('SOCKET_ALREADY_EXISTS')
    os.umask(0o077)
    server=socketserver.UnixStreamServer(str(path),Handler)
    path.chmod(0o600)
    prior={}
    if threading.current_thread() is threading.main_thread():
        def stop(signum,frame):
            threading.Thread(target=server.shutdown,daemon=True).start()
        for signum in (signal.SIGTERM,signal.SIGINT):
            prior[signum]=signal.signal(signum,stop)
    try:server.serve_forever()
    finally:
        server.server_close();path.unlink(missing_ok=True)
        for signum,handler in prior.items():signal.signal(signum,handler)


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--state',type=Path,required=True)
    parser.add_argument('--image-id',required=True)
    parser.add_argument('--use-shared-model',action='store_true')
    parser.add_argument('--existing-adapters',action='store_true')
    parser.add_argument('action',choices=['serve','srt-pilot','gateway-check','list','inspect','disable','rollback','revalidate','approve',
                                        'adapter-list','adapter-approve','adapter-disable'])
    parser.add_argument('--hash');parser.add_argument('--name',default='srt_inspect')
    parser.add_argument('--permissions',nargs='*',default=None)
    args=parser.parse_args()
    registry=Registry(args.state);executor=DockerExecutor(args.state/'executor',args.image_id)
    model=None
    if args.use_shared_model:
        model=SharedModel.from_environment()
    service=None;owned=[]
    try:
        if args.action=='gateway-check':
            if model is None:raise ToolError('EXPLICIT_BATCH_CONFIG_REQUIRED')
            print(json.dumps(model.inspect()))
        elif args.action in {'serve','srt-pilot'}:
            # Acquire before crash recovery: a second service must never mark live jobs UNKNOWN.
            import fcntl
            fd=os.open(args.state/'service.lock',os.O_WRONLY|os.O_CREAT|os.O_NOFOLLOW,0o600)
            lock=os.fdopen(fd,'a')
            try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
            except BlockingIOError:raise ToolError('SERVICE_ALREADY_RUNNING') from None
            service=ToolService(registry,executor,model)
            if args.existing_adapters:
                from .legacy_setup import install_existing
                owned=install_existing(service)
            if args.action=='serve':serve(service,args.state/'service.sock')
            else:print(json.dumps(service.build_srt()))
        elif args.action=='list':print(json.dumps(registry.listing(active_only=False)))
        elif args.action=='inspect':print(json.dumps(registry.load_package(args.hash)))
        elif args.action=='disable':registry.suspend(args.hash)
        elif args.action=='rollback':registry.rollback(args.name,args.hash)
        elif args.action=='revalidate':print(json.dumps(Validator(registry,executor).validate(args.hash)))
        elif args.action=='approve':
            if args.permissions is None:raise ToolError('EXPLICIT_SCOPE_REQUIRED')
            registry.activate(args.hash,human_scope=args.permissions)
        elif args.action=='adapter-list':print(json.dumps(AdapterCatalog(registry).review()))
        elif args.action=='adapter-approve':
            if args.permissions is None:raise ToolError('EXPLICIT_SCOPE_REQUIRED')
            AdapterCatalog(registry).approve(args.hash,args.permissions)
        elif args.action=='adapter-disable':AdapterCatalog(registry).disable(args.hash)
    finally:
        if service is None or service.close():
            for resource in owned:resource.close()
            registry.close()


if __name__=='__main__':main()
