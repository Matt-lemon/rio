#!/usr/bin/env python3
"""Deterministic offline reproducer: run against a supplied checkout, never live.

Usage: python tools/audit_fallback_precheck_race.py CHECKOUT [--baseline]
Baseline policy uses the old v1 contract; otherwise the reviewed v2 contract.
"""
import json
import hashlib
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
from unittest.mock import Mock

sys.path.insert(0,str(Path(sys.argv[1]).resolve()))
os.umask(0o077)
def forbidden(*args,**kwargs):raise AssertionError('OFFLINE_AUDIT_ONLY')
socket.socket.connect=forbidden
subprocess.run=forbidden
for key in tuple(os.environ):
    if key.startswith(('RIO_','SMARTTHINGS_')):os.environ.pop(key)

from core.home import smartthings_fallback as fb
from core.home.device_controls import DeviceControlRegistry

with tempfile.TemporaryDirectory(prefix='rio-fallback-repro-') as temporary:
    os.environ['HOME']=temporary
    root=Path(temporary)
    config=root/'.config/rio';config.mkdir(parents=True)
    alias='dev_'+'a'*24
    item={'device_id':'12345678-1234-4234-8234-123456789abc','label':'에어컨','device_type':'ZIGBEE',
          'confirmed_same_physical_device':True,'allowed_commands':['on','off']}
    version=1 if '--baseline' in sys.argv else 2
    if version==2:item.update(primary_alias=alias,identity_evidence={'method':'operator_physical_verification',
        'reference_sha256':'a'*64,'confirmed_at':'2026-01-01T00:00:00Z'})
    if version==2:
        evidence=item['identity_evidence']
        record=json.dumps({'schema_version':1,'target':'에어컨','primary_alias':alias,
            'smartthings_device_id':item['device_id'],'method':evidence['method'],
            'confirmed_at':evidence['confirmed_at'],'verification_basis':'operator_observed_same_device'})
        hashed=hashlib.sha256(record.encode()).hexdigest();evidence['reference_sha256']=hashed
        directory=config/'fallback-identity-evidence';directory.mkdir()
        proof=directory/(hashed+'.json');proof.write_text(record);proof.chmod(0o600)
    path=config/'smartthings-fallback.json';path.write_text(json.dumps({'schema_version':version,'enabled':True,'targets':{'에어컨':item}}));path.chmod(0o600)
    path=config/'pc-auth-provider.json';path.write_text(json.dumps({'schema_version':1,'provider':'oauth-cli','cli_root':'/not-used','cli_sha256':'a'*64}));path.chmod(0o600)
    sent=[]
    transport=Mock()
    transport.status.return_value={'available':False,'code':'GATEWAY_UNREACHABLE'}
    transport.catalog.return_value={'bridge':{'available':True,'permission':'GRANTED','bridge_state':'READY','controls_enabled':True},
        'devices':[{'alias':alias,'name':'에어컨','device_capabilities':[{'device_type':'OnOffPluginUnitDevice','supported_commands':{'on':True}}],
            'device_control_binding':{'status':'SELECTED_EXPLICIT_TYPE','device_type':'OnOffPluginUnitDevice','allowed_commands':{'on':True}}}]}
    transport.request.side_effect=lambda *args: (sent.append('google-home'),{'id':'primary-race-01','status':'UNKNOWN'})[1]
    registry=DeviceControlRegistry(transport,{'schema_version':1,'enabled':True,'targets':{'에어컨':{
        'alias':alias,'device_type':'OnOffPluginUnitDevice','family':'air_conditioner_power','expected_name':'에어컨','allowed_commands':['on','off']}}},root/'primary/commands.sqlite3')
    cli=Mock(control_enabled=True)
    cli.switch.return_value={'value':'off','timestamp':'2026-01-01T00:00:00Z'}
    cli.on.side_effect=lambda: (sent.append('smartthings'),{'results':[{'status':'UNKNOWN'}]})[1]
    fb.VerifiedLightCli=lambda *args,**kw:cli
    before=fb.no_unresolved_primary
    def interleave(path):
        result=before(path)
        registry.execute('에어컨','on','primary-race-01')
        return result
    fb.no_unresolved_primary=interleave
    try:
        result=fb.attempt('에어컨','on','fallback-race-02',registry)
        print(json.dumps({'providers_dispatched':sent,'commands':len(sent),'fallback_result':result['result']},ensure_ascii=False))
    finally:registry.close()
