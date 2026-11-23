from __future__ import annotations

import json
from pathlib import Path

import pytest

from hermetic.execute import ExecuteError, execute_suite
from hermetic.history import TestResult
from hermetic.redact import REDACTED


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


def test_url_credentials_are_redacted_in_the_record_and_repro(server: str, tmp_path: Path) -> None:
    suite = write_suite(
        tmp_path,
        """
def test_q(hermetic_client):
    assert hermetic_client.get("/ok", params={"api_key": "KEYVALUE"}).status_code == 500
""",
    )
    (result,) = execute_suite(suite, server)
    assert "KEYVALUE" not in result.exchanges[0].url
    assert result.curl is not None and "KEYVALUE" not in result.curl


def test_credentials_quoted_in_failure_text_are_masked(server: str, tmp_path: Path) -> None:
    suite = write_suite(
        tmp_path,
        """
def test_q(hermetic_client):
    token = "tok-9f8e7d"
    response = hermetic_client.get("/ok", headers={"Authorization": f"Bearer {token}"})
    assert response.request.headers["authorization"] == "nope"
""",
    )
    (result,) = execute_suite(suite, server)
    assert result.status == "failed" and "tok-9f8e7d" not in (result.message or "")


def test_a_response_without_content_length_is_not_read_by_the_hook(
    server: str, tmp_path: Path
) -> None:
    suite = write_suite(
        tmp_path,
        """
def test_chunked(hermetic_client):
    assert hermetic_client.get("/chunked").text == "hello"
    assert hermetic_client.get("/ok").text
""",
    )
    (result,) = execute_suite(suite, server)
    assert result.status == "passed"
    chunked, ok = result.exchanges
    assert chunked.response_body is None and ok.response_body is not None


@pytest.mark.parametrize(
    "target", ["localhost:8000", "http://host:abc", "ftp://host", "http://", "nonsense"]
)
def test_invalid_targets_are_rejected_before_running(target: str, tmp_path: Path) -> None:
    with pytest.raises(ExecuteError, match="invalid --target"):
        execute_suite(tmp_path, target)


def test_an_interrupted_run_is_an_error_not_a_partial_pass(server: str, tmp_path: Path) -> None:
    suite = write_suite(
        tmp_path,
        """
import pytest

def test_a():
    pass

def test_b():
    pytest.exit("stop here")
""",
    )
    with pytest.raises(ExecuteError, match="interrupted"):
        execute_suite(suite, server)


def test_only_new_suite_modules_are_evicted(tmp_path: Path) -> None:
    import sys
    import types

    from hermetic.execute import _forget_new_modules_under

    def module(name: str, file: Path) -> None:
        mod = types.ModuleType(name)
        mod.__file__ = str(file)
        sys.modules[name] = mod

    lib = tmp_path / ".venv" / "lib" / "site-packages" / "dep.py"
    module("zz_old", tmp_path / "old.py")  # imported before the run
    before = set(sys.modules)
    module("zz_suite", tmp_path / "test_x.py")
    module("zz_lib", lib)
    module("zz_elsewhere", tmp_path.parent / "elsewhere.py")
    sys.modules["zz_nofile"] = types.ModuleType("zz_nofile")
    try:
        _forget_new_modules_under(tmp_path, before)
        assert "zz_suite" not in sys.modules
        assert all(
            name in sys.modules for name in ("zz_old", "zz_lib", "zz_elsewhere", "zz_nofile")
        )
    finally:
        for name in ("zz_old", "zz_suite", "zz_lib", "zz_elsewhere", "zz_nofile"):
            sys.modules.pop(name, None)


def test_teardown_error_after_a_failed_call_keeps_both_messages(
    server: str, tmp_path: Path
) -> None:
    suite = write_suite(
        tmp_path,
        """
import pytest

@pytest.fixture
def cleanup():
    yield
    raise RuntimeError("teardown broke")

def test_both(hermetic_client, cleanup):
    assert hermetic_client.get("/boom").status_code == 200
""",
    )
    (result,) = execute_suite(suite, server)
    assert result.status == "error"
    assert "500 == 200" in (result.message or "") and "teardown broke" in (result.message or "")
    assert result.curl is not None


