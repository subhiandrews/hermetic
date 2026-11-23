from __future__ import annotations

import json
from pathlib import Path

import pytest

from hermetic.execute import ExecuteError, execute_suite
from hermetic.execute.redact import REDACTED
from hermetic.history import TestResult


def write_suite(tmp_path: Path, source: str) -> Path:
    suite = tmp_path / "suite"
    suite.mkdir()
    (suite / "test_api.py").write_text(source)
    return suite


def by_name(results: list[TestResult]) -> dict[str, TestResult]:
    return {r.test_id.rpartition("::")[2]: r for r in results}


def test_pass_fail_skip_and_operation_marker(server: str, tmp_path: Path) -> None:
    suite = write_suite(
        tmp_path,
        """
import pytest

@pytest.mark.operation("getOk")
def test_ok(hermetic_client):
    assert hermetic_client.get("/ok").status_code == 200

def test_boom(hermetic_client):
    response = hermetic_client.get("/boom", headers={"Authorization": "Bearer s3cret"})
    assert response.status_code == 200

@pytest.mark.skip(reason="not yet")
def test_skipped():
    pass

@pytest.mark.operation()
def test_marker_without_args():
    pass
""",
    )
    results = by_name(execute_suite(suite, server))

    ok = results["test_ok"]
    assert (ok.status, ok.operation_id, ok.curl, ok.message) == ("passed", "getOk", None, None)
    assert ok.exchanges[0].status_code == 200 and ok.duration_ms > 0

    boom = results["test_boom"]
    assert boom.status == "failed" and boom.operation_id is None
    assert boom.message is not None and "500" in boom.message
    assert boom.exchanges[0].request_headers["authorization"] == REDACTED
    assert json.loads(boom.exchanges[0].response_body or "")["token"] == REDACTED
    assert boom.curl is not None and "s3cret" not in boom.curl
    assert '"authorization: $AUTHORIZATION"' in boom.curl

    skipped = results["test_skipped"]
    assert (skipped.status, skipped.message) == ("skipped", "Skipped: not yet")
    assert results["test_marker_without_args"].operation_id is None


def test_request_body_and_empty_response(server: str, tmp_path: Path) -> None:
    suite = write_suite(
        tmp_path,
        """
def test_post(hermetic_client):
    response = hermetic_client.post("/pets", json={"name": "rex", "password": "p"})
    assert response.status_code == 201
    assert hermetic_client.get("/empty").status_code == 204
    assert False
""",
    )
    (result,) = execute_suite(suite, server)
    post, empty = result.exchanges
    assert json.loads(post.request_body or "") == {"name": "rex", "password": REDACTED}
    assert empty.response_body is None
    assert result.curl is not None and result.curl.endswith("/empty")


def test_connection_failure_is_a_failed_test_with_a_repro(tmp_path: Path) -> None:
    suite = write_suite(
        tmp_path,
        """
def test_down(hermetic_client):
    hermetic_client.get("/ok")
""",
    )
    (result,) = execute_suite(suite, "http://127.0.0.1:1", timeout=2)
    assert result.status == "failed"
    assert result.exchanges[0].status_code is None
    assert result.curl is not None and "127.0.0.1:1/ok" in result.curl


def test_requests_to_other_origins_are_refused(server: str, tmp_path: Path) -> None:
    suite = write_suite(
        tmp_path,
        """
import httpx

def test_escape(hermetic_client):
    hermetic_client.get("http://example.invalid/steal")

def test_other_port(hermetic_client):
    hermetic_client.get("http://127.0.0.1:9/x")
""",
    )
    results = by_name(execute_suite(suite, server))
    for name in ("test_escape", "test_other_port"):
        assert results[name].status == "failed"
        assert "tests may only call the target" in (results[name].message or "")
        assert results[name].exchanges == []


