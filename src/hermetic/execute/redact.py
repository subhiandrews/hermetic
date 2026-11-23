"""Secret redaction for recorded requests and responses."""

from __future__ import annotations

import contextlib
import json
from collections.abc import Mapping
from typing import Any

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
_SENSITIVE_KEY_PARTS = ("password", "passwd", "secret", "token", "apikey", "api_key", "api-key")


def redact_headers(headers: Mapping[str, str]) -> dict[str, str]:
    return {
        name: REDACTED if name.lower() in _SENSITIVE_HEADERS else value
        for name, value in headers.items()
    }


def _is_sensitive_key(key: str) -> bool:
    lowered = key.lower()
    return any(part in lowered for part in _SENSITIVE_KEY_PARTS)


def _redact_json(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: REDACTED if _is_sensitive_key(key) else _redact_json(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact_json(item) for item in value]
    return value


def redact_body(raw: bytes) -> str | None:
    """Decode a body for storage: secret-looking JSON fields masked, length bounded.

    Returns None for an empty body. A body that is not UTF-8 is described, not stored.
    """
    if not raw:
        return None
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return f"<{len(raw)} bytes of binary data>"
    # Not JSON: stored as text. Only JSON field names can be recognised as secrets.
    with contextlib.suppress(ValueError):
        text = json.dumps(_redact_json(json.loads(text)))
    if len(text) > MAX_BODY_CHARS:
        return text[:MAX_BODY_CHARS] + f"... [truncated, {len(text)} characters]"
    return text
