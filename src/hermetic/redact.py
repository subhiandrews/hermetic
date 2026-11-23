"""Secret redaction for recorded requests and responses."""

from __future__ import annotations

import contextlib
import json
from collections.abc import Mapping
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

REDACTED = "[REDACTED]"
MAX_BODY_CHARS = 64 * 1024

_SENSITIVE_HEADERS = frozenset(
    {
        "authorization",
        "proxy-authorization",
        "cookie",
        "set-cookie",
        "x-api-key",
        "x-auth-token",
        "x-csrf-token",
        "x-xsrf-token",
    }
)
_SENSITIVE_KEY_PARTS = (
    "password",
    "passwd",
    "secret",
    "token",
    "apikey",
    "api_key",
    "api-key",
    "authorization",
    "credential",
    "private_key",
    "private-key",
    "privatekey",
    "access_key",
    "access-key",
    "accesskey",
    "cookie",
    "session",
)


def _is_sensitive_key(key: str) -> bool:
    lowered = key.lower()
    return any(part in lowered for part in _SENSITIVE_KEY_PARTS)


def is_sensitive_header(name: str) -> bool:
    return name.lower() in _SENSITIVE_HEADERS or _is_sensitive_key(name)


def redact_headers(headers: Mapping[str, str]) -> dict[str, str]:
    return {
        name: REDACTED if is_sensitive_header(name) else value for name, value in headers.items()
    }


def secret_values(headers: Mapping[str, str]) -> set[str]:
    """Raw values of the sensitive headers, plus the credential after a scheme word.

    `Authorization: Bearer abc` yields both the whole value and `abc`, so free text that
    quotes only the token can still be masked.
    """
    found: set[str] = set()
    for name, value in headers.items():
        if is_sensitive_header(name) and value:
            found.add(value)
            _, _, credential = value.partition(" ")
            if credential:
                found.add(credential)
    return found


MIN_MASKED_LENGTH = 4  # shorter values would mangle the text they are replaced in


def mask_secrets(text: str, secrets: set[str]) -> str:
    for secret in sorted(secrets, key=len, reverse=True):
        if len(secret) >= MIN_MASKED_LENGTH:
            text = text.replace(secret, REDACTED)
    return text


def url_secrets(url: str) -> set[str]:
    """The userinfo password and the secret-looking query values of a URL."""
    parts = urlsplit(url)
    found = {value for key, value in parse_qsl(parts.query) if _is_sensitive_key(key) and value}
    if parts.password:
        found.add(parts.password)
    return found


def redact_url(url: str) -> str:
    """Drop userinfo and mask the values of secret-looking query parameters."""
    parts = urlsplit(url)
    netloc = parts.netloc.rpartition("@")[2]
    query = urlencode(
        [
            (key, REDACTED if _is_sensitive_key(key) else value)
            for key, value in parse_qsl(parts.query, keep_blank_values=True)
        ]
    )
    return urlunsplit(parts._replace(netloc=netloc, query=query if parts.query else ""))


def _redact_json(value: Any, found: set[str]) -> Any:
    """Mask secret-looking fields; the string values masked are added to `found`."""
    if isinstance(value, dict):
        masked: dict[str, Any] = {}
        for key, item in value.items():
            if _is_sensitive_key(key):
                if isinstance(item, str):
                    found.add(item)
                masked[key] = REDACTED
            else:
                masked[key] = _redact_json(item, found)
        return masked
    if isinstance(value, list):
        return [_redact_json(item, found) for item in value]
    return value


def redact_body(raw: bytes, secrets: set[str] | None = None) -> str | None:
    """Decode a body for storage: secret-looking JSON fields masked, length bounded.

    String values it masks are added to `secrets`, if given. Returns None for an empty body.
    A body that is not UTF-8 is described, not stored.
    """
    if not raw:
        return None
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return f"<{len(raw)} bytes of binary data>"
    # Not JSON: stored as text. Only JSON field names can be recognised as secrets.
    found: set[str] = set()
    # RecursionError: a few KB of `[` is enough to overflow the parser.
    with contextlib.suppress(ValueError, RecursionError):
        text = json.dumps(_redact_json(json.loads(text), found))
        if secrets is not None:
            secrets |= found
    if len(text) > MAX_BODY_CHARS:
        return text[:MAX_BODY_CHARS] + f"... [truncated, {len(text)} characters]"
    return text
