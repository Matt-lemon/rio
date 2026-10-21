"""Operator-selected browser transports; never selectable by model arguments."""
import json
import os
from pathlib import Path
import stat
import tempfile
import socket

MODES=('extension','managed','cdp')
STATE=Path.home()/'.local/state/rio/browser-research'
BASE=Path.home()/'.local/share/rio-browser'
SELECTION=Path.home()/'.config/rio-browser/transport.json'
LEGACY_SELECTION=Path.home()/'.config/rio/browser-transport.json'
CDP_PORT=9337
CDP_ENDPOINT=f'http://127.0.0.1:{CDP_PORT}'


def mode_name(value):
    if value not in MODES:raise ValueError('INVALID_BROWSER_TRANSPORT')
    return value


def private_directory(path):
    path.mkdir(parents=True,exist_ok=True,mode=0o700)
    info=path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid!=os.getuid() or info.st_mode&0o077:
        raise ValueError('PRIVATE_BROWSER_DIRECTORY_REQUIRED')
    return path


def read_private_json(path):
    fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
    with os.fdopen(fd,'rb') as stream:
        info=os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid!=os.getuid() or info.st_mode&0o077:
            raise ValueError('PRIVATE_BROWSER_CONFIG_REQUIRED')
        data=stream.read(8193)
    if len(data)>8192:raise ValueError('BROWSER_CONFIG_TOO_LARGE')
    return json.loads(data)


def selected_mode():
    if 'RIO_BROWSER_TRANSPORT' in os.environ:return mode_name(os.environ['RIO_BROWSER_TRANSPORT'])
    path=SELECTION if os.path.lexists(SELECTION) else LEGACY_SELECTION
    if not os.path.lexists(path):return 'extension'
    value=read_private_json(path)
    if not isinstance(value,dict) or set(value)!={'schema_version','transport'} or type(value['schema_version']) is not int or value['schema_version']!=1:
        raise ValueError('INVALID_BROWSER_SELECTION')
    return mode_name(value['transport'])


def profile(mode):
    return BASE/({'extension':'edge-profile','managed':'edge-managed-profile','cdp':'edge-cdp-profile'}[mode_name(mode)])


def endpoint(mode):
    return STATE/({'extension':'browser.sock','managed':'browser-managed.sock','cdp':'browser-cdp.sock'}[mode_name(mode)])


def profile_label(mode):
    return 'rio-dedicated-edge'+('' if mode_name(mode)=='extension' else '-'+mode)


def profile_in_use(mode):
    lock=profile(mode)/'SingletonLock'
    if not os.path.lexists(lock):return False
    if not lock.is_symlink():return True
    try:
        host,pid=os.readlink(lock).rsplit('-',1)
        # Let Chromium itself recover only a dead local process lock. We never
        # delete profile locks or interfere with a live/foreign profile owner.
        return host!=socket.gethostname() or not pid.isdigit() or Path('/proc/'+pid).exists()
    except OSError:return True


def write_selection(mode):
    mode_name(mode);private_directory(SELECTION.parent)
    fd,name=tempfile.mkstemp(prefix='.transport-',dir=SELECTION.parent)
    tmp=Path(name)
    try:
        with os.fdopen(fd,'w') as stream:
            json.dump({'schema_version':1,'transport':mode},stream);stream.write('\n');stream.flush();os.fsync(stream.fileno())
        os.replace(tmp,SELECTION)
    finally:tmp.unlink(missing_ok=True)


def process_start(pid):
    value=Path(f'/proc/{pid}/stat').read_text()
    return value[value.rfind(')')+2:].split()[19]


def listener_inodes(tables):
    listeners=[]
    for table in tables:
        for line in table.splitlines()[1:]:
            fields=line.split();address,port=fields[1].split(':')
            if int(port,16)==CDP_PORT and fields[3]=='0A':
                if address not in {'0100007F','00000000000000000000000001000000'}:
                    raise ValueError('CDP_MUST_BE_LOOPBACK_ONLY')
                listeners.append(fields[9])
    return listeners


def cdp_identity():
    """Require this user's dedicated launch receipt, process and loopback listener.

    No HTTP target discovery, port scans, authentication or browser commands.
    """
    receipt=read_private_json(STATE/'cdp-launch.json')
    if not isinstance(receipt,dict) or set(receipt)!={'pid','start','profile','port'}:
        raise ValueError('USER_CDP_LAUNCH_REQUIRED')
    pid=receipt['pid']
    if type(pid) is not int or pid<2 or receipt['profile']!=str(profile('cdp')) or receipt['port']!=CDP_PORT:
        raise ValueError('USER_CDP_LAUNCH_REQUIRED')
    if Path(f'/proc/{pid}').stat().st_uid!=os.getuid() or process_start(pid)!=receipt['start']:
        raise ValueError('CDP_PROCESS_CHANGED')
    if Path(f'/proc/{pid}/exe').resolve().name not in {'msedge','microsoft-edge'}:
        raise ValueError('DEDICATED_EDGE_REQUIRED')
    lock=profile('cdp')/'SingletonLock'
    if not lock.is_symlink() or int(os.readlink(lock).rsplit('-',1)[1])!=pid:
        raise ValueError('CDP_PROFILE_MISMATCH')
    listeners=listener_inodes([Path(p).read_text() for p in ('/proc/net/tcp','/proc/net/tcp6')])
    owned=set()
    for fd in Path(f'/proc/{pid}/fd').iterdir():
        try:owned.add(os.readlink(fd))
        except OSError:pass
    if not listeners or not all('socket:['+inode+']' in owned for inode in listeners):
        raise ValueError('CDP_LISTENER_NOT_OWNED_BY_PROFILE')
    return receipt


def mcp_arguments(mode,proof):
    mode_name(mode)
    common=['--browser','msedge','--no-webmcp','--snapshot-mode','none',
            '--console-level','error','--codegen','none','--output-dir',str(proof)]
    if mode=='extension':return common+['--extension','--user-data-dir',str(profile(mode)),'--profile-dir-name','Default']
    if mode=='managed':return common+['--sandbox','--user-data-dir',str(profile(mode))]
    return common+['--cdp-endpoint',CDP_ENDPOINT,'--cdp-timeout','10000']
