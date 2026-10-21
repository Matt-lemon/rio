"""Bounded, reference-free contracts shared with the trusted tool service."""
from __future__ import annotations
import hashlib
import json
import re

LOW_RISK = frozenset({'input.read', 'workspace.compute', 'workspace.output'})
PERMISSIONS = LOW_RISK | {'device.control', 'external.read', 'external.send',
                         'original.write', 'original.delete', 'system.change', 'access.expand'}


class ToolError(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False).encode()


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def schema_check(schema):
    from jsonschema import Draft202012Validator
    if not isinstance(schema, dict) or len(canonical(schema)) > 32768:
        raise ToolError('SCHEMA_LIMIT')
    def walk(value, depth=0):
        if depth > 12:
            raise ToolError('SCHEMA_DEPTH')
        if isinstance(value, dict):
            if any(k in value for k in ('$ref', '$dynamicRef', '$id', 'pattern', 'patternProperties', 'uniqueItems')):
                raise ToolError('SCHEMA_REFERENCE_FORBIDDEN')
            for v in value.values():
                walk(v, depth+1)
        elif isinstance(value, list):
            for v in value:
                walk(v, depth+1)
    walk(schema)
    try:
        Draft202012Validator.check_schema(schema)
    except Exception:
        raise ToolError('INVALID_SCHEMA') from None


def validate_value(schema, value, code='INVALID_INPUT'):
    from jsonschema import Draft202012Validator
    if len(canonical(value)) > 2*1024*1024:
        raise ToolError('PAYLOAD_LIMIT')
    try:
        Draft202012Validator(schema).validate(value)
    except Exception:
        raise ToolError(code) from None


def validate_package(package):
    if not isinstance(package, dict) or set(package) != {'contract', 'files'}:
        raise ToolError('PACKAGE_FIELDS')
    c = package['contract']
    expected = {'name', 'description', 'version', 'input_schema', 'output_schema',
                'kind', 'entrypoint', 'image_id', 'permissions', 'capability'}
    if not isinstance(c, dict) or set(c) != expected:
        raise ToolError('CONTRACT_FIELDS')
    if not isinstance(c['name'], str) or not re.fullmatch(r'[a-z][a-z0-9_]{2,63}', c['name']):
        raise ToolError('INVALID_NAME')
    if not isinstance(c['version'], str) or not re.fullmatch(r'\d{1,4}\.\d{1,4}\.\d{1,4}', c['version']):
        raise ToolError('INVALID_VERSION')
    if not isinstance(c['description'], str) or not 1 <= len(c['description']) <= 1000:
        raise ToolError('DESCRIPTION_LIMIT')
    if c['kind'] not in {'python', 'bash'}:
        raise ToolError('GENERATED_KIND_FORBIDDEN')
    if c['entrypoint'] != ('tool.py' if c['kind'] == 'python' else 'tool.sh'):
        raise ToolError('ENTRYPOINT_NOT_FIXED')
    if not isinstance(c['image_id'], str) or not re.fullmatch(r'sha256:[0-9a-f]{64}', c['image_id']):
        raise ToolError('IMAGE_NOT_PINNED')
    if not isinstance(c['capability'], str) or not re.fullmatch(r'[a-z][a-z0-9_.]{1,63}', c['capability']):
        raise ToolError('INVALID_CAPABILITY')
    permissions = c['permissions']
    if (not isinstance(permissions, list) or any(not isinstance(p, str) or p not in PERMISSIONS for p in permissions)
            or len(set(permissions)) != len(permissions)):
        raise ToolError('INVALID_PERMISSIONS')
    for key in ('input_schema', 'output_schema'):
        schema_check(c[key])
    if set(package['files']) != {c['entrypoint']}:
        raise ToolError('PACKAGE_PATH_FORBIDDEN')
    code = package['files'][c['entrypoint']]
    if not isinstance(code, str) or not 1 <= len(code.encode()) <= 128*1024 or '\0' in code:
        raise ToolError('CODE_LIMIT')
    return c
