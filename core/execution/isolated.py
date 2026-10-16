"""Core-side clients: no operator credentials, Docker or device transports."""
from __future__ import annotations

import os
import time
from pathlib import Path

from core.research_browser import BrowserConversation
from .catalog import Catalog
from .engine import Task
from .peers import PeerClient,load_policy
from .proactive import Suggestions
from .service import Application,serve
from .store import Store
from .types import ToolResult,source_revision


class RemoteCatalog(Catalog):
    def __init__(self,peer):self.peer=peer
    def list(self,search='',*,planner=False):return self.peer.request('catalog',search=search,planner=planner)
    def legacy(self):return {t['name']:t for t in self.list(planner=True)}


class RemoteAdapters:
    def __init__(self,peer,store):self.peer,self.store=peer,store

    def call_tracked(self,descriptor,args,key,browser,progress):
        progress.attempted('OPERATOR_BRIDGE_IPC_BOUNDARY')
        value=self.peer.request('call',session=browser.session_id,name=descriptor['name'],args=args,
            revision=descriptor['revision'],request_id=key,original_request=progress.claim['completion'])
        result=ToolResult.restore(value['result'])
        if result.request_id!=key:raise ValueError('RESULT_ID_MISMATCH')
        history=value.get('history') or {}
        if any(r['phase']=='ACCEPTED' for r in history.get('transitions',[])):
            progress.accepted('BRIDGE_OBSERVED_BACKEND_RECEIPT')
        saved=value.get('browser',{})
        age=self.store.clock()-saved.get('checked_at',0)
        if 0<=age<600:
            browser.results=saved.get('results',[]);browser.index=saved.get('index');browser.required_host=saved.get('required_host');browser.checked=time.monotonic()-age
        else:browser.clear()
        return result

    def status(self,name,args,key):
        row=self.store.row('SELECT tasks.session_id FROM calls JOIN tasks ON tasks.id=calls.task_id WHERE calls.id=?',(key,))
        if not row:raise ValueError('REQUEST_NOT_FOUND')
        value=self.peer.request('status',session=row['session_id'],request_id=key)
        return ToolResult.restore(value['result'])


class RemoteSuggestions(Suggestions):
    def __init__(self,store,peer):
        super().__init__(store,lambda query:peer.request('research',query=query));self.peer=peer
    def import_legacy_events(self):
        for row in self.peer.request('legacy.events'):self.ingest(row['kind'],row['text'],row['id'])


def main():
    policy=load_policy()
    if os.getuid()!=policy['core_uid']:raise ValueError('CORE_ACCOUNT_REQUIRED')
    if os.access('/var/run/docker.sock',os.R_OK|os.W_OK):raise ValueError('CORE_DOCKER_ACCESS_FORBIDDEN')
    from .isolation_audit import audit
    inspected=audit()
    if inspected['docker_connect']=='ALLOWED':raise ValueError('CORE_DOCKER_ACCESS_FORBIDDEN')
    peer=PeerClient(policy['bridge_socket'],policy['operator_uid'])
    bridge_health=peer.request('health')
    if not bridge_health.get('ready') or bridge_health.get('source_revision')!=source_revision():raise ValueError('BRIDGE_SOURCE_CHANGED')
    inspected['bridge_uid']=bridge_health['process_uid'];inspected['operator_context']=bridge_health['operator_context']
    store=Store(Path.home())
    task=Task(store,catalog=RemoteCatalog(peer),adapters=RemoteAdapters(peer,store),
        selector=lambda payload:peer.request('model.select',payload=payload),
        planner=lambda payload:peer.request('model.plan',payload=payload),
        responder=lambda session,request,results:peer.request('model.reply',session=session,request=request,results=results))
    app=Application(store,task,RemoteSuggestions(store,peer))
    app.isolation_audit=inspected
    serve(store.root,policy['core_socket'],application=app,peer_uid=policy['operator_uid'])


if __name__=='__main__':main()
