"""Offline checks of the actual Nix Python versions and effective CA bundle."""
import hashlib
from importlib.metadata import version
import json
from pathlib import Path
import ssl

import certifi
from packaging.version import Version

FLOORS = {'anyio': '4.14.2', 'certifi': '2026.10.19', 'urllib3': '2.8.0'}
REVOKED_ROOT_SHA256 = '86a1ecba089c4a8d3bbe2734c612ba341d813e043cf9e8a862cd5c57a36bbe6b'


def main():
    versions = {name: version(name) for name in FLOORS}
    if not all(Version(versions[name]) >= Version(floor) for name, floor in FLOORS.items()):
        raise ValueError('VULNERABLE_PACKAGE_VERSION')
    bundle = Path(certifi.where())
    trusted = ssl.create_default_context(cafile=str(bundle)).get_ca_certs(binary_form=True)
    standard = ssl.create_default_context().get_ca_certs(binary_form=True)
    if not trusted or not standard or any(
            hashlib.sha256(cert).hexdigest() == REVOKED_ROOT_SHA256
            for cert in [*trusted, *standard]):
        raise ValueError('REVOKED_ROOT_STILL_TRUSTED')
    print(json.dumps({'status': 'PASS', 'versions': versions, 'trusted_ca_count': len(trusted),
                      'stdlib_ca_count': len(standard),
                      'ca_bundle_sha256': hashlib.sha256(bundle.read_bytes()).hexdigest(),
                      'revoked_root_trusted': False}, sort_keys=True))


if __name__ == '__main__':
    main()
