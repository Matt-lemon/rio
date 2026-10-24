"""Check public preview lock/inventory versions against OSV; fail closed.

No installed/private package discovery, credentials or arbitrary API endpoints.
The query covers declared Python packages, not the native/container closure.
"""
import json
from pathlib import Path
import re
import urllib.request

ROOT = Path(__file__).resolve().parents[2]
ENDPOINT = 'https://api.osv.dev/v1/querybatch'


def packages(root=ROOT):
    pairs = set()
    for name in ('requirements-preview.lock', 'services/rio-tools/requirements-preview.lock'):
        for line in (root / name).read_text().splitlines():
            if line.strip() and not line.startswith('#'):
                requirement = line.split()[0]
                package, version = requirement.split('==')
                pairs.add((package, version))
    inventory = root / 'docs/release/audit/nix-dependency-license-inventory.json'
    for item in json.loads(inventory.read_text())['dependencies']:
        pairs.add((item['name'], item['version']))
    if not 1 <= len(pairs) <= 200 or any(
            not re.fullmatch(r'[A-Za-z0-9_.-]{1,100}', name) or
            not re.fullmatch(r'[A-Za-z0-9_.+!-]{1,100}', version)
            for name, version in pairs):
        raise ValueError('INVALID_PUBLIC_PACKAGE_SET')
    return sorted(pairs)


def check(pairs, opener=urllib.request.urlopen):
    body = {'queries': [{'package': {'name': name, 'ecosystem': 'PyPI'},
                         'version': version} for name, version in pairs]}
    request = urllib.request.Request(ENDPOINT, data=json.dumps(body).encode(),
                                     headers={'Content-Type': 'application/json'})
    with opener(request, timeout=25) as response:
        raw = response.read(2 * 1024 * 1024 + 1)
    if len(raw) > 2 * 1024 * 1024:
        raise ValueError('ADVISORY_RESPONSE_LIMIT')
    rows = json.loads(raw)['results']
    if not isinstance(rows, list) or len(rows) != len(pairs):
        raise ValueError('INCOMPLETE_ADVISORY_RESPONSE')
    matches = []
    for (name, version), row in zip(pairs, rows):
        if not isinstance(row, dict) or row.get('error'):
            raise ValueError('INVALID_ADVISORY_RESPONSE')
        if row.get('vulns'):
            matches.append({'name': name, 'version': version,
                            'advisories': [v['id'] for v in row['vulns']]})
    return {'status': 'FAIL' if matches else 'PASS', 'package_pairs': len(pairs),
            'matches': matches, 'scope': 'declared Python versions only'}


def main():
    try:
        result = check(packages())
    except Exception as error:
        print(json.dumps({'status': 'UNVERIFIED', 'error_type': type(error).__name__}))
        return 1
    print(json.dumps(result, sort_keys=True))
    return int(result['status'] != 'PASS')


if __name__ == '__main__':
    raise SystemExit(main())
