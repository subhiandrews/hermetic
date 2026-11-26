"""Switching a mock to a declared error: the control, the fixture and the checks around them."""

from __future__ import annotations

import gzip
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from hermetic.execute import ExecuteError, execute_suite
from hermetic.execute import mocks as mocks_module
from hermetic.execute.mocks import Declared, MockControl, MockError, Mocks, check_mock
from hermetic.ingest import ingest
from hermetic.provision import build_stubs
from tests.fake_admin import FakeAdmin

SPEC = """
openapi: 3.0.3
info: {title: Carts, version: "1"}
paths:
  /carts/{id}:
    get:
      operationId: getCart
      parameters: [{name: id, in: path, required: true, schema: {type: string}}]
      responses:
        "200": {description: ok}
        "404": {description: missing}
        "409": {description: conflict}
  /items:
    get:
      operationId: listItems
      responses:
        "200": {description: ok}
"""


@pytest.fixture
def stubs(tmp_path: Path) -> list[dict[str, Any]]:
    """The stubs `hermetic stubs` writes for SPEC, as a mock would list them."""
    path = tmp_path / "carts.yaml"
    path.write_text(SPEC)
    return list(build_stubs(ingest(path))[0].values())


def serve(admin: FakeAdmin, mappings: list[Any], total: int | None = None) -> None:
    count = len(mappings) if total is None else total
    admin.listing = {"mappings": mappings, "meta": {"total": count}}


def control(admin: FakeAdmin) -> MockControl:
    return MockControl("carts", admin.url, timeout=5)


def scenario_of(stubs: list[dict[str, Any]], operation: str) -> str:
    return str(
        next(s["metadata"]["scenario"] for s in stubs if s["metadata"]["operation"] == operation)
    )


# --- switching ---


def test_answer_switches_the_scenario_to_the_declared_state(
    mock_admin: FakeAdmin, stubs: list[dict[str, Any]]
) -> None:
    serve(mock_admin, stubs)
    mock = control(mock_admin)
    mock.answer("getCart", 404)
    scenario = scenario_of(stubs, "getCart")
    assert mock_admin.calls == [
        ("GET", "/__admin/mappings", None),
        ("PUT", f"/__admin/scenarios/{scenario}/state", {"state": "404"}),
    ]
    assert mock.touched
    mock.close()


def test_the_listing_is_read_once(mock_admin: FakeAdmin, stubs: list[dict[str, Any]]) -> None:
    serve(mock_admin, stubs)
    mock = control(mock_admin)
    mock.answer("getCart", 404)
    mock.answer("getCart", 409)
    assert mock_admin.paths("GET") == ["/__admin/mappings"]
    assert len(mock_admin.paths("PUT")) == 2
    mock.close()


def test_reset_returns_every_scenario_to_its_first_state(
    mock_admin: FakeAdmin, stubs: list[dict[str, Any]]
) -> None:
    serve(mock_admin, stubs)
    mock = control(mock_admin)
    mock.answer("getCart", 404)
    mock.reset()
    assert mock_admin.paths("POST") == ["/__admin/scenarios/reset"]
    assert not mock.touched
    mock.close()


def test_an_operation_without_a_declared_error_is_refused(
    mock_admin: FakeAdmin, stubs: list[dict[str, Any]]
) -> None:
    serve(mock_admin, stubs)
    mock = control(mock_admin)
    for operation in ("listItems", "nope"):
        with pytest.raises(MockError, match="declares no error for operation") as caught:
            mock.answer(operation, 404)
        assert "operations with errors: getCart" in str(caught.value)
    assert not mock_admin.paths("PUT")
    mock.close()


def test_a_mock_with_no_declared_errors_says_so(mock_admin: FakeAdmin) -> None:
    serve(mock_admin, [])
    mock = control(mock_admin)
    with pytest.raises(MockError, match="operations with errors: none"):
        mock.answer("getCart", 404)
    mock.close()


def test_a_status_the_operation_does_not_declare_is_refused(
    mock_admin: FakeAdmin, stubs: list[dict[str, Any]]
) -> None:
    serve(mock_admin, stubs)
    mock = control(mock_admin)
    with pytest.raises(MockError, match=r"does not declare 500 for 'getCart'; declared: 404, 409"):
        mock.answer("getCart", 500)
    assert not mock_admin.paths("PUT") and not mock.touched
    mock.close()


def test_a_long_list_of_names_is_cut(mock_admin: FakeAdmin, stubs: list[dict[str, Any]]) -> None:
    def stub(i: int) -> dict[str, Any]:
        metadata = {
            "hermetic": True,
            "operation": f"op{i:02d}",
            "scenario": f"s{i}",
            "state": "404",
        }
        return {**stubs[0], "metadata": metadata}

    serve(mock_admin, [stub(i) for i in range(22)])
    mock = control(mock_admin)
    with pytest.raises(MockError, match=r"op19, and 2 more"):
        mock.answer("nope", 404)
    mock.close()


