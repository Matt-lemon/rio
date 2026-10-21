#!/usr/bin/env python3
"""Start the existing approved Registry with its explicit private configuration."""
import json
import os
from pathlib import Path
import re
import stat
import sys

PROJECT=Path(__file__).resolve().parents[3]
CONFIG=Path.home()/'.config/rio/tools-validation-20261014/connection.json'


def main():
    info=CONFIG.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid!=os.getuid() or info.st_mode&0o077 or info.st_size>8192:
        raise ValueError('PRIVATE_REGISTRY_CONFIG_REQUIRED')
    value=json.loads(CONFIG.read_text())
    state=Path.home()/'.local/state/rio/tools-llm-20261014'
    if value['state']!=str(state) or value['socket']!=str(state/'service.sock'):
        raise ValueError('REGISTRY_STATE_CHANGED')
    if not re.fullmatch(r'sha256:[0-9a-f]{64}',value['image_id']):
        raise ValueError('IMAGE_NOT_PINNED')
    # Keep the approved Builder configuration; starting this service never builds.
    environment={**os.environ,'PYTHONPATH':str(PROJECT)+':'+str(PROJECT/'services/rio-tools'),
                 'PYTHONDONTWRITEBYTECODE':'1','RIO_TOOLS_LLM_BASE_URL':value['batch_url'],
                 'RIO_TOOLS_LLM_MODEL':value['model']}
    os.chdir(PROJECT)
    os.execve('/usr/bin/python3',['/usr/bin/python3','-m','rio_tools.service','--state',str(state),
              '--image-id',value['image_id'],'--use-shared-model','serve'],environment)


if __name__=='__main__':main()
