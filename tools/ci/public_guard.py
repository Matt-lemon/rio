"""Check reachable blobs and metadata without printing private values."""
import argparse
import json
from pathlib import Path
import re
import subprocess

ROOT = Path(__file__).resolve().parents[2]
RULES = {
    'personal_home': re.compile(rb'/' + rb'home/(?!example(?:/|\b)|operator(?:/|\b)|rio(?:/|\b))[^/\s\x00"\x27<>]+'),
    'personal_windows_home': re.compile(rb'(?i)[A-Z]:[\\/]+Users[\\/]+(?!example\b|operator\b)[^\\/\s"\x27]+'),
}
FORBIDDEN = re.compile(r'(^|/)(?:\.env(?:\.(?!example$)[^/]+)?|data|logs|diagnostics|node_modules)(/|$)|\.(?:gguf|onnx|wav|mp3|sqlite3?|db|dump|pem|key|token)$')


def git(*args):
    return subprocess.check_output(['git', '-C', str(ROOT), *args])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--release', action='store_true')
    args = parser.parse_args()
    objects = git('rev-list', '--objects', '--all').splitlines()
    ids = list(dict.fromkeys(line.split(b' ', 1)[0] for line in objects))
    raw = subprocess.run(['git', '-C', str(ROOT), 'cat-file', '--batch'],
                         input=b'\n'.join(ids) + b'\n', stdout=subprocess.PIPE, check=True).stdout
    offset = blobs = 0
    findings = {}
    for _ in ids:
        end = raw.index(b'\n', offset)
        header = raw[offset:end].split()
        size = int(header[2])
        body = raw[end + 1:end + 1 + size]
        offset = end + 2 + size
        if header[1] not in (b'blob', b'commit', b'tag'):
            continue
        blobs += header[1] == b'blob'
        for name, rule in RULES.items():
            if rule.search(body):
                findings[name] = findings.get(name, 0) + 1
    paths = [line.partition(b' ')[2].decode('utf-8', 'replace') for line in objects]
    findings['forbidden_paths'] = sum(bool(FORBIDDEN.search(p)) for p in paths if p)
    findings = {k: v for k, v in findings.items() if v}
    license_pending = not (ROOT / 'LICENSE').is_file()
    print(json.dumps({'reachable_blobs': blobs, 'findings': findings,
                      'license_selection_pending': license_pending,
                      'mode': 'release' if args.release else 'candidate'}))
    return int(bool(findings) or (args.release and license_pending))


if __name__ == '__main__':
    raise SystemExit(main())
