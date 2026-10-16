"""Operator-side bounded adapters/model bridge; no shell or Builder operation."""
from __future__ import annotations

import argparse
from dataclasses import asdict
from datetime import datetime
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import socketserver
import stat
import threading
import time

from .adapters import Adapters
from .catalog import Catalog
from .engine import Task
from .peers import load_policy,peer_uid,grant_socket
from .store import Store
from .types import ToolResult,encoded,fingerprint,source_revision


def operator_context():
    import rio_core
    names=('LLM_BASE_URL','LLM_MODEL','LLM_TIMEOUT','LLM_TEMPERATURE','LLM_TOP_P','LLM_TOP_K',
           'LLM_MIN_P','LLM_MAX_TOKENS','LLM_REPEAT_PENALTY','LLM_REPEAT_LAST_N','LLM_REASONING_EFFORT')
    return {'identity':hashlib.sha256(rio_core.IDENTITY_PATH.read_bytes()).hexdigest() if rio_core.IDENTITY_PATH.is_file() else None,
            'model':fingerprint({k:getattr(rio_core,k) for k in names}),
            'memory':fingerprint({k:os.getenv(k) for k in ('RIO_MEMORY_ENABLED','MEMORY_READ_ENABLED','MEMORY_API_URL','MEMORY_MIN_SCORE','MEMORY_SEARCH_LIMIT','RIO_MEMORY_SHADOW')})}


class Bridge:
    def __init__(self,store,*,catalog=None,adapters=None,project=None):
        self.store=store
        self.catalog=catalog or Catalog()
        self.engine=Task(store,catalog=self.catalog,adapters=adapters or Adapters(store))
        self.lock=threading.RLock()
        self.project=Path(project or Path(__file__).resolve().parents[2])
        store.interrupted()
        with store.transaction() as db:
            db.execute('CREATE TABLE IF NOT EXISTS bridge_calls(request_id TEXT PRIMARY KEY,session_id TEXT NOT NULL,name TEXT NOT NULL,args TEXT NOT NULL,revision TEXT NOT NULL,task_id TEXT NOT NULL)')

    def snapshot(self,session):
        return self.store.session(session)['browser']

    def result(self,row,result):
        history=self.engine.runner.history(row['request_id'],row['session_id']) if self.store.row('SELECT id FROM calls WHERE id=?',(row['request_id'],)) else None
        return {'result':result.public(),'browser':self.snapshot(row['session_id']),'history':history}

    def call(self,request):
        if set(request)!={'op','session','name','args','revision','request_id','original_request'}:
            raise ValueError('BRIDGE_FIELDS_FORBIDDEN')
        session,key=request['session'],request['request_id']
        if not isinstance(session,str) or not re.fullmatch('[a-f0-9]{32}',session):raise ValueError('INVALID_SESSION')
        if not isinstance(key,str) or not re.fullmatch('[A-Za-z0-9_-]{8,100}',key):raise ValueError('INVALID_REQUEST_ID')
        original=request['original_request']
        if not isinstance(original,str) or not 1<=len(original)<=2000:raise ValueError('PUBLIC_REQUEST_REQUIRED')
        with self.lock:
            old=self.store.row('SELECT * FROM bridge_calls WHERE request_id=?',(key,))
            if old:
                if (old['session_id'],old['name'],old['args'],old['revision'])!=(session,request['name'],encoded(request['args']),request['revision']):
                    raise ValueError('REQUEST_ID_CONFLICT')
                return self.status(session,key)
            tool=self.catalog.check(request['name'],request['args'],request['revision'])
            browser=self.engine.browser(session)
            if tool['name'].startswith('legacy.') or tool['name'] in {'pc.wake','pc.shutdown','spotify.timer.schedule','spotify.timer.cancel'}:
                trusted=self.catalog.resolve(original,browser)
                if not trusted or trusted.get('name')!=tool['name'] or trusted.get('arguments')!=request['args']:
                    raise ValueError('EXPLICIT_COMMAND_REQUIRED')
            task=self.store.create_task(session,original)
            with self.store.transaction() as db:
                db.execute('INSERT INTO bridge_calls VALUES(?,?,?,?,?,?)',(key,session,tool['name'],encoded(request['args']),request['revision'],task['id']))
            claim=self.store.claim_task(task['id'])
            result=self.engine.runner.call(claim,tool['name'],request['args'],key,request['revision'],browser)
            self.engine.save_browser(session,browser,claim)
            self.store.update_task(task['id'],claim=claim,state='COMPLETED' if result.completed else 'UNKNOWN',answer=result.message)
            row=self.store.row('SELECT * FROM bridge_calls WHERE request_id=?',(key,))
            return self.result(row,result)

    def status(self,session,key):
        with self.lock:
            row=self.store.row('SELECT * FROM bridge_calls WHERE request_id=?',(key,))
            if not row or row['session_id']!=session:raise ValueError('REQUEST_NOT_FOUND')
            if not self.store.row('SELECT id FROM calls WHERE id=?',(key,)):
                result=ToolResult('UNKNOWN','이전 중계 접수 뒤 실제 전송 여부가 미확정이야. 다시 보내지 않을게.',request_id=key)
            else:result=self.engine.runner.status(key,session)
            return self.result(row,result)

    def legacy_events(self):
        path=self.project/'data/agency/inbox.jsonl'
        if not path.is_file() or path.is_symlink():return []
        processed=set();state=path.parent/'state.json'
        if state.is_file() and not state.is_symlink() and state.stat().st_size<128*1024:
            try:processed=set(json.loads(state.read_text()).get('processed',[]))
            except (ValueError,TypeError,AttributeError):pass
        with path.open('rb') as stream:
            stream.seek(max(0,path.stat().st_size-512*1024));raw=stream.read(512*1024)
        rows=[]
        for line in raw.decode('utf-8',errors='replace').splitlines()[-500:]:
            try:
                item=json.loads(line)
                if item['id'] in processed:continue
                if item.get('ts') and time.time()-datetime.fromisoformat(item['ts']).timestamp()>7*86400:continue
                rows.append({k:item[k] for k in ('id','kind','text')})
            except (ValueError,TypeError,KeyError):pass
        return rows

    def dispatch(self,request):
        if not isinstance(request,dict):raise ValueError('INVALID_REQUEST')
        op=request.get('op')
        fields={'health':set(),'catalog':{'search','planner'},'call':{'session','name','args','revision','request_id','original_request'},
                'status':{'session','request_id'},'model.select':{'payload'},'model.plan':{'payload'},
                'model.reply':{'session','request','results'},'research':{'query'},'legacy.events':set()}
        if op not in fields or set(request)!={'op'}|fields[op]:raise ValueError('BRIDGE_OPERATION_FORBIDDEN')
        if op=='health':return {'ready':True,'process_uid':os.getuid(),'docker_socket_access':os.access('/var/run/docker.sock',os.R_OK|os.W_OK),'automatic_build':False,
                                'source_revision':source_revision(),'operator_context':operator_context()}
        if op=='catalog':
            if type(request['planner']) is not bool or not isinstance(request['search'],str) or len(request['search'])>200:raise ValueError('INVALID_SEARCH')
            return self.catalog.list(request['search'],planner=request['planner'])
        if op=='call':return self.call(request)
        if op=='status':return self.status(request['session'],request['request_id'])
        if op=='legacy.events':return self.legacy_events()
        if op=='model.select':
            from core.context_tools import local_plan
            return local_plan(request['payload'])
        if op=='model.plan':
            from .planner import next_step
            return next_step(request['payload'])
        if op=='model.reply':
            from .planner import reply
            return reply(request['session'],request['request'],request['results'])
        if op=='research':
            from core.current_knowledge import search_forbidden
            from web_search import web_search
            query=request['query']
            if not isinstance(query,str) or not 1<=len(query)<=160 or search_forbidden(query):raise ValueError('PUBLIC_QUERY_REQUIRED')
            return [asdict(r) for r in web_search(query,max_results=4,timeout=10)]
        raise ValueError('BRIDGE_OPERATION_FORBIDDEN')


