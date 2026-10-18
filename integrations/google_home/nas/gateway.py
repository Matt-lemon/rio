"""Isolated Rio Home gateway. No offline queue, no write retries, no SDK on NAS."""
import asyncio
import hashlib
import hmac
import http
import json
import logging
import os
from pathlib import Path
import secrets
import sqlite3
import ssl
import time
from websockets.asyncio.server import serve
from websockets.exceptions import ConnectionClosed

BASE = Path.home() / 'google-home-bridge'
STATE = BASE / 'state/gateway'
DEVICE_TYPES = frozenset({'OnOffPluginUnitDevice','RoomAirConditionerDevice','GoogleTVDevice',
                         'GoogleSoundbarDevice','GoogleStreamingSoundbarDevice','SpeakerDevice'})
MAX_AGE = 90
HEARTBEAT_TIMEOUT = 35
PROTOCOL_VERSION = 1
SUPPORTED_OPERATIONS = ('get_state', 'set_power', 'device_command')
SOURCE_SHA256 = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()

class Gateway:
    def __init__(self, state=STATE):
        self.state = Path(state)
        self.bridge_token = (self.state / 'bridge.token').read_text().strip()
        self.rio_token = (self.state / 'rio.token').read_text().strip()
        self.policy = json.loads((self.state / 'policy.json').read_text())
        self.db = sqlite3.connect(self.state / 'jobs.sqlite')
        self.db.execute('CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY, digest TEXT, status TEXT, result TEXT, expires REAL)')
        self.db.execute('CREATE TABLE IF NOT EXISTS job_recoveries '
                        '(id TEXT NOT NULL, previous_status TEXT NOT NULL, '
                        'previous_result TEXT NOT NULL, recovered_at REAL NOT NULL)')
        # A process restart cannot prove whether an in-flight device command executed.
        self.db.execute("INSERT INTO job_recoveries SELECT id,status,result,? FROM jobs WHERE status='DISPATCHED'", (time.time(),))
        self.db.execute("UPDATE jobs SET status='UNKNOWN', result=? WHERE status='DISPATCHED'",
                        (json.dumps({'code': 'GATEWAY_RESTART_DO_NOT_RETRY'}),))
        self.db.commit()
        self.ws = None
        self.session = None
        self.mode = 'OFFLINE'
        self.permission = 'UNOBSERVED'
        self.catalog = []
        self.heartbeat_at = 0.
        self.snapshot_at = 0.
        self.pending = None
        self.connected_at = 0.
        self.connection_count = 0
        self.snapshot_count = 0
        self.last_snapshot_diagnostics = {}
        self.heartbeat_count = 0
        self.lock = asyncio.Lock()

    def expire_pending(self):
        if self.pending:
            row = self.db.execute('SELECT expires FROM jobs WHERE id=?', (self.pending,)).fetchone()
            if row and time.time() > row[0]:
                self.finish(self.pending, 'UNKNOWN', {'code':'RESULT_TIMEOUT'})

    def status(self):
        self.expire_pending()
        now = time.monotonic()
        fresh = self.ws is not None and self.mode == 'READY' and bool(self.catalog) and self.permission == 'GRANTED' and now-self.heartbeat_at <= HEARTBEAT_TIMEOUT and now-self.snapshot_at <= MAX_AGE
        mode = 'DEGRADED' if self.mode == 'READY' and not self.catalog else self.mode
        return {'bridge_state': mode if not (mode=='READY' and not fresh) else 'STALE',
                'available': fresh, 'permission':self.permission,
                'controls_enabled': fresh and self.policy.get('controls_enabled', False),
                'device_controls_enabled': fresh and self.policy.get('device_controls_enabled') is True,
                'protocol_version': PROTOCOL_VERSION,
                'supported_operations': list(SUPPORTED_OPERATIONS),
                'gateway_revision': SOURCE_SHA256,
                'heartbeat_age_s':round(now-self.heartbeat_at,1) if self.ws else None,
                'snapshot_age_s':round(now-self.snapshot_at,1) if self.snapshot_at else None,
                'device_count':len(self.catalog) if fresh else 0,
                'connections':self.connection_count,'snapshots':self.snapshot_count,'heartbeats':self.heartbeat_count,
                'inflight':bool(self.pending), 'rio_core_independent':True, 'sync_diagnostics':self.last_snapshot_diagnostics}

    def finish(self, ident, status, result):
        self.db.execute('UPDATE jobs SET status=?,result=? WHERE id=?', (status,json.dumps(result),ident))
        self.db.commit()
        if self.pending == ident: self.pending = None

    def disconnect(self):
        if self.pending: self.finish(self.pending,'UNKNOWN',{'code':'CONNECTION_LOST_DO_NOT_RETRY'})
        self.ws = None
        self.session = None
        self.catalog = []
        self.snapshot_at = 0.
        self.mode = 'OFFLINE'
        self.permission = 'UNOBSERVED'

    async def authenticate(self, connection, request):
        if request.path != '/bridge': return connection.respond(http.HTTPStatus.NOT_FOUND,'Not found\n')
        auth = request.headers.get_all('Authorization')
        if len(auth)!=1 or not hmac.compare_digest(auth[0], 'Bearer '+self.bridge_token):
            return connection.respond(http.HTTPStatus.UNAUTHORIZED,'Unauthorized\n')
        if self.ws is not None: return connection.respond(http.HTTPStatus.CONFLICT,'Bridge already connected\n')

    async def bridge(self, ws):
        # Recheck after handshake: two simultaneous authenticating clients must not replace each other.
        if self.ws is not None:
            await ws.close(1008,'Already connected'); return
        self.ws = ws
        self.session = secrets.token_hex(16)
        session = self.session
        self.connected_at = self.heartbeat_at = time.monotonic()
        self.mode = 'SYNCING'
        self.connection_count += 1
        await ws.send(json.dumps({'type':'hello','protocol':PROTOCOL_VERSION,'session':session,'server_time_ms':int(time.time()*1000),'heartbeat_s':10,'snapshot_s':60}))
        try:
            async for raw in ws:
                data = json.loads(raw)
                if not isinstance(data,dict) or data.get('session') != session: raise ValueError('session')
                kind=data.get('type')
                if kind=='heartbeat':
                    self.heartbeat_at = time.monotonic(); self.heartbeat_count += 1
                elif kind=='snapshot':
                    self.permission=data.get('permission','UNOBSERVED')
                    source=data.get('diagnostics',{})
                    self.last_snapshot_diagnostics={k:source[k] for k in ('timeouts','errors','structures','onoff_traits','light_type_matches','overall_timeout','structure_observer_active','device_flow_emissions','empty_device_flow_emissions','empty_device_waits','missing_room_metadata','empty_type_waits','device_observer_active','device_emission_age_ms','screen_interactive','device_idle_mode','battery_optimization_exempt','room_observer_active','room_flow_emissions','empty_room_flow_emissions','room_count','room_emission_age_ms','permission_observer_active','permission_stream_active','permission_observer_completions','structure_observer_starts','permission_observation_age_ms','permission_wait_timeout','inventory_stall_repairs') if isinstance(source,dict) and k in source and type(source[k]) in (int,bool)}
                    for k in ('type_factory_counts','error_classes'):
                        values=source.get(k,{}) if isinstance(source,dict) else {}
                        if isinstance(values,dict): self.last_snapshot_diagnostics[k]={name:count for name,count in values.items() if isinstance(name,str) and 0<len(name)<=80 and all(c.isalnum() or c=='_' for c in name) and type(count) is int and 0<=count<=500}
                    self.catalog=[]; self.snapshot_at=0.; self.mode='SYNCING'
                    if self.permission!='GRANTED':
                        self.mode='SYNCING' if self.permission=='PERMISSIONS_STATE_UNINITIALIZED' else 'PERMISSION_REQUIRED'
                        if self.pending: self.finish(self.pending,'UNKNOWN',{'code':'PERMISSION_REVOKED'})
                    elif data.get('complete') is True:
                        devices=data.get('devices')
                        if not isinstance(devices,list) or len(devices)>500: raise ValueError('catalog')
                        aliases=set()
                        for item in devices:
                            if not isinstance(item,dict) or not isinstance(item.get('alias'),str) or len(item['alias'])>100 or item['alias'] in aliases: raise ValueError('alias')
                            aliases.add(item['alias'])
                        if devices:
                            self.catalog=devices; self.snapshot_at=time.monotonic(); self.snapshot_count+=1; self.mode='READY'
                        else:
                            self.mode='DEGRADED'
                    else: self.mode='DEGRADED'
                    await ws.send(json.dumps({'type':'snapshot_ack','session':session,'state':self.mode}))
                elif kind=='result':
                    ident=data.get('id')
                    if ident==self.pending:
                        status=data.get('status')
                        if status not in ('SUCCEEDED','REJECTED','UNKNOWN'): raise ValueError('result')
                        result=data.get('result',{})
                        if not isinstance(result,dict): raise ValueError('result')
                        self.finish(ident,status,result)
                else: raise ValueError('type')
        except ConnectionClosed: pass
        except (ValueError,TypeError,KeyError):
            await ws.close(1008,'Invalid protocol')
        finally:
            if self.ws is ws: self.disconnect()

    async def command(self, data):
        if not isinstance(data,dict): return 400,{'code':'INVALID_BODY'}
        if set(data)-{'idempotency_key','operation','alias','arguments','ttl_s'}: return 400,{'code':'UNKNOWN_FIELD'}
        ident=data.get('idempotency_key')
        if not isinstance(ident,str) or not 8<=len(ident)<=100 or not all(c.isalnum() or c in '-_' for c in ident): return 400,{'code':'INVALID_IDEMPOTENCY_KEY'}
        op=data.get('operation'); args=data.get('arguments',{})
        if op not in SUPPORTED_OPERATIONS or not isinstance(args,dict): return 400,{'code':'INVALID_OPERATION'}
        if (op=='get_state' and args) or (op=='set_power' and (set(args)!= {'on'} or type(args['on']) is not bool)): return 400,{'code':'INVALID_ARGUMENTS'}
        if op=='device_command':
            if (set(args)!={'device_type','command'} or not isinstance(args.get('device_type'),str)
                    or args.get('device_type') not in DEVICE_TYPES
                    or args.get('command') not in ('on','off','mute','unmute','play','pause','stop')):
                return 400,{'code':'INVALID_ARGUMENTS'}
        ttl=data.get('ttl_s',20)
        if type(ttl) not in (int,float) or not 1<=ttl<=30: return 400,{'code':'INVALID_TTL'}
        digest=hashlib.sha256(json.dumps(data,sort_keys=True,separators=(',',':')).encode()).hexdigest()
        async with self.lock:
            old=self.db.execute('SELECT digest,status,result FROM jobs WHERE id=?',(ident,)).fetchone()
            if old:
                if not hmac.compare_digest(old[0],digest): return 409,{'code':'IDEMPOTENCY_CONFLICT'}
                return 200,{'id':ident,'status':old[1],'result':json.loads(old[2]),'replayed':False}
            if not self.status()['available']: return 503,{'code':'BRIDGE_NOT_READY','bridge':self.status()}
            item=next((d for d in self.catalog if d['alias']==data.get('alias')),None)
            if item is None: return 404,{'code':'UNKNOWN_DEVICE'}
            if op=='set_power':
                if not self.policy.get('controls_enabled') or item['alias'] not in self.policy.get('allowed_aliases',[]): return 403,{'code':'CONTROL_NOT_APPROVED'}
                if not item.get('lighting') or not item.get('commands',{}).get('on' if args['on'] else 'off'): return 422,{'code':'UNSUPPORTED_COMMAND'}
            if op=='device_command':
                binding=self.policy.get('device_bindings',{}).get(item['alias'],{})
                if (self.policy.get('device_controls_enabled') is not True
                        or item.get('name') in ('컴퓨터','환풍기','청소기','Mi Robot Vacuum')
                        or binding.get('expected_name')!=item.get('name')
                        or binding.get('device_type')!=args['device_type']
                        or args['command'] not in binding.get('allowed_commands',[])):
                    return 403,{'code':'CONTROL_NOT_APPROVED'}
                current=item.get('device_control_binding',{})
                caps=[c for c in item.get('device_capabilities',[]) if c.get('device_type')==args['device_type']]
                if (args['command'] in ('play','pause','stop') and (len(caps)!=1
                        or caps[0].get('media_connectivity') not in ('ONLINE','PARTIALLY_ONLINE'))):
                    return 422,{'code':'DEVICE_OFFLINE_OR_UNKNOWN'}
                if (len(caps)!=1 or caps[0].get('supported_commands',{}).get(args['command']) is not True
                        or current.get('status')!='SELECTED_EXPLICIT_TYPE'
                        or current.get('device_type')!=args['device_type']
                        or current.get('allowed_commands',{}).get(args['command']) is not True):
                    return 422,{'code':'UNSUPPORTED_COMMAND'}
            if self.pending: return 409,{'code':'BRIDGE_BUSY'}
            expires=time.time()+ttl
            self.db.execute('INSERT INTO jobs VALUES (?,?,?,?,?)',(ident,digest,'DISPATCHED','{}',expires)); self.db.commit()
            self.pending=ident
            # Persist before send; an ambiguous send/disconnect is UNKNOWN, never automatically resent.
            try:
                await self.ws.send(json.dumps({'type':'command','session':self.session,'id':ident,'operation':op,'alias':item['alias'],'arguments':args,'expires_ms':int(expires*1000)}))
            except (ConnectionClosed, TimeoutError, ConnectionError, OSError):
                self.finish(ident,'UNKNOWN',{'code':'SEND_UNCERTAIN'})
            return 202,{'id':ident,'status':self.db.execute('SELECT status FROM jobs WHERE id=?',(ident,)).fetchone()[0]}

    async def local_http(self, reader, writer):
        code=400; answer={'code':'BAD_REQUEST'}
        try:
            head=await asyncio.wait_for(reader.readuntil(b'\r\n\r\n'),5)
            if len(head)>8192: raise ValueError()
            lines=head.decode('ascii').split('\r\n'); method,path,_=lines[0].split(' ')
            pairs=[s.split(':',1) for s in lines[1:] if s]
            headers={k.lower():v.strip() for k,v in pairs}
            if len(headers)!=len(pairs) or 'transfer-encoding' in headers: raise ValueError()
            if not hmac.compare_digest(headers.get('authorization',''),'Bearer '+self.rio_token): code=401; answer={'code':'UNAUTHORIZED'}
            elif method=='GET' and path=='/v1/status': code=200; answer=self.status()
            elif method=='GET' and path=='/v1/catalog': code=200; answer={'bridge':self.status(),'devices':self.catalog if self.status()['available'] else []}
            elif method=='GET' and path.startswith('/v1/jobs/'):
                self.expire_pending()
                ident=path.removeprefix('/v1/jobs/')
                row=self.db.execute('SELECT status,result FROM jobs WHERE id=?',(ident,)).fetchone()
                code=200 if row else 404; answer={'id':ident,'status':row[0],'result':json.loads(row[1])} if row else {'code':'NOT_FOUND'}
            elif method=='POST' and path=='/v1/commands':
                n=int(headers.get('content-length','0'))
                if not 0<n<=8192: raise ValueError()
                body=await asyncio.wait_for(reader.readexactly(n),5)
                code,answer=await self.command(json.loads(body))
            else: code=404; answer={'code':'NOT_FOUND'}
        except (ValueError,TypeError,UnicodeError,asyncio.TimeoutError,asyncio.IncompleteReadError,asyncio.LimitOverrunError): pass
        finally:
            payload=json.dumps(answer,ensure_ascii=False).encode()
            writer.write(f'HTTP/1.1 {code} {http.HTTPStatus(code).phrase}\r\nContent-Type: application/json; charset=utf-8\r\nContent-Length: {len(payload)}\r\nConnection: close\r\nCache-Control: no-store\r\n\r\n'.encode()+payload)
            try: await writer.drain()
            except ConnectionError: pass
            writer.close(); await writer.wait_closed()

    async def monitor(self):
        while True:
            await asyncio.sleep(2)
            self.expire_pending()
            if self.ws is not None and time.monotonic()-self.heartbeat_at>HEARTBEAT_TIMEOUT:
                ws=self.ws; self.disconnect(); await ws.close(1001,'Heartbeat timeout')
            elif self.mode=='READY' and time.monotonic()-self.snapshot_at>MAX_AGE:
                self.mode='STALE'; self.catalog=[]
                if self.pending: self.finish(self.pending,'UNKNOWN',{'code':'SNAPSHOT_STALE'})

async def main():
    os.umask(0o077)
    logging.getLogger('websockets').setLevel(logging.CRITICAL)
    gateway=Gateway()
    tls=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER); tls.minimum_version=ssl.TLSVersion.TLSv1_2
    tls.load_cert_chain(STATE/'server.pem',STATE/'server.key')
    async with serve(gateway.bridge,'192.0.2.234',18443,ssl=tls,process_request=gateway.authenticate,max_size=512*1024,max_queue=4,ping_interval=20,ping_timeout=20,open_timeout=10), await asyncio.start_server(gateway.local_http,'127.0.0.1',18444,limit=8192):
        await gateway.monitor()

if __name__=='__main__': asyncio.run(main())
