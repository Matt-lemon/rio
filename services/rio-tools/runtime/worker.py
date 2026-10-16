"""Trusted, fixed container entrypoint. Never installed or run inside Rio Core."""
import json
import os
from pathlib import Path
import subprocess
import sys
import resource

LIMIT=1024*1024
COMMANDS={'sort':('/usr/bin/sort',{'-n','-r','-u'}),
          'uniq':('/usr/bin/uniq',{'-c','-d','-u'}),
          'wc':('/usr/bin/wc',{'-l','-w','-c'})}


def main():
    request=json.loads(sys.stdin.buffer.read(2*LIMIT+1))
    root=Path('/work')
    if request.get('mode')=='pipeline':
        steps=request['steps']
        if not isinstance(steps,list) or not 1<=len(steps)<=4:
            raise ValueError('pipeline')
        data=request['text'].encode()
        for i,step in enumerate(steps):
            if set(step)!={'command','args'} or step['command'] not in COMMANDS:
                raise ValueError('command')
            executable,allowed=COMMANDS[step['command']]
            args=step['args']
            if not isinstance(args,list) or len(args)>3 or any(a not in allowed for a in args):
                raise ValueError('args')
            target=root/('out-'+str(i))
            with target.open('wb') as output, (root/'stderr').open('wb') as errors:
                child=subprocess.run([executable,*args],input=data,stdout=output,stderr=errors,
                                     cwd=root,env={'PATH':'/usr/bin:/bin','LANG':'C.UTF-8'})
            if child.returncode or target.stat().st_size>LIMIT:
                raise ValueError('output')
            data=target.read_bytes()
        result={'text':data.decode('utf8')}
    else:
        if set(request)!={'mode','kind','source','input'} or request['kind'] not in {'python','bash'}:
            raise ValueError('request')
        name='tool.py' if request['kind']=='python' else 'tool.sh'
        target=root/name
        target.write_text(request['source'])
        command=(['/usr/local/bin/python','-I','-B',str(target)] if request['kind']=='python'
                 else ['/bin/bash','--noprofile','--norc',str(target)])
        stdout=root/'stdout'
        with stdout.open('wb') as output, (root/'stderr').open('wb') as errors:
            child=subprocess.run(command,input=json.dumps(request['input']).encode(),stdout=output,stderr=errors,
                                 cwd=root,env={'PATH':'/usr/local/bin:/usr/bin:/bin','HOME':'/work',
                                               'TMPDIR':'/work','LANG':'C.UTF-8','PYTHONDONTWRITEBYTECODE':'1'})
        if child.returncode or stdout.stat().st_size>LIMIT:
            raise ValueError('child')
        result=json.loads(stdout.read_bytes())
    usage=resource.getrusage(resource.RUSAGE_CHILDREN)
    encoded=json.dumps({'ok':True,'result':result,'usage':{
        'child_cpu_seconds':round(usage.ru_utime+usage.ru_stime,6),
        'child_max_rss_kib':usage.ru_maxrss,
        'output_bytes':len(json.dumps(result).encode())}},separators=(',',':'),allow_nan=False).encode()
    if len(encoded)>LIMIT:
        raise ValueError('output')
    sys.stdout.buffer.write(encoded)


try:
    main()
except BaseException:
    # No untrusted code/inputs/tracebacks in host logs.
    sys.stdout.write('{"ok":false,"code":"CONTAINER_TOOL_FAILED"}')
    sys.exit(1)