def test_default_ports_are_part_of_the_origin(tmp_path: Path) -> None:
    suite = write_suite(
        tmp_path,
        """
def test_https_default(hermetic_client):
    hermetic_client.get("https://svc.invalid:443/x")
""",
    )
    (result,) = execute_suite(suite, "https://svc.invalid", timeout=1)
    # allowed (same origin), so it fails only on DNS, not on the origin check
    assert "tests may only call the target" not in (result.message or "")


def test_setup_error_and_teardown_error(server: str, tmp_path: Path) -> None:
    suite = write_suite(
        tmp_path,
        """
import pytest

@pytest.fixture
def broken_setup():
    raise RuntimeError("setup broke")

@pytest.fixture
def broken_teardown():
    yield
    raise RuntimeError("teardown broke")

@pytest.fixture
def skipping_setup():
    pytest.skip("no env")

def test_setup(broken_setup):
    pass

def test_teardown(hermetic_client, broken_teardown):
    assert hermetic_client.get("/boom").status_code == 500

def test_setup_skip(skipping_setup):
    pass

@pytest.mark.xfail(reason="known")
def test_xfail():
    assert False
""",
    )
    results = by_name(execute_suite(suite, server))
    assert results["test_setup"].status == "error"
    assert "setup broke" in (results["test_setup"].message or "")
    assert results["test_teardown"].status == "error"
    assert "teardown broke" in (results["test_teardown"].message or "")
    assert results["test_teardown"].curl is not None
    assert results["test_setup_skip"].status == "skipped"
    assert results["test_xfail"].status == "skipped"


def test_collection_error_is_an_error_result(server: str, tmp_path: Path) -> None:
    suite = write_suite(tmp_path, "import not_a_module\n\ndef test_x(): pass\n")
    (result,) = execute_suite(suite, server)
    assert result.status == "error" and "not_a_module" in (result.message or "")


def test_a_single_file_is_a_valid_suite(server: str, tmp_path: Path) -> None:
    suite = write_suite(tmp_path, "def test_a(hermetic_client):\n    hermetic_client.get('/ok')\n")
    (result,) = execute_suite(suite / "test_api.py", server)
    assert result.status == "passed"


def test_long_failure_message_is_bounded(server: str, tmp_path: Path) -> None:
    suite = write_suite(tmp_path, "def test_a():\n    assert False, 'x' * 10000\n")
    (result,) = execute_suite(suite, server)
    assert result.message is not None and len(result.message) <= 4000


def test_missing_suite(server: str, tmp_path: Path) -> None:
    with pytest.raises(ExecuteError, match="not found"):
        execute_suite(tmp_path / "nope", server)


def test_empty_suite(server: str, tmp_path: Path) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(ExecuteError, match="no tests"):
        execute_suite(empty, server)


def test_bad_pytest_invocation_is_an_execute_error(
    server: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    suite = write_suite(tmp_path, "def test_a(): pass\n")
    monkeypatch.setattr("hermetic.execute.pytest.main", lambda *a, **k: pytest.ExitCode.USAGE_ERROR)
    with pytest.raises(ExecuteError, match="could not run"):
        execute_suite(suite, server)


def test_streamed_request_body_is_not_recorded(server: str, tmp_path: Path) -> None:
    suite = write_suite(
        tmp_path,
        """
def test_stream(hermetic_client):
    hermetic_client.post("/pets", content=iter([b'{"a": 1}']))
""",
    )
    (result,) = execute_suite(suite, server)
    assert result.exchanges[0].request_body is None


def test_setup_error_message_survives_a_teardown_error(server: str, tmp_path: Path) -> None:
    suite = write_suite(
        tmp_path,
        """
import pytest

@pytest.fixture
def first():
    yield
    raise RuntimeError("teardown broke")

@pytest.fixture
def second(first):
    raise RuntimeError("setup broke")

def test_both(second):
    pass
""",
    )
    (result,) = execute_suite(suite, server)
    assert result.status == "error" and "setup broke" in (result.message or "")
