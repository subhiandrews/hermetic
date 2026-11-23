from __future__ import annotations

import json

from hermetic.redact import MAX_BODY_CHARS, REDACTED, redact_body, redact_headers


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


def test_headers_are_matched_by_name_fragment() -> None:
    from hermetic.redact import is_sensitive_header

    for name in ("Api-Key", "apikey", "X-Access-Token", "Private-Token", "X-Amz-Security-Token"):
        assert is_sensitive_header(name)
    assert not is_sensitive_header("Content-Type")


def test_more_json_secret_names() -> None:
    raw = b'{"credentials": "c", "privateKey": "k", "access_key": "a", "ok": 1}'
    assert json.loads(redact_body(raw) or "") == {
        "credentials": REDACTED,
        "privateKey": REDACTED,
        "access_key": REDACTED,
        "ok": 1,
    }


def test_url_userinfo_is_dropped_and_secret_query_values_masked() -> None:
    from hermetic.redact import redact_url

    assert (
        redact_url("http://user:pw@host:8080/p?api_key=abc&q=1&flag=")
        == "http://host:8080/p?api_key=%5BREDACTED%5D&q=1&flag="
    )
    assert redact_url("http://host/p") == "http://host/p"


def test_secret_values_and_masking() -> None:
    from hermetic.redact import mask_secrets, secret_values

    secrets = secret_values(
        {"Authorization": "Bearer abc123", "Accept": "x", "X-Api-Key": "", "X-Auth-Token": "k123"}
    )
    assert secrets == {"Bearer abc123", "abc123", "k123"}
    assert mask_secrets("got Bearer abc123 and abc123", secrets) == f"got {REDACTED} and {REDACTED}"
