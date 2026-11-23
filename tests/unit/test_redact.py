from __future__ import annotations

import json

from hermetic.execute.redact import MAX_BODY_CHARS, REDACTED, redact_body, redact_headers


def test_sensitive_headers_are_masked_case_insensitively() -> None:
    headers = {"Authorization": "Bearer abc", "X-API-Key": "k", "Accept": "application/json"}
    assert redact_headers(headers) == {
        "Authorization": REDACTED,
        "X-API-Key": REDACTED,
        "Accept": "application/json",
    }


def test_empty_body_is_none() -> None:
    assert redact_body(b"") is None


def test_json_secret_fields_are_masked_recursively() -> None:
    raw = json.dumps({"user": "a", "password": "p", "items": [{"apiKey": "k", "n": 1}]}).encode()
    assert json.loads(redact_body(raw) or "") == {
        "user": "a",
        "password": REDACTED,
        "items": [{"apiKey": REDACTED, "n": 1}],
    }


def test_json_scalars_pass_through() -> None:
    assert redact_body(b"42") == "42"


def test_plain_text_is_kept() -> None:
    assert redact_body(b"hello") == "hello"


def test_binary_body_is_described() -> None:
    assert redact_body(b"\xff\xfe") == "<2 bytes of binary data>"


def test_long_body_is_truncated() -> None:
    result = redact_body(b"x" * (MAX_BODY_CHARS + 10)) or ""
    assert result.startswith("x" * MAX_BODY_CHARS)
    assert result.endswith(f"[truncated, {MAX_BODY_CHARS + 10} characters]")
