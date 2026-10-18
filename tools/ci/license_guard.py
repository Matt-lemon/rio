"""Fail-closed Python license and vendored-artifact gate (stdlib only).

Run in an isolated CI venv after installing the project, subproject and test
requirements. This is an inventory/policy check, not legal advice or SBOM.
"""
from __future__ import annotations

import argparse
from importlib import metadata
import json
from pathlib import Path
import re
import subprocess
import sys


BINARY_EXTENSIONS = frozenset({
    '.exe', '.dll', '.so', '.dylib', '.a', '.o', '.class', '.jar', '.aar',
    '.apk', '.ipa', '.wasm', '.zip', '.tar', '.gz', '.7z', '.rar',
    '.gguf', '.onnx', '.safetensors', '.pt', '.pth', '.pkl', '.ckpt',
    '.bin', '.pyc', '.whl', '.egg', '.mp3', '.mp4', '.wav', '.flac',
})
MEDIA_EXTENSIONS = frozenset({'.png', '.jpg', '.jpeg', '.gif', '.webp', '.svg', '.ico', '.bmp', '.tiff'})
BINARY_MAGIC = (b'\x7fELF', b'MZ', b'PK\x03\x04', b'\x00asm')
LICENSE_ALIASES = {
    'MIT LICENSE': 'MIT', 'MIT': 'MIT', 'APACHE SOFTWARE LICENSE': 'Apache-2.0',
    'APACHE LICENSE 2.0': 'Apache-2.0', 'APACHE-2.0': 'Apache-2.0',
    'BSD LICENSE': 'BSD-3-Clause', 'BSD-3-CLAUSE': 'BSD-3-Clause',
    'BSD-2-CLAUSE': 'BSD-2-Clause', 'ISC LICENSE (ISCL)': 'ISC',
    'ISC LICENSE': 'ISC', 'ISC': 'ISC', 'MOZILLA PUBLIC LICENSE 2.0 (MPL 2.0)': 'MPL-2.0',
    'MPL-2.0': 'MPL-2.0', 'PYTHON SOFTWARE FOUNDATION LICENSE': 'PSF-2.0',
    'PSF LICENSE': 'PSF-2.0', 'THE UNLICENSE (UNLICENSE)': 'Unlicense',
}
CLASSIFIER_ALIASES = {
    'MIT License': 'MIT', 'Apache Software License': 'Apache-2.0',
    'ISC License (ISCL)': 'ISC',
    'Mozilla Public License 2.0 (MPL 2.0)': 'MPL-2.0',
    'Python Software Foundation License': 'PSF-2.0',
    'The Unlicense (Unlicense)': 'Unlicense',
    'GNU General Public License v3 (GPLv3)': 'GPL-3.0-only',
    'GNU Affero General Public License v3': 'AGPL-3.0-only',
}
IDENTIFIER = re.compile(r'[A-Za-z0-9][A-Za-z0-9.+-]*\Z')
NAME = re.compile(r'[-_.]+')


def normalize_name(value: str) -> str:
    return NAME.sub('-', value).lower()


def license_ids(expression: str, allowed: set[str]) -> set[str] | None:
    """Accept only plain SPDX identifiers combined with AND/OR; no exceptions.

    Every component must be individually allowable. This is conservative even
    with dual licenses: a GPL OR MIT expression cannot be auto-approved.
    """
    if not isinstance(expression, str) or not expression.strip() or len(expression) > 500:
        return None
    terms = re.split(r'\s+(?:AND|OR)\s+|[()]', expression.strip(), flags=re.I)
    tokens = [value.strip() for value in terms if value.strip()]
    if not tokens or any(not IDENTIFIER.fullmatch(token) for token in tokens):
        return None
    return set(tokens) if all(token in allowed for token in tokens) else None


def distribution_licenses(dist, allowed: set[str]) -> set[str] | None:
    info = dist.metadata
    spdx = info.get('License-Expression')
    if spdx:
        return license_ids(spdx, allowed)
    raw = info.get('License', '').strip()
    if raw and len(raw) < 200:
        normalized = LICENSE_ALIASES.get(raw.upper(), raw)
        ids = license_ids(normalized, allowed)
        if ids:
            return ids
        # Unknown explicit license must not be silently overridden by a generic
        # classifier such as "OSI Approved".
        if raw != 'UNKNOWN':
            return None
    matched = set()
    for value in info.get_all('Classifier', []):
        prefix = 'License :: OSI Approved :: '
        if value.startswith(prefix):
            name = value[len(prefix):]
            ident = CLASSIFIER_ALIASES.get(name)
            if ident is None or ident not in allowed:
                return None
            matched.add(ident)
    return matched or None


def guard_dependencies(policy: dict, distributions=None) -> tuple[list[str], list[dict]]:
    allowed = set(policy['allowed_licenses'])
    ignored = {normalize_name(item) for item in policy['ignored_build_tools']}
    exceptions = policy['reviewed_exceptions']
    errors, audit = [], []
    all_distributions = distributions if distributions is not None else metadata.distributions()
    observed = set()
    for dist in sorted(all_distributions, key=lambda d: normalize_name(d.metadata.get('Name', ''))):
        name = dist.metadata.get('Name')
        if not name:
            errors.append('UNKNOWN_DISTRIBUTION_NAME')
            continue
        key = normalize_name(name)
        if key in ignored or key == 'rio-localization':
            continue
        if key in observed:
            errors.append('DUPLICATE_DISTRIBUTION: ' + key)
            continue
        observed.add(key)
        version = str(dist.version)
        found = distribution_licenses(dist, allowed)
        exception = exceptions.get(key)
        if not found:
            if (isinstance(exception, dict) and set(exception) == {'version', 'license', 'reason'}
                    and exception['version'] == version
                    and exception['license'] in allowed
                    and isinstance(exception['reason'], str) and len(exception['reason'].strip()) >= 15):
                found = {exception['license']}
            else:
                errors.append(f'UNAPPROVED_LICENSE: {name}=={version}')
        audit.append({'name': name, 'version': version, 'licenses': sorted(found or [])})
    if not observed:
        errors.append('NO_INSTALLED_DEPENDENCIES')
    return errors, audit