def serve(root,path,allowed_uid,project):
    root=Path(root);store=Store(root)
    fd=os.open(root/'service.lock',os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW,0o600)
    fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
    app=Bridge(store,project=project)
    path=Path(path);parent=path.parent.lstat()
    if parent.st_uid!=os.getuid() or not stat.S_ISDIR(parent.st_mode) or parent.st_mode&0o022:raise ValueError('PEER_DIRECTORY_UNSAFE')
    if path.exists():
        from .peers import check_socket
        import socket
        check_socket(path,os.getuid())
        with socket.socket(socket.AF_UNIX,socket.SOCK_STREAM) as check:
            try:check.connect(str(path))
            except ConnectionRefusedError:path.unlink()
            else:raise ValueError('SOCKET_ALREADY_EXISTS')
    slots=threading.BoundedSemaphore(16)
    class Handler(socketserver.StreamRequestHandler):
        def handle(self):
            self.request.settimeout(5)
            if peer_uid(self.request)!=allowed_uid:return
            if not slots.acquire(blocking=False):return
            try:
                raw=self.rfile.readline(65537)
                if len(raw)>65536 or not raw.endswith(b'\n'):raise ValueError('REQUEST_LIMIT')
                response={'ok':True,'result':app.dispatch(json.loads(raw))}
                if len(encoded(response).encode())>524288:raise ValueError('RESPONSE_LIMIT')
            except Exception as error:
                code=str(error) if isinstance(error,ValueError) and re.fullmatch('[A-Z_]{1,80}',str(error)) else 'BRIDGE_REQUEST_FAILED'
                response={'ok':False,'code':code}
            finally:slots.release()
            try:self.wfile.write((encoded(response)+'\n').encode())
            except OSError:pass
    class Server(socketserver.ThreadingMixIn,socketserver.UnixStreamServer):
        daemon_threads=True
    os.umask(0o077);server=Server(str(path),Handler);path.chmod(0o600);grant_socket(path,allowed_uid)
    for sig in (signal.SIGTERM,signal.SIGINT):
        signal.signal(sig,lambda *_:threading.Thread(target=server.shutdown,daemon=True).start())
    try:server.serve_forever(poll_interval=.2)
    finally:
        server.server_close();path.unlink(missing_ok=True);os.close(fd)


def main():
    policy=load_policy()
    if os.getuid()!=policy['operator_uid']:raise ValueError('OPERATOR_ACCOUNT_REQUIRED')
    from dotenv import load_dotenv
    project=Path(policy['operator_project'])
    load_dotenv(project/'.env')
    # Immutable code lives under /opt; identity/data continue to refer to the
    # existing operator project. Credentials never cross to the Core account.
    import rio_core
    rio_core.ROOT=project
    identity=Path(os.getenv('RIO_IDENTITY_PATH',str(project/'config/identity.yaml'))).expanduser()
    rio_core.IDENTITY_PATH=identity if identity.is_absolute() else project/identity
    os.environ.setdefault('RIO_MEMORY_SHADOW_LOG',str(project/'data/memory-shadow.jsonl'))
    import rio_agent
    rio_agent.ROOT=project;rio_agent.CONFIG_PATH=project/'config/agency.yaml';rio_agent.DATA_DIR=project/'data/agency'
    serve(Path.home()/'.local/state/rio/execution-bridge',policy['bridge_socket'],policy['core_uid'],policy['operator_project'])


if __name__=='__main__':main()
