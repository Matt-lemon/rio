#!/usr/bin/env python3
"""Test a hash-pinned gateway snapshot without altering its owner's checkout.

This is a candidate-source test, never a release approval or an installer.
Only a source file is copied; no gateway state, tokens or operating DBs.
"""
import argparse
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--sha256', required=True)
    parser.add_argument('--output', type=Path, required=True)
    args, pytest_args = parser.parse_known_args()
    os.umask(0o077)
    repo = Path(__file__).resolve().parents[1]
    source = args.source.resolve(strict=True)
    data = source.read_bytes()
    hashed = hashlib.sha256(data).hexdigest()
    if hashed != args.sha256:
        parser.error('GATEWAY_SOURCE_HASH_CHANGED')
    output = args.output.resolve()
    output.mkdir(mode=0o700, parents=True, exist_ok=False)
    head = subprocess.check_output(['git', '-C', str(repo), 'rev-parse', 'HEAD'], text=True).strip()
    archive = subprocess.check_output(['git', '-C', str(repo), 'archive', head])
    checkout = output/'checkout'
    checkout.mkdir(mode=0o700)
    with tarfile.open(fileobj=io.BytesIO(archive)) as stream:
        stream.extractall(checkout, filter='data')
    # These audit-owned additions may still be under review before committing.
    additions = {}
    for relative in ('tools/audit_fallback_gateway.py', 'tools/audit_fallback_pytest.py',
                     'tests/test_fallback_writer_transition.py',
                     'core/home/example', 'tests/test_smartthings_result_reconciliation.py'):
        path = repo/relative
        if path.is_file():
            body = path.read_bytes()
            target = checkout/relative
            target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            target.write_bytes(body)
            additions[relative] = hashlib.sha256(body).hexdigest()
    target = checkout/'integrations/google_home/nas/gateway.py'
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    target.write_bytes(data)
    tracked = subprocess.run(['git', '-C', str(source.parent), 'ls-files', '--error-unmatch',
                              source.name], capture_output=True).returncode == 0
    receipt = {'audit_head': head, 'audit_working_files_sha256': additions,
               'gateway_source': str(source), 'gateway_sha256': hashed,
               'gateway_tracked': tracked, 'release_approved': False,
               'operating_changes': False, 'appliance_commands': 0,
               'scope': 'synthetic transports and temporary state only'}
    (output/'manifest.json').write_text(json.dumps(receipt, indent=2)+'\n')
    command = [sys.executable, 'tools/audit_fallback_pytest.py', *(pytest_args or [
        '-q', 'tests/test_home_device_controls.py', 'tests/test_fallback_writer_transition.py'])]
    with (output/'pytest.txt').open('w') as log:
        result = subprocess.run(command, cwd=checkout, stdout=log, stderr=subprocess.STDOUT)
    receipt['test_exit_code'] = result.returncode
    receipt['gateway_unchanged'] = hashlib.sha256(source.read_bytes()).hexdigest() == hashed
    (output/'manifest.json').write_text(json.dumps(receipt, indent=2)+'\n')
    print(json.dumps({'test_exit_code': result.returncode, 'gateway_tracked': tracked,
                      'gateway_unchanged': receipt['gateway_unchanged'], 'output': str(output)}))
    return result.returncode or (0 if receipt['gateway_unchanged'] else 2)


if __name__ == '__main__':
    raise SystemExit(main())
