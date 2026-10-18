#!/usr/bin/env python3
"""Run audit pytest with a synthetic HOME, no IP network or external CLI.

Python children are allowed for inspected crash/IPC tests. This is a test guard,
not an OS sandbox; child programs must also use only synthetic providers.
"""
import os,sys,tempfile,socket,subprocess
from pathlib import Path
os.umask(0o077)
root=Path(tempfile.mkdtemp(prefix='rio-fallback-offline-'))
for key in list(os.environ):
    if key.startswith(('RIO_','SMARTTHINGS_','XDG_')):os.environ.pop(key)
os.environ.update(HOME=str(root),XDG_CONFIG_HOME=str(root/'config'),XDG_STATE_HOME=str(root/'state'),XDG_DATA_HOME=str(root/'data'),PYTEST_DISABLE_PLUGIN_AUTOLOAD='1')
connect=socket.socket.connect
connect_ex=socket.socket.connect_ex
popen=subprocess.Popen

def guarded_connect(sock,address):
    if sock.family in (socket.AF_INET,socket.AF_INET6):raise RuntimeError('AUDIT_NETWORK_FORBIDDEN')
    if sock.family==socket.AF_UNIX and not str(address).startswith(('/tmp/','/private/tmp/')):raise RuntimeError('AUDIT_OPERATING_SOCKET_FORBIDDEN')
    return connect(sock,address)
def guarded_connect_ex(sock,address):
    if sock.family in (socket.AF_INET,socket.AF_INET6):raise RuntimeError('AUDIT_NETWORK_FORBIDDEN')
    if sock.family==socket.AF_UNIX and not str(address).startswith(('/tmp/','/private/tmp/')):raise RuntimeError('AUDIT_OPERATING_SOCKET_FORBIDDEN')
    return connect_ex(sock,address)
def guarded_popen(args,*a,**kw):
    if isinstance(args,(str,bytes)) or Path(str(args[0])).resolve()!=Path(sys.executable).resolve():raise RuntimeError('AUDIT_EXTERNAL_PROCESS_FORBIDDEN')
    return popen(args,*a,**kw)
socket.socket.connect=guarded_connect
socket.socket.connect_ex=guarded_connect_ex
subprocess.Popen=guarded_popen
sys.path.insert(0,os.getcwd())
sys.path.insert(0,str(Path(os.getcwd())/'services/rio-tools'))
import pytest
raise SystemExit(pytest.main(sys.argv[1:]))
