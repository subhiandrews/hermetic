from __future__ import annotations

import json

import pytest

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


def test_short_secrets_are_not_masked() -> None:
    from hermetic.redact import mask_secrets

    assert mask_secrets("a=1 and token", {"a=1", "x"}) == "a=1 and token"


def test_url_secrets() -> None:
    from hermetic.redact import url_secrets

    assert url_secrets("http://u:pw123@h/p?api_key=K1&q=v&token=") == {"pw123", "K1"}
    assert url_secrets("http://h/p") == set()


def test_redact_body_reports_the_values_it_masked() -> None:
    found: set[str] = set()
    redact_body(
        b'{"password": "pw", "token": 123456, "secret_flag": true, "x": {"secret": "deep"}}',
        found,
    )
    assert found == {"pw", "deep", "123456"}  # numbers count, booleans do not


def test_json_nested_too_deeply_to_redact_is_omitted() -> None:
    from hermetic.redact import NESTED_BODY, capture_body

    nested = b"[" * 100_000 + b"]" * 100_000
    assert capture_body(nested) == (NESTED_BODY, False)
    # parses (the C parser allows more depth) but overflows the redaction walk
    near_limit = b'{"password": "pw99", "x": ' + b"[" * 3000 + b"]" * 3000 + b"}"
    assert capture_body(near_limit) == (NESTED_BODY, False)


def test_url_headers_are_redacted() -> None:
    headers = redact_headers(
        {
            "Location": "http://u:pw@h/cb?access_token=T&x=1",
            "Content-Location": "/p?api_key=K",
            "Refresh": "5; URL=http://h/r?token=T",
            "Retry-After": "5",
        }
    )
    assert "pw" not in headers["Location"] and "x=1" in headers["Location"]
    assert "K" not in headers["Content-Location"].replace("Content", "")
    assert "token=%5BREDACTED%5D" in headers["Refresh"] and headers["Refresh"].startswith("5; URL=")
    assert headers["Retry-After"] == "5"


def test_cookie_values_are_collected_and_a_malformed_cookie_is_ignored() -> None:
    from hermetic.redact import secret_values

    found = secret_values(
        {
            "Set-Cookie": "session=abc123; Path=/",
            "Cookie": "a=111111; b=222222",
            "Location": "/x?token=T1",
        }
    )
    assert {"abc123", "111111", "222222"} <= found and "T1" in found
    # SimpleCookie rejects this header; the whole value is still masked.
    assert secret_values({"Cookie": "=="}) == {"=="}


def test_form_encoded_secrets_are_masked_and_collected() -> None:
    found: set[str] = set()
    text = redact_body(b"grant_type=password&username=a&password=hunter2xx&client_secret=cs", found)
    assert (
        text
        == "grant_type=password&username=a&password=%5BREDACTED%5D&client_secret=%5BREDACTED%5D"
    )
    assert found == {"hunter2xx", "cs"}


@pytest.mark.parametrize("text", ["plain words here", "no_equals", "a=1 b=2", "a=1&b"])
def test_text_that_is_not_a_form_is_kept(text: str) -> None:
    assert redact_body(text.encode()) == text


def test_malformed_urls_do_not_raise() -> None:
    from hermetic.redact import redact_url, url_secrets

    assert url_secrets("http://[bad") == set()
    assert redact_url("http://[bad") == "<unparseable URL>"
    assert redact_headers({"Location": "http://[bad"}) == {"Location": "<unparseable URL>"}


def test_a_credential_across_the_cut_is_masked() -> None:
    secret = "CROSSING-SECRET-123"
    text = redact_body(b"x" * (MAX_BODY_CHARS - 5) + secret.encode(), {secret}) or ""
    assert "CROSSING" not in text


def test_capture_reports_whether_the_body_is_complete() -> None:
    from hermetic.redact import capture_body

    assert capture_body(b"").complete and capture_body(b"{}").complete
    assert not capture_body(b"\xff").complete
    assert not capture_body(b"x" * (MAX_BODY_CHARS + 1)).complete


@pytest.mark.parametrize(
    "name", ["Ocp-Apim-Subscription-Key", "X-Functions-Key", "Authentication", "key", "SIG", "jwt"]
)
def test_more_credential_names_are_sensitive(name: str) -> None:
    from hermetic.redact import is_sensitive_header

    assert is_sensitive_header(name)


@pytest.mark.parametrize("name", ["monkey", "keyword", "sortKey", "Content-Type"])
def test_short_fragments_do_not_overmatch(name: str) -> None:
    from hermetic.redact import is_sensitive_header

    assert not is_sensitive_header(name)


def test_url_fragment_parameters_are_masked_and_collected() -> None:
    from hermetic.redact import redact_url, url_secrets

    url = "http://h/cb?sig=SIGV&x=1#access_token=FRAGTOK&state=ok"
    assert (
        redact_url(url) == "http://h/cb?sig=%5BREDACTED%5D&x=1#access_token=%5BREDACTED%5D&state=ok"
    )
    assert url_secrets(url) == {"SIGV", "FRAGTOK"}
    assert redact_url("http://h/p#section-2") == "http://h/p#section-2"


def test_url_secrets_include_the_encoded_form() -> None:
    from hermetic.redact import url_secrets

    found = url_secrets("http://h/p?sig=a%2Bb%2Fc%3D&q=1#access_token=x%20y")
    assert {"a%2Bb%2Fc%3D", "a+b/c=", "x%20y", "x y"} <= found
    assert not any(value in ("1", "q") for value in found)


def test_known_secrets_are_masked_inside_json_despite_escaping() -> None:
    secrets = {'pässword"\\x'}
    body = json.dumps({"note": 'pässword"\\x', "list": ['pässword"\\x'], "n": 1}).encode()
    text = redact_body(body, secrets) or ""
    assert "pässword" not in text and "p\\u00e4ssword" not in text and REDACTED in text


def test_a_numeric_credential_echoed_as_a_number_is_masked() -> None:
    secrets = {"123456"}
    body = json.dumps({"echo": 123456, "list": [123456.0, 123456], "short": 12, "ok": True})
    found = json.loads(redact_body(body.encode(), secrets) or "")
    assert found == {"echo": REDACTED, "list": [123456.0, REDACTED], "short": 12, "ok": True}


@pytest.mark.parametrize("name", ["pwd", "passphrase", "passcode", "user_pwd"])
def test_more_password_field_names_are_masked(name: str) -> None:
    assert json.loads(redact_body(json.dumps({name: "v"}).encode()) or "") == {name: REDACTED}


def test_credentials_nested_under_a_secret_key_are_collected() -> None:
    found: set[str] = set()
    body = b'{"credentials": {"refresh": "RT-1234", "list": ["a-secret", 987654, true]}, "ok": 1}'
    text = redact_body(body, found) or ""
    assert json.loads(text) == {"credentials": REDACTED, "ok": 1}
    assert found == {"RT-1234", "a-secret", "987654"}
