from __future__ import annotations

import shlex

from hermetic.execute.redact import REDACTED
from hermetic.history.models import Exchange
from hermetic.report.curl import build_curl


def _exchange(**overrides: object) -> Exchange:
    fields: dict[str, object] = {
        "method": "POST",
        "url": "http://svc/pets",
        "request_headers": {"content-type": "application/json", "host": "svc"},
        "request_body": '{"name": "it\'s"}',
        "status_code": 500,
        "response_headers": {},
        "response_body": None,
        "duration_ms": 1.0,
    }
    fields.update(overrides)
    return Exchange.model_validate(fields)


def test_curl_has_method_headers_body_and_url() -> None:
    command = build_curl(_exchange())
    argv = shlex.split(command)
    assert argv[:2] == ["curl", "-sS"]
    assert argv[argv.index("-X") + 1] == "POST"
    assert "content-type: application/json" in argv
    assert argv[argv.index("--data-raw") + 1] == '{"name": "it\'s"}'
    assert argv[-1] == "http://svc/pets"
    assert not any(part.lower().startswith("host:") for part in argv)


def test_redacted_header_becomes_an_expandable_placeholder() -> None:
    command = build_curl(
        _exchange(request_headers={"Authorization": REDACTED}, request_body=None, method="GET")
    )
    assert '-H "Authorization: $AUTHORIZATION"' in command
    assert "--data-raw" not in command


def test_placeholder_name_cannot_break_out_of_the_quotes() -> None:
    command = build_curl(_exchange(request_headers={'X-A"b$(x)': REDACTED}, request_body=None))
    assert '-H "X-A-b--x-: $X_A_B_X"' in command


def test_unnameable_header_falls_back_to_secret() -> None:
    command = build_curl(_exchange(request_headers={"$$$": REDACTED}, request_body=None))
    assert '-H "---: $SECRET"' in command