def test_url_secrets_are_masked_in_failure_text(server: str, tmp_path: Path) -> None:
    suite = write_suite(
        tmp_path,
        """
def test_status(hermetic_client):
    response = hermetic_client.get("/ok", params={"api_key": "QUERYSECRET"})
    response.raise_for_status()
    assert response.url.params["api_key"] == "other"
""",
    )
    (result,) = execute_suite(suite, server)
    assert result.status == "failed" and "QUERYSECRET" not in (result.message or "")


def test_json_body_secrets_are_masked_in_failure_text(server: str, tmp_path: Path) -> None:
    suite = write_suite(
        tmp_path,
        """
def test_body(hermetic_client):
    body = hermetic_client.post("/pets", json={"password": "hunter2hunter2"}).json()
    assert body == {}
""",
    )
    (result,) = execute_suite(suite, server)
    assert result.status == "failed" and "hunter2hunter2" not in (result.message or "")


def test_origin_error_does_not_quote_url_credentials(server: str, tmp_path: Path) -> None:
    suite = write_suite(
        tmp_path,
        """
def test_escape(hermetic_client):
    hermetic_client.get("http://user:PASSWORD1@elsewhere.invalid/x?token=TOKEN123")
""",
    )
    (result,) = execute_suite(suite, server)
    message = result.message or ""
    assert "tests may only call the target" in message
    assert "PASSWORD1" not in message and "TOKEN123" not in message


def test_credential_is_masked_before_the_message_is_cut(server: str, tmp_path: Path) -> None:
    suite = write_suite(
        tmp_path,
        """
def test_long(hermetic_client):
    secret = "S3CRET-VALUE-XYZ"
    hermetic_client.get("/ok", headers={"X-Api-Key": secret})
    assert False, "x" * 3990 + secret
""",
    )
    (result,) = execute_suite(suite, server)
    message = result.message or ""
    assert "S3CRET" not in message and len(message) <= 4000


def test_compressed_and_deeply_nested_bodies_do_not_break_the_hook(
    server: str, tmp_path: Path
) -> None:
    suite = write_suite(
        tmp_path,
        """
def test_odd_bodies(hermetic_client):
    assert hermetic_client.get("/gzip").json()["big"]
    assert hermetic_client.get("/deep").status_code == 200
""",
    )
    (result,) = execute_suite(suite, server)
    assert result.status == "passed"
    gz, deep = result.exchanges
    assert gz.response_body is None  # compressed: not read in the hook
    assert deep.response_body == "<body omitted: nested too deeply to redact>"


def test_recorded_bodies_mask_credentials_echoed_under_innocent_keys(
    server: str, tmp_path: Path
) -> None:
    suite = write_suite(
        tmp_path,
        """
def test_echo(hermetic_client):
    response = hermetic_client.get("/echo-auth", headers={"Authorization": "Bearer ECHOTOKEN99"})
    assert response.status_code == 200
""",
    )
    (result,) = execute_suite(suite, server)
    assert result.exchanges[0].response_body is not None
    assert "ECHOTOKEN99" not in result.exchanges[0].response_body


def test_request_bodies_mask_a_credential_seen_earlier_in_the_request(
    server: str, tmp_path: Path
) -> None:
    suite = write_suite(
        tmp_path,
        """
def test_body(hermetic_client):
    hermetic_client.post(
        "/pets",
        json={"note": "uses KEYINBODY99"},
        headers={"X-Api-Key": "KEYINBODY99"},
    )
    assert False
""",
    )
    (result,) = execute_suite(suite, server)
    assert "KEYINBODY99" not in (result.exchanges[0].request_body or "")