def guard_artifacts(root: Path, policy: dict) -> tuple[list[str], list[str]]:
    tracked = subprocess.run(['git', '-C', str(root), 'ls-files', '-z'],
                             check=True, stdout=subprocess.PIPE).stdout
    errors, notices = [], []
    approved = policy['reviewed_artifacts']
    pending = set(policy['pending_human_review_assets'])
    for raw in tracked.split(b'\0'):
        if not raw:
            continue
        rel = raw.decode('utf-8')
        file = root / rel
        if not file.is_file() or file.is_symlink():
            errors.append('MISSING_OR_LINKED_FILE: ' + rel)
            continue
        with file.open('rb') as source:
            header = source.read(8)
        is_media = file.suffix.lower() in MEDIA_EXTENSIONS
        binary = file.suffix.lower() in BINARY_EXTENSIONS or any(header.startswith(x) for x in BINARY_MAGIC)
        if binary or is_media:
            justification = approved.get(rel)
            if (is_media and rel in pending and not justification):
                print('::warning title=Unreviewed legacy media::'+rel+' requires provenance/license review.')
                notices.append('PENDING_MEDIA_REVIEW:'+rel)
                continue
            if (not isinstance(justification, dict) or set(justification) != {'license', 'source', 'notice'}
                    or justification['license'] not in policy['allowed_licenses']
                    or not isinstance(justification['source'], str) or not justification['source'].startswith('https://')
                    or not isinstance(justification['notice'], str)
                    or not (root / justification['notice']).is_file()):
                errors.append(('UNREVIEWED_MEDIA: ' if is_media else 'UNREVIEWED_BINARY: ') + rel)
            else:
                notices.append(rel)
    for name in pending:
        if not isinstance(name,str) or name not in {x.decode('utf-8') for x in tracked.split(b'\0') if x}:
            errors.append('STALE_PENDING_MEDIA_POLICY: '+str(name))
    return errors, notices


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--policy', default='.github/license-policy.json')
    parser.add_argument('--source-only', action='store_true')
    parser.add_argument('--deps-only', action='store_true')
    parser.add_argument('--output', default='')
    args = parser.parse_args(argv)
    if args.source_only and args.deps_only:
        parser.error('Choose at most one scan mode')
    root = Path(__file__).resolve().parents[2]
    policy = json.loads((root / args.policy).read_text(encoding='utf-8'))
    expected = {'schema_version', 'allowed_licenses', 'ignored_build_tools',
                'reviewed_exceptions', 'reviewed_artifacts', 'pending_human_review_assets'}
    if (not isinstance(policy, dict) or set(policy) != expected
            or type(policy['schema_version']) is not int or policy['schema_version'] != 1
            or not isinstance(policy['allowed_licenses'], list)
            or any(not isinstance(v, str) for v in policy['allowed_licenses'])
            or not isinstance(policy['ignored_build_tools'], list)
            or not isinstance(policy['reviewed_exceptions'], dict)
            or not isinstance(policy['reviewed_artifacts'], dict)
            or not isinstance(policy['pending_human_review_assets'], list)
            or any(not isinstance(x,str) for x in policy['pending_human_review_assets'])
            or len(set(policy['pending_human_review_assets'])) != len(policy['pending_human_review_assets'])):
        print('INVALID_LICENSE_POLICY', file=sys.stderr)
        return 2
    errors, result = [], {}
    if not args.deps_only:
        source_errors, artifacts = guard_artifacts(root, policy)
        errors.extend(source_errors)
        result['reviewed_artifacts'] = artifacts
        if not (root / 'LICENSE').is_file() and not (root / 'LICENCE').is_file():
            print('::warning title=Root repository license::No root LICENSE; source distribution rights must be reviewed.')
            result['root_license'] = 'MISSING_REVIEW_REQUIRED'
        else:
            result['root_license'] = 'PRESENT_REVIEW_TERMS_MANUALLY'
    if not args.source_only:
        dep_errors, dependencies = guard_dependencies(policy)
        errors.extend(dep_errors)
        result['dependencies'] = dependencies
    result['errors'] = errors
    if args.output:
        out = Path(args.output)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    pending_count = sum(item.startswith('PENDING_MEDIA_REVIEW:') for item in result.get('reviewed_artifacts', []))
    print('LICENSE_GATE', 'FAIL' if errors else 'PASS',
          'packages', len(result.get('dependencies', [])),
          'reviewed_artifacts', len(result.get('reviewed_artifacts', []))-pending_count,
          'pending_media_review', pending_count, 'errors', len(errors))
    for entry in errors:
        print('LICENSE_ERROR:', entry)
    return 1 if errors else 0


if __name__ == '__main__':
    raise SystemExit(main())
