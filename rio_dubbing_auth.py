"""Shared bearer configuration; secrets may live outside the checkout."""
import os
from pathlib import Path


def dubbing_token() -> str:
    token = os.getenv('RIO_DUBBING_TOKEN', '')
    if token:
        return token
    filename = os.getenv('RIO_DUBBING_TOKEN_FILE', '')
    if not filename:
        return ''
    path = Path(filename).expanduser()
    if path.stat().st_size > 4096:
        raise ValueError('dubbing token file is too large')
    token = path.read_text(encoding='utf-8').strip()
    if not token or any(c.isspace() for c in token):
        raise ValueError('invalid dubbing token file')
    return token
