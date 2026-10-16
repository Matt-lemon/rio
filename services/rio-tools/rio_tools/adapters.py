"""Trusted wrappers around existing handlers; they retain their own policy gates."""
import hashlib
import inspect
import json
from pathlib import Path
from core.tools.contracts import ToolError, canonical, digest, validate_value


class AdapterCatalog:
    def __init__(self,registry):
        self.registry=registry;self.bindings={}
        registry.db.executescript('''
          CREATE TABLE IF NOT EXISTS adapters(hash TEXT PRIMARY KEY,name TEXT NOT NULL,
            definition TEXT NOT NULL,state TEXT NOT NULL,approval TEXT);
          CREATE TABLE IF NOT EXISTS adapter_active(name TEXT PRIMARY KEY,hash TEXT NOT NULL);
        ''')

    def install(self,name,schema,callback,source_type,*,mutates=False):
        # Only trusted service setup can install callbacks. No public install endpoint.
        source=Path(inspect.getfile(source_type)).resolve()
        code_hash=hashlib.sha256(source.read_bytes()).hexdigest()
        definition={'name':name,'description':'Existing protected handler: '+name,
                    'kind':'adapter','version':'1.0.0','input_schema':schema,
                    'output_schema':{'type':'object','maxProperties':64},
                    'source_hash':code_hash,'source_component':source_type.__name__,
                    'permissions':['device.control' if name.startswith(('home_','pc_')) and mutates
                                   else 'external.send' if mutates else 'external.read']}
        key=digest(definition)
        with self.registry.transaction():
            self.registry.db.execute('INSERT OR IGNORE INTO adapters VALUES(?,?,?,?,NULL)',
                (key,name,canonical(definition).decode(),'PENDING_APPROVAL' if mutates else 'ACTIVE'))
            if not mutates:
                self.registry.db.execute('INSERT INTO adapter_active VALUES(?,?) ON CONFLICT(name) DO UPDATE SET hash=excluded.hash',(name,key))
        self.bindings[name]=(callback,source,key)

    def home(self,home):
        for tool in home.schemas():
            name=tool['name']
            self.install(name,tool['parameters'],lambda args,key,n=name:
                         home.call(n,{**args,**({'idempotency_key':key} if n=='home_set_light' else {})}),
                         type(home),mutates=name=='home_set_light')

    def pc(self,controller):
        empty={'type':'object','properties':{},'additionalProperties':False}
        for name,method in [('pc_status','observe'),('pc_wake','wake'),('pc_shutdown','shutdown')]:
            self.install(name,empty,lambda args,key,m=method:getattr(controller,m)(),type(controller),mutates=name!='pc_status')

    def dubbing(self,client,definitions):
        reads={'dubbing_health','dubbing_status','dubbing_cloud_search'}
        for descriptor in definitions:
            spec=descriptor['function'];name=spec['name']
            self.install(name,spec['parameters'],lambda args,key,n=name:client.dispatch(n,args),type(client),mutates=name not in reads)

    def listing(self):
        with self.registry.lock:
            rows=self.registry.db.execute('SELECT a.* FROM adapters a JOIN adapter_active p ON a.hash=p.hash WHERE a.state=?',('ACTIVE',)).fetchall()
        return [dict(row)|{'contract':json.loads(row['definition'])} for row in rows]

    def describe(self,name):
        matches=[x for x in self.listing() if x['name']==name]
        if not matches:
            with self.registry.lock:
                exists=self.registry.db.execute('SELECT 1 FROM adapters WHERE name=?',(name,)).fetchone()
            raise ToolError('ADAPTER_NOT_ACTIVE' if exists else 'TOOL_NOT_REGISTERED')
        return matches[0]

    def approve(self,key,permissions):
        """Local administrator only, separate from the model-facing API."""
        with self.registry.transaction():
            row=self.registry.db.execute('SELECT * FROM adapters WHERE hash=?',(key,)).fetchone()
            if not row:raise ToolError('UNKNOWN_ADAPTER')
            spec=json.loads(row['definition'])
            if set(permissions)!=set(spec['permissions']):raise ToolError('APPROVAL_SCOPE_MISMATCH')
            self.registry.db.execute('UPDATE adapters SET state=?,approval=? WHERE hash=?',
                ('ACTIVE',canonical({'hash':key,'permissions':permissions}).decode(),key))
            self.registry.db.execute('INSERT INTO adapter_active VALUES(?,?) ON CONFLICT(name) DO UPDATE SET hash=excluded.hash',(row['name'],key))

    def review(self):
        with self.registry.lock:
            rows=self.registry.db.execute('SELECT * FROM adapters ORDER BY name, rowid').fetchall()
        return [dict(row)|{'contract':json.loads(row['definition'])} for row in rows]

    def disable(self,key):
        with self.registry.transaction():
            self.registry.db.execute("UPDATE adapters SET state='DISABLED' WHERE hash=?",(key,))
            self.registry.db.execute('DELETE FROM adapter_active WHERE hash=?',(key,))

    def call(self,name,args,key,expected_hash=None):
        item=self.describe(name);spec=item['contract'];validate_value(spec['input_schema'],args)
        if expected_hash is not None and item['hash']!=expected_hash:
            raise ToolError('ADAPTER_VERSION_CHANGED')
        if name not in self.bindings:raise ToolError('ADAPTER_NOT_CONFIGURED')
        callback,source,bound_hash=self.bindings[name]
        if bound_hash!=item['hash'] or hashlib.sha256(source.read_bytes()).hexdigest()!=spec['source_hash']:
            raise ToolError('ADAPTER_SOURCE_CHANGED')
        # Underlying Home target/permissions, PC reservation/readiness, and dubbing
        # auth/revision checks remain authoritative; no backup or auto replay here.
        value=callback(args,key)
        validate_value(spec['output_schema'],value,'INVALID_ADAPTER_OUTPUT')
        return value
