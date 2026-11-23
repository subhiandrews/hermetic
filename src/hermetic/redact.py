"""Secret redaction for recorded requests and responses."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from http.cookies import CookieError, SimpleCookie
from typing import Any, NamedTuple
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
    "subscription-key",
    "subscription_key",
    "functions-key",
    "authentication",
    "signature",
    "assertion",
)
# Too short to match as a fragment ("key" is inside "monkey" and "keyword").
_SENSITIVE_EXACT_KEYS = frozenset({"key", "sig", "jwt"})


def _is_sensitive_key(key: str) -> bool:
    lowered = key.lower()
    return lowered in _SENSITIVE_EXACT_KEYS or any(part in lowered for part in _SENSITIVE_KEY_PARTS)


def is_sensitive_header(name: str) -> bool:
    return name.lower() in _SENSITIVE_HEADERS or _is_sensitive_key(name)


_URL_HEADERS = frozenset({"location", "content-location"})
_REFRESH_URL = re.compile(r"(url\s*=\s*)(\S+)", re.IGNORECASE)


def _redact_header(name: str, value: str) -> str:
    lowered = name.lower()
    if is_sensitive_header(name):
        return REDACTED
    if lowered in _URL_HEADERS:
        return redact_url(value)
    if lowered == "refresh":  # "0; url=https://..."
        return _REFRESH_URL.sub(lambda m: m.group(1) + redact_url(m.group(2)), value)
    return value


def redact_headers(headers: Mapping[str, str]) -> dict[str, str]:
    return {name: _redact_header(name, value) for name, value in headers.items()}


def secret_values(headers: Mapping[str, str]) -> set[str]:
    """Raw values of the sensitive headers, plus the credential after a scheme word.

    `Authorization: Bearer abc` yields both the whole value and `abc`, so free text that
    quotes only the token can still be masked.
    """
    found: set[str] = set()
    for name, value in headers.items():
        lowered = name.lower()
        if lowered in _URL_HEADERS:
            found |= url_secrets(value)
        elif is_sensitive_header(name) and value:
            found.add(value)
            _, _, credential = value.partition(" ")
            if credential:
                found.add(credential)
            if lowered in ("cookie", "set-cookie"):
                found |= _cookie_values(value)
    return found


def _cookie_values(header: str) -> set[str]:
    """The value of each cookie in a Cookie or Set-Cookie header."""
    jar: SimpleCookie = SimpleCookie()
    try:
        jar.load(header)
    except CookieError:
        return set()
    return {morsel.value for morsel in jar.values() if morsel.value}


MIN_MASKED_LENGTH = 4  # shorter values would mangle the text they are replaced in


def mask_secrets(text: str, secrets: set[str]) -> str:
    for secret in sorted(secrets, key=len, reverse=True):
        if len(secret) >= MIN_MASKED_LENGTH:
            text = text.replace(secret, REDACTED)
    return text


def _mask_params(encoded: str) -> str:
    """Mask the secret-looking parameters of a query string or an OAuth-style fragment."""
    return urlencode(
        [
            (key, REDACTED if _is_sensitive_key(key) else value)
            for key, value in parse_qsl(encoded, keep_blank_values=True)
        ]
    )


def url_secrets(url: str) -> set[str]:
    """The userinfo password and the secret-looking query and fragment values of a URL."""
    try:
        parts = urlsplit(url)
    except ValueError:  # a buggy service can send a malformed URL; there is nothing to mask
        return set()
    found = {
        value
        for encoded in (parts.query, parts.fragment)
        for key, value in parse_qsl(encoded)
        if _is_sensitive_key(key) and value
    }
    if parts.password:
        found.add(parts.password)
    return found


def redact_url(url: str) -> str:
    """Drop userinfo and mask the values of secret-looking query and fragment parameters.

    The fragment is read as parameters when it has any (`#access_token=...`, as OAuth
    implicit flows return it); other fragments are kept.
    """
    try:
        parts = urlsplit(url)
    except ValueError:  # too malformed to clean, so it is not shown
        return "<unparseable URL>"
    return urlunsplit(
        parts._replace(
            netloc=parts.netloc.rpartition("@")[2],
            query=_mask_params(parts.query) if parts.query else "",
            fragment=_mask_params(parts.fragment) if "=" in parts.fragment else parts.fragment,
        )
    )


def _redact_json(value: Any, found: set[str]) -> Any:
    """Mask secret-looking fields; the string values masked are added to `found`."""
    if isinstance(value, dict):
        masked: dict[str, Any] = {}
        for key, item in value.items():
            if _is_sensitive_key(key):
                if isinstance(item, str):
                    found.add(item)
                elif isinstance(item, int | float) and not isinstance(item, bool):
                    found.add(str(item))
                masked[key] = REDACTED
            else:
                masked[key] = _redact_json(item, found)
        return masked
    if isinstance(value, list):
        return [_redact_json(item, found) for item in value]
    return value


class Captured(NamedTuple):
    text: str | None
    complete: bool  # False when the text only describes the body (binary) or cuts it short


def _redact_form(text: str, found: set[str]) -> str | None:
    """Mask secret fields of a form-urlencoded body, or return None if it is not one."""
    if "=" not in text or any(char.isspace() for char in text):
        return None
    try:
        pairs = parse_qsl(text, keep_blank_values=True, strict_parsing=True)
    except ValueError:
        return None
    for key, value in pairs:
        if _is_sensitive_key(key) and value:
            found.add(value)
    return urlencode([(k, REDACTED if _is_sensitive_key(k) else v) for k, v in pairs])


def capture_body(raw: bytes, secrets: set[str] | None = None) -> Captured:
    """Decode a body for storage.

    Secret-looking JSON and form fields are masked, then every credential in `secrets` is
    masked, then the text is cut to a bound. String values masked here are added to `secrets`
    (when given), so later text that quotes them is masked too. An empty body has no text; a
    body that is not UTF-8 is described, not stored.
    """
    if not raw:
        return Captured(None, True)
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return Captured(f"<{len(raw)} bytes of binary data>", False)
    found: set[str] = set()
    try:
        # RecursionError: a few KB of `[` is enough to overflow the parser.
        text = json.dumps(_redact_json(json.loads(text), found))
    except (ValueError, RecursionError):
        text = _redact_form(text, found) or text  # not JSON: a form, or plain text kept as is
    if secrets is not None:
        secrets |= found
        text = mask_secrets(text, secrets)  # before the cut: a credential across it would escape
    if len(text) > MAX_BODY_CHARS:
        return Captured(text[:MAX_BODY_CHARS] + f"... [truncated, {len(text)} characters]", False)
    return Captured(text, True)


def redact_body(raw: bytes, secrets: set[str] | None = None) -> str | None:
    """The text of `capture_body`."""
    return capture_body(raw, secrets).text