def test_redirect_and_cookie_headers_are_redacted(server: str, tmp_path: Path) -> None:
    suite = write_suite(
        tmp_path,
        """
def test_headers(hermetic_client):
    redirect = hermetic_client.get("/redirect")
    cookie = hermetic_client.get("/cookie")
    assert cookie.cookies["session"] == "different", redirect.headers["location"]
""",
    )
    (result,) = execute_suite(suite, server)
    redirect, cookie = result.exchanges
    shown = str(redirect.response_headers) + str(cookie.response_headers) + (result.message or "")
    for leaked in ("LOCPASS99", "LOCTOK99", "REFTOK99", "COOKIEVAL99"):
        assert leaked not in shown
    assert "elsewhere.invalid" in redirect.response_headers["location"]
    assert "elsewhere.invalid/r" in redirect.response_headers["refresh"]
    assert cookie.response_headers["set-cookie"] == "[REDACTED]"


def test_target_credentials_are_not_echoed_in_errors(tmp_path: Path) -> None:
    for target in ("ftp://u:TARGETPW99@host/?token=TARGETTOK99", "http://u:TARGETPW99@[bad"):
        with pytest.raises(ExecuteError) as raised:
            execute_suite(tmp_path, target)
        assert "TARGETPW99" not in str(raised.value) and "TARGETTOK99" not in str(raised.value)


def test_a_malformed_location_header_does_not_break_the_test(server: str, tmp_path: Path) -> None:
    suite = write_suite(
        tmp_path,
        """
def test_bad_location(hermetic_client):
    assert hermetic_client.get("/bad-location").status_code == 302
""",
    )
    (result,) = execute_suite(suite, server)
    assert result.status == "passed"
    assert result.exchanges[0].response_headers["location"] == "<unparseable URL>"


def test_binary_request_bodies_are_marked_incomplete(server: str, tmp_path: Path) -> None:
    suite = write_suite(
        tmp_path,
        """
def test_bin(hermetic_client):
    hermetic_client.post("/pets", content=b"\\xff\\xfe")
    assert False
""",
    )
    (result,) = execute_suite(suite, server)
    assert not result.exchanges[0].request_body_complete
    assert result.curl is not None and "not reproduced here" in result.curl


def test_the_client_asks_for_uncompressed_bodies(server: str, tmp_path: Path) -> None:
    suite = write_suite(
        tmp_path,
        """
def test_header(hermetic_client):
    hermetic_client.get("/ok")
""",
    )
    (result,) = execute_suite(suite, server)
    assert result.exchanges[0].request_headers["accept-encoding"] == "identity"


def test_many_tests_keep_their_own_results(server: str, tmp_path: Path) -> None:
    source = "\n".join(f"def test_{n}(hermetic_client):\n    assert {n} != 7\n" for n in range(12))
    results = execute_suite(write_suite(tmp_path, source), server)
    assert [r.status for r in results].count("failed") == 1 and len(results) == 12


def test_a_header_that_echoes_a_known_credential_is_masked(server: str, tmp_path: Path) -> None:
    suite = write_suite(
        tmp_path,
        """
def test_echo(hermetic_client):
    hermetic_client.get("/echo-header", headers={"Authorization": "Bearer HDRECHO99"})
""",
    )
    (result,) = execute_suite(suite, server)
    assert "HDRECHO99" not in str(result.exchanges[0].response_headers)
    assert "HDRECHO99" not in str(result.exchanges[0].request_headers)


def test_target_credentials_are_masked_in_collection_errors(tmp_path: Path) -> None:
    suite = write_suite(
        tmp_path, "raise RuntimeError('http://u:TARGETPW77@127.0.0.1:1/?token=TT99')\n"
    )
    (result,) = execute_suite(suite, "http://u:TARGETPW77@127.0.0.1:1/?token=TT99")
    assert result.status == "error"
    assert "TARGETPW77" not in (result.message or "") and "TT99" not in (result.message or "")


