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

# Explicit, stable entry points only. These are not a general search index.
# All other names remain subject to the existing browser/search selection flow.
KNOWN_SITES = {
    'gpt': 'https://chatgpt.com/',
    'chatgpt': 'https://chatgpt.com/',
    '챗지피티': 'https://chatgpt.com/',
    '유튜브': 'https://www.youtube.com/',
    'youtube': 'https://www.youtube.com/',
    '깃허브': 'https://github.com/',
    'github': 'https://github.com/',
}


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


def known_site_url(command):
    """Map only an explicit request to open a known site's *homepage*.

    The user must identify the site and use an opening verb; ambiguous names,
    search requests and compound instructions do not become navigation.
    """
    if not isinstance(command, str):
        return None
    text = re.sub(r'^리오(?:야)?[,，]?\s*', '', command.strip()).rstrip('.!。')
    name = '|'.join(re.escape(name) for name in sorted(KNOWN_SITES, key=len, reverse=True))
    match = re.fullmatch(
        rf'(?:브라우저(?:에서|로)?\s*)?(?P<site>{name})\s*'
        r'(?:공식\s*)?(?:(?:웹\s*)?사이트|홈페이지|페이지)?(?:를|을)?\s*'
        r'(?:하나\s*)?(?:찾아서\s*(?:그\s*페이지(?:를|을)?\s*)?)?'
        r'(?:열어\s*(?:줘|주세요|봐(?:줘|봐)?|보세요)?|띄워\s*(?:줘|주세요))',
        text, flags=re.I)
    if match is None:
        return None
    return KNOWN_SITES[match.group('site').casefold()]


def requested_url(command):
    """Only caller-authorized URL: literal explicit URL or known homepage."""
    return direct_url(command) or known_site_url(command)