# --- what the mock says and does ---


def test_a_switch_the_mock_refuses_is_an_error_and_is_still_reset(
    mock_admin: FakeAdmin, stubs: list[dict[str, Any]]
) -> None:
    serve(mock_admin, stubs)
    mock_admin.statuses["PUT"] = 500
    mock = control(mock_admin)
    with pytest.raises(MockError, match=r"answered PUT .* with 500"):
        mock.answer("getCart", 404)
    assert mock.touched  # the mock may have changed before it failed, so it is reset
    mock.close()


def test_a_mock_that_cannot_be_reached_is_an_error() -> None:
    mock = MockControl("carts", "http://127.0.0.1:1", timeout=2)
    with pytest.raises(MockError, match=r"cannot reach mock 'carts' at http://127.0.0.1:1"):
        mock.answer("getCart", 404)
    mock.close()


@pytest.mark.parametrize(
    "document",
    [
        b"not json",
        b'{"meta": {"total": 0}}',
        b'{"mappings": [], "meta": {}}',
        b'{"mappings": "x", "meta": {"total": 0}}',
        b'{"mappings": [], "meta": {"total": "0"}}',
        b'{"mappings": [], "meta": {"total": 3}}',  # a page of a longer listing
        b"[]",
    ],
)
def test_a_listing_that_is_not_complete_is_refused(mock_admin: FakeAdmin, document: bytes) -> None:
    mock_admin.listing_raw = document
    mock = control(mock_admin)
    with pytest.raises(MockError, match=r"did not (return a stub listing|list all of its stubs)"):
        mock.answer("getCart", 404)
    mock.close()