def test_a_known_credential_is_masked_in_the_url_path(server: str, tmp_path: Path) -> None:
    suite = write_suite(
        tmp_path,
        """
def test_path(hermetic_client):
    hermetic_client.get("/ok", headers={"X-Api-Key": "PATHSECRET99"})
    hermetic_client.get("/reset/PATHSECRET99")
    assert False
""",
    )
    (result,) = execute_suite(suite, server)
    assert "PATHSECRET99" not in result.exchanges[1].url
    assert result.curl is not None and "PATHSECRET99" not in result.curl


def test_responses_are_paired_with_their_own_request() -> None:
    import httpx

    from hermetic.execute.plugin import _Recording, origin_of

    recording = _Recording({origin_of("http://svc")})
    first = httpx.Request("GET", "http://svc/first")
    second = httpx.Request("GET", "http://svc/second")
    recording.on_request(first)
    recording.on_request(second)
    # the second response arrives first
    recording.on_response(httpx.Response(201, content=b"b", request=second))
    recording.on_response(httpx.Response(200, content=b"a", request=first))
    by_url = {e.url: e for e in recording.exchanges}
    assert by_url["http://svc/first"].status_code == 200
    assert by_url["http://svc/second"].status_code == 201


def test_a_non_ascii_content_length_does_not_break_the_hook() -> None:
    import httpx

    from hermetic.execute.plugin import _Recording, origin_of

    recording = _Recording({origin_of("http://svc")})
    request = httpx.Request("GET", "http://svc/x")
    recording.on_request(request)
    response = httpx.Response(200, headers=[(b"Content-Length", "٣".encode())], request=request)
    recording.on_response(response)
    assert recording.exchanges[0].response_body is None


def test_credentials_quoted_in_a_skip_reason_are_masked(server: str, tmp_path: Path) -> None:
    suite = write_suite(
        tmp_path,
        """
import pytest

def test_skip(hermetic_client):
    hermetic_client.get("/ok", headers={"X-Api-Key": "SKIPSECRET99"})
    pytest.skip("saw SKIPSECRET99 in the request")
""",
    )
    (result,) = execute_suite(suite, server)
    assert result.status == "skipped"
    assert "SKIPSECRET99" not in (result.message or "") and "saw" in (result.message or "")


def test_a_credential_learned_late_is_masked_in_earlier_exchanges(
    server: str, tmp_path: Path
) -> None:
    suite = write_suite(
        tmp_path,
        """
def test_late(hermetic_client):
    body = hermetic_client.post("/pets", json={"greeting": "LATETOKEN99"}).text
    hermetic_client.get("/ok", headers={"X-Api-Key": "LATETOKEN99"})
    assert False
""",
    )
    (result,) = execute_suite(suite, server)
    assert "LATETOKEN99" not in str(result.exchanges[0])
    assert "LATETOKEN99" not in str(result.exchanges[1])


def test_the_secrets_set_survives_hooks_running_on_many_threads() -> None:
    import threading

    import httpx

    from hermetic.execute.plugin import _Recording, origin_of

    recording = _Recording({origin_of("http://svc")})
    errors: list[BaseException] = []

    def work(n: int) -> None:
        try:
            for i in range(60):
                request = httpx.Request(
                    "POST",
                    "http://svc/x",
                    headers={"X-Api-Key": f"key-{n}-{i}-secret"},
                    json={"n": i},
                )
                recording.on_request(request)
                recording.on_response(httpx.Response(200, content=b"{}", request=request))
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=work, args=(n,)) for n in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == [] and len(recording.exchanges) == 360
    assert all(e.status_code == 200 for e in recording.exchanges)


def test_a_credential_in_a_parametrize_id_is_masked_in_the_test_id(
    server: str, tmp_path: Path
) -> None:
    suite = write_suite(
        tmp_path,
        """
import pytest

@pytest.mark.parametrize("key", ["PARAMSECRET99"])
def test_param(hermetic_client, key):
    hermetic_client.get("/ok", headers={"X-Api-Key": key})
""",
    )
    (result,) = execute_suite(suite, server)
    assert "PARAMSECRET99" not in result.test_id and result.test_id.startswith(
        "test_api.py::test_param["
    )
