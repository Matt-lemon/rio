from __future__ import annotations

from dataclasses import dataclass, field, asdict
import hashlib
import json
import re
from pathlib import Path


TEXT = {'type': 'string', 'minLength': 1, 'maxLength': 160}
SENSITIVE = re.compile(r'비밀번호|인증번호|Bearer\s|access.?token|password|sk-[A-Za-z0-9]|api[_ -]?key', re.I)


def enum(*values):
    return {'type': 'string', 'enum': list(values)}


def spec(tool_name, description, effect, **properties):
    return {'name': tool_name, 'description': description, 'effect': effect,
            'parameters': {'type': 'object', 'properties': properties,
                           'required': list(properties), 'additionalProperties': False}}


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def fingerprint(value):
    return hashlib.sha256(encoded(value).encode()).hexdigest()


def source_revision():
    root = Path(__file__).resolve().parents[2]
    paths = sorted((root/'core').rglob('*.py')) + [root/p for p in
            ('main.py', 'rio_core.py', 'rio_voice.py', 'rio_agent.py', 'rio_memory_context.py',
             'rio_dubbing_client.py', 'rio_dubbing_tools.py', 'web_search.py')]
    return fingerprint({str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest() for p in paths})


def safe_text(value, limit=2000):
    text = str(value)
    if SENSITIVE.search(text):
        return '[민감 정보 생략]'
    return ''.join(c for c in text if c.isprintable() or c == '\n')[:limit]


def validate(schema, value, depth=0):
    """Deliberately small local contract dialect; unsupported schemas fail closed."""
    if depth > 12 or not isinstance(schema, dict):
        return False
    kind = schema.get('type')
    if kind == 'object':
        if not isinstance(value, dict):
            return False
        props = schema.get('properties', {})
        return (set(schema.get('required', [])) <= set(value)
                and (schema.get('additionalProperties') is not False or set(value) <= set(props))
                and all(k in props and validate(props[k], v, depth + 1) for k, v in value.items()))
    if kind == 'string':
        if not isinstance(value, str) or not schema.get('minLength', 0) <= len(value) <= schema.get('maxLength', 4096):
            return False
        if any(ord(c) < 32 and c not in '\n\t' for c in value):
            return False
    elif kind in {'integer', 'number'}:
        if type(value) not in ({int} if kind == 'integer' else {int, float}):
            return False
        if not schema.get('minimum', -1e15) <= value <= schema.get('maximum', 1e15):
            return False
    elif kind == 'boolean':
        if type(value) is not bool:
            return False
    else:
        return False
    return 'enum' not in schema or value in schema['enum']


@dataclass
class ToolResult:
    status: str
    message: str
    evidence: dict = field(default_factory=dict)
    request_id: str | None = None
    confirmation: str = 'NONE'
    code: str | None = None

    def public(self):
        return asdict(self)

    @property
    def completed(self):
        return self.status in {'SUCCEEDED', 'OBSERVED', 'RETURNED', 'SCHEDULED'}

    @classmethod
    def restore(cls, value):
        return cls(**{k: v for k, v in value.items() if k in cls.__dataclass_fields__})