def test_a_huge_listing_is_refused(
    mock_admin: FakeAdmin, stubs: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    serve(mock_admin, stubs)
    monkeypatch.setattr(mocks_module, "MAX_LISTING_BYTES", 100)
    mock = control(mock_admin)
    with pytest.raises(MockError, match="sent more than 100 bytes"):
        mock.answer("getCart", 404)
    mock.close()


def test_a_compressed_listing_is_never_expanded(
    mock_admin: FakeAdmin, stubs: list[dict[str, Any]]
) -> None:
    serve(mock_admin, stubs)
    mock_admin.listing_raw = gzip.compress(json.dumps(mock_admin.listing).encode())
    mock_admin.listing_headers = {"Content-Encoding": "gzip"}
    mock = control(mock_admin)
    with pytest.raises(MockError, match="did not return a stub listing"):
        mock.answer("getCart", 404)  # the raw bytes are not JSON, and were not decoded
    mock.close()


def test_a_reply_that_is_too_long_for_what_it_is_for_is_refused(
    mock_admin: FakeAdmin, stubs: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    serve(mock_admin, stubs)
    monkeypatch.setattr(mocks_module, "MAX_REPLY_BYTES", 1)  # the switch's `{}` is two bytes
    mock = control(mock_admin)
    with pytest.raises(MockError, match="sent more than 1 bytes"):
        mock.answer("getCart", 404)
    mock.close()


def test_a_reply_that_keeps_arriving_is_cut_off_at_a_deadline(
    mock_admin: FakeAdmin, stubs: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    serve(mock_admin, stubs)
    reads: list[int] = []

    def clock() -> float:  # the first read sets the deadline; every later one is past it
        reads.append(1)
        return 0.0 if len(reads) == 1 else 1000.0

    # only the name inside `mocks`, so nothing else that reads the clock is affected
    monkeypatch.setattr("hermetic.execute.mocks.time", SimpleNamespace(monotonic=clock))
    mock = control(mock_admin)
    with pytest.raises(MockError, match="answered too slowly"):
        mock.answer("getCart", 404)
    mock.close()


def test_the_listing_is_shared_by_the_controls_of_a_run(
    mock_admin: FakeAdmin, stubs: list[dict[str, Any]]
) -> None:
    serve(mock_admin, stubs)
    shared: dict[str, Declared] = {}
    for _ in range(3):  # three tests, each with its own control
        mock = MockControl("carts", mock_admin.url, timeout=5, listings=shared)
        mock.answer("getCart", 404)
        mock.close()
    assert mock_admin.paths("GET") == ["/__admin/mappings"]
    assert len(mock_admin.paths("PUT")) == 3


def test_only_the_stubs_hermetic_wrote_with_a_usable_scenario_count(
    mock_admin: FakeAdmin, stubs: list[dict[str, Any]]
) -> None:
    def stub(**changes: Any) -> dict[str, Any]:
        metadata = {"hermetic": True, "operation": "x", "scenario": "x", "state": "404"}
        return {"metadata": {**metadata, **changes}}

    ignored = [
        "not a stub",
        {"no": "metadata"},
        {"metadata": "text"},
        stub(hermetic=False),  # hand-written
        stub(operation=3),
        stub(scenario=3),
        stub(scenario="../escape"),  # not a slug, so never put in a path
        stub(scenario="UPPER"),
        stub(state=404),
        stub(state="Started"),
        stub(state="404\n"),
        stub(state="2xx"),
    ]
    serve(mock_admin, [*ignored, *stubs])
    mock = control(mock_admin)
    with pytest.raises(MockError, match=r"operations with errors: getCart$"):
        mock.answer("x", 404)  # none of the ignored ones made an operation `x`
    mock.close()


def test_two_scenarios_for_one_operation_are_refused(
    mock_admin: FakeAdmin, stubs: list[dict[str, Any]]
) -> None:
    metadata = {"hermetic": True, "operation": "getCart", "scenario": "other", "state": "404"}
    serve(mock_admin, [*stubs, {**stubs[0], "metadata": metadata}])
    mock = control(mock_admin)
    with pytest.raises(MockError, match="two scenarios for operation 'getCart'"):
        mock.answer("getCart", 404)
    mock.close()


# --- the set of mocks ---


def test_an_unknown_mock_name_says_what_is_known(mock_admin: FakeAdmin) -> None:
    mocks = Mocks({"carts": control(mock_admin)})
    assert mocks["carts"].name == "carts"
    with pytest.raises(MockError, match=r"no mock named 'users'; known: carts\. Pass --mock"):
        mocks["users"]
    mocks.finish()
    with pytest.raises(MockError, match="known: none"):
        Mocks({})["carts"]


def test_finish_resets_only_what_was_switched(
    mock_admin: FakeAdmin, stubs: list[dict[str, Any]]
) -> None:
    serve(mock_admin, stubs)
    untouched = FakeAdmin()
    untouched.start()
    try:
        users = MockControl("users", untouched.url, timeout=5)
        mocks = Mocks({"carts": control(mock_admin), "users": users})
        mocks["carts"].answer("getCart", 404)
        mocks.finish()
        assert mock_admin.paths("POST") == ["/__admin/scenarios/reset"]
        assert untouched.calls == []
    finally:
        untouched.stop()


def test_finish_tries_every_mock_then_reports_the_first_failure(
    mock_admin: FakeAdmin, stubs: list[dict[str, Any]]
) -> None:
    serve(mock_admin, stubs)
    second = FakeAdmin()
    second.start()
    try:
        serve(second, stubs)
        first_mock, second_mock = control(mock_admin), MockControl("users", second.url, timeout=5)
        first_mock.answer("getCart", 404)
        second_mock.answer("getCart", 404)
        mock_admin.statuses["POST"] = 500
        with pytest.raises(MockError, match="mock 'carts' answered POST"):
            Mocks({"carts": first_mock, "users": second_mock}).finish()
        assert second.paths("POST") == ["/__admin/scenarios/reset"]  # still reset
    finally:
        second.stop()


# --- the checks on what a run is given ---


@pytest.mark.parametrize(
    ("name", "url", "message"),
    [
        ("", "http://127.0.0.1:1", "invalid mock name"),
        ("bad name", "http://127.0.0.1:1", "invalid mock name"),
        ("-x", "http://127.0.0.1:1", "invalid mock name"),
        ("carts", "http://[bad", "invalid URL for mock 'carts'"),
        ("carts", "ftp://127.0.0.1:21", "expected http"),
        ("carts", "http://", "expected http"),
        ("carts", "127.0.0.1:8080", "expected http"),
        ("carts", "http://999.1.1.1:8080", "invalid URL for mock 'carts'"),  # httpx refuses it
    ],
)
def test_an_unusable_mock_is_refused_before_a_run(name: str, url: str, message: str) -> None:
    with pytest.raises(MockError, match=message):
        check_mock(name, url)


def test_a_control_for_a_url_the_client_refuses_is_a_mock_error() -> None:
    with pytest.raises(MockError, match="invalid URL for mock 'carts'"):
        MockControl("carts", "http://999.1.1.1:8080", timeout=1)


def test_a_url_with_a_password_is_not_echoed_when_refused() -> None:
    with pytest.raises(MockError) as caught:
        check_mock("carts", "http://u:hunter2@[bad")
    assert "hunter2" not in str(caught.value)


def test_a_usable_mock_passes() -> None:
    check_mock("carts_2-a", "https://mock.example:9090/prefix")


def test_execute_suite_refuses_an_unusable_mock(server: str, tmp_path: Path) -> None:
    (tmp_path / "test_a.py").write_text("def test_a():\n    pass\n")
    with pytest.raises(ExecuteError, match="invalid mock name"):
        execute_suite(tmp_path, server, mocks={"bad name": "http://127.0.0.1:1"})


# --- through a suite ---


def write_suite(tmp_path: Path, source: str) -> Path:
    suite = tmp_path / "suite"
    suite.mkdir()
    (suite / "test_api.py").write_text(source)
    return suite


def test_a_test_switches_a_mock_and_it_is_reset_when_the_test_ends(
    server: str, tmp_path: Path, mock_admin: FakeAdmin, stubs: list[dict[str, Any]]
) -> None:
    serve(mock_admin, stubs)
    suite = write_suite(
        tmp_path,
        """
def test_error_path(hermetic_client, hermetic_mocks):
    hermetic_mocks["carts"].answer("getCart", 404)
    assert hermetic_client.get("/ok").status_code == 200

def test_plain(hermetic_client):
    assert hermetic_client.get("/ok").status_code == 200
""",
    )
    results = execute_suite(suite, server, mocks={"carts": mock_admin.url})
    assert {r.status for r in results} == {"passed"}
    scenario = scenario_of(stubs, "getCart")
    assert [(m, p) for m, p, _ in mock_admin.calls] == [
        ("GET", "/__admin/mappings"),
        ("PUT", f"/__admin/scenarios/{scenario}/state"),
        ("POST", "/__admin/scenarios/reset"),  # one reset, for the one test that switched
    ]


def test_the_listing_is_read_once_for_a_whole_run(
    server: str, tmp_path: Path, mock_admin: FakeAdmin, stubs: list[dict[str, Any]]
) -> None:
    serve(mock_admin, stubs)
    suite = write_suite(
        tmp_path,
        """
def test_one(hermetic_mocks):
    hermetic_mocks["carts"].answer("getCart", 404)

def test_two(hermetic_mocks):
    hermetic_mocks["carts"].answer("getCart", 409)
""",
    )
    results = execute_suite(suite, server, mocks={"carts": mock_admin.url})
    assert {r.status for r in results} == {"passed"}
    assert mock_admin.paths("GET") == ["/__admin/mappings"]  # not once per test
    assert len(mock_admin.paths("PUT")) == 2 and len(mock_admin.paths("POST")) == 2


def test_a_failing_test_still_resets_the_mock(
    server: str, tmp_path: Path, mock_admin: FakeAdmin, stubs: list[dict[str, Any]]
) -> None:
    serve(mock_admin, stubs)
    suite = write_suite(
        tmp_path,
        """
def test_fails(hermetic_client, hermetic_mocks):
    hermetic_mocks["carts"].answer("getCart", 409)
    assert hermetic_client.get("/ok").status_code == 500
""",
    )
    (result,) = execute_suite(suite, server, mocks={"carts": mock_admin.url})
    assert result.status == "failed"
    assert mock_admin.paths("POST") == ["/__admin/scenarios/reset"]


def test_a_reset_that_fails_is_an_error_on_that_test(
    server: str, tmp_path: Path, mock_admin: FakeAdmin, stubs: list[dict[str, Any]]
) -> None:
    serve(mock_admin, stubs)
    mock_admin.statuses["POST"] = 500
    suite = write_suite(
        tmp_path,
        """
def test_switches(hermetic_mocks):
    hermetic_mocks["carts"].answer("getCart", 404)
""",
    )
    (result,) = execute_suite(suite, server, mocks={"carts": mock_admin.url})
    assert result.status == "error"
    assert result.message is not None and "answered POST /__admin/scenarios/reset" in result.message


def test_a_test_cannot_reach_a_mock_itself(
    server: str, tmp_path: Path, mock_admin: FakeAdmin
) -> None:
    suite = write_suite(
        tmp_path,
        f"""
def test_direct(hermetic_client):
    hermetic_client.get("{mock_admin.url}/__admin/mappings")
""",
    )
    (result,) = execute_suite(suite, server, mocks={"carts": mock_admin.url})
    assert result.status == "failed"
    assert result.message is not None and "tests may only call the target" in result.message
    assert mock_admin.calls == []


def test_a_test_naming_a_mock_that_was_not_given_fails_with_the_flag_to_use(
    server: str, tmp_path: Path
) -> None:
    suite = write_suite(
        tmp_path,
        """
def test_no_mock(hermetic_mocks):
    hermetic_mocks["carts"].answer("getCart", 404)
""",
    )
    (result,) = execute_suite(suite, server)
    assert result.status == "failed"
    assert result.message is not None
    assert "no mock named 'carts'; known: none" in result.message
    assert "--mock NAME=URL" in result.message
