"""Shared public-page scope for Rio's trusted browser commands.

This is an application-level URL filter, not a DNS or browser-wide sandbox.
"""
from __future__ import annotations

import ipaddress
import re
from urllib.parse import parse_qsl, urlparse

SENSITIVE_KEYS = re.compile(
    r'^(?:access_token|refresh_token|id_token|token|auth|authorization|'
    r'api[_-]?key|secret|password|passwd|session(?:_id)?|code)$', re.I)


def public_url(value):
    if not isinstance(value, str) or not 1 <= len(value) <= 2048:
        return False
    if re.search(r'[\\\x00-\x20\x7f]', value):
        return False
    try:
        parsed = urlparse(value)
        host = (parsed.hostname or '').lower()
        if (parsed.scheme != 'https' or parsed.username or parsed.password or
                parsed.port not in (None, 443) or not host or host.endswith('.')):
            return False
        if '.' not in host or host.endswith(('.local', '.localhost', '.internal')):
            return False
        if host in {'home.google.com', 'accounts.google.com',
                    'my.smartthings.com', 'account.samsung.com'}:
            return False
        if re.search(r'/(?:login|signin|logout|checkout|payment|accounts)(?:/|$)',
                     parsed.path, re.I):
            return False
        if any(SENSITIVE_KEYS.match(key) for key, _ in
               parse_qsl(parsed.query, keep_blank_values=True)):
            return False
        if parsed.fragment and SENSITIVE_KEYS.match(parsed.fragment.split('=', 1)[0]):
            return False
        try:
            return ipaddress.ip_address(host).is_global
        except ValueError:
            return True
    except (TypeError, ValueError):
        return False


def direct_url(command):
    """Return the URL only for an explicit, unambiguous request to open it."""
    if not isinstance(command, str):
        return None
    text = re.sub(r'^리오(?:야)?[,，]?\s*', '', command.strip())
    patterns = (
        r'(https://\S+?)(?:\s+(?:(?:이\s*)?(?:주소|링크|페이지)(?:를|을)?\s*)?열어\s*(?:줘|주세요))?',
        r'(?:이\s*)?(?:주소|링크|페이지)(?:를|을)?\s*열어\s*(?:줘|주세요)\s*[:：]?\s*(https://\S+)',
    )
    for pattern in patterns:
        match = re.fullmatch(pattern, text)
        if match:
            return match.group(1)
    return None
