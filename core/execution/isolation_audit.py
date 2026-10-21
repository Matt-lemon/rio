"""Fixed read-only OS check. Never sends a Docker API request."""
import errno
import os
import socket


def audit():
    result={'uid':os.getuid(),'gid':os.getgid(),'groups':os.getgroups(),
            'docker_file_access':os.access('/var/run/docker.sock',os.R_OK|os.W_OK),
            'docker_connect':'UNVERIFIED','docker_api_requests':0}
    try:
        with socket.socket(socket.AF_UNIX,socket.SOCK_STREAM) as connection:
            connection.settimeout(1);connection.connect('/var/run/docker.sock')
        result['docker_connect']='ALLOWED'
    except OSError as error:
        result['docker_connect']='DENIED' if error.errno in {errno.EACCES,errno.EPERM} else 'UNAVAILABLE'
        result['errno']=error.errno
    return result


if __name__=='__main__':
    import json
    print(json.dumps(audit()))
