from __future__ import annotations

import json
import logging
import re
import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from hermetic.history import (
    FileRunStore,
    RunNotFoundError,
    RunRecord,
    RunStoreError,
    RunSummary,
    ServiceSummary,
    SuiteChange,
    summarize,
)
from hermetic.history.models import RECENT_RUNS
from hermetic.web import LOOPBACK_HOSTS, create_app
from hermetic.web.app import (
    DEFAULT_RUN_LIMIT,
    MAX_INGEST_BYTES,
    MAX_RUN_LIMIT,
    STATIC_DIR,
    hostname,
)
from tests.factories import make_record, make_result

SECRET = "secret-marker"


Summaries = list[RunSummary]  # `list` is a method name inside the fake stores
Services = list[ServiceSummary]


class MemoryStore:
    """A RunStore that is not the file store: the web UI must work over the protocol alone."""

    def __init__(self, *records: RunRecord) -> None:
        self._records = {(record.service, record.run_id): record for record in records}

    def save(self, record: RunRecord) -> None:
        self._records[(record.service, record.run_id)] = record

    def get(self, service: str, run_id: str) -> RunRecord:
        record = self._records.get((service, run_id))
        if record is None:
            raise RunNotFoundError(run_id)
        return record

    def list(self, service: str | None = None) -> Summaries:
        summaries = [
            summarize(record)
            for record in self._records.values()
            if service is None or record.service == service
        ]
        return sorted(summaries, key=lambda summary: summary.started_at, reverse=True)

    def services(self) -> Services:
        found: dict[str, list[RunSummary]] = {}
        for summary in self.list():
            found.setdefault(summary.service, []).append(summary)
        return [
            ServiceSummary(service=n, runs=len(r), latest=r[0], recent=r[:RECENT_RUNS])
            for n, r in sorted(found.items())
        ]


class BrokenStore:
    """Every read fails, with a message that quotes stored data."""

    def save(self, record: RunRecord) -> None:
        raise RunStoreError(SECRET)

    def get(self, service: str, run_id: str) -> RunRecord:
        raise RunStoreError(f"run {run_id} is corrupt: {SECRET}")

    def list(self, service: str | None = None) -> Summaries:
        raise RunStoreError(f"index is corrupt: {SECRET}")

    def services(self) -> Services:
        raise RunStoreError(f"index is corrupt: {SECRET}")


OLD = make_record("old", started_at="2026-01-01T00:00:00+00:00", results=[make_result("passed")])
NEW = make_record(
    "new",
    started_at="2026-01-02T00:00:00+00:00",
    results=[make_result("failed"), make_result("passed")],
    changes=[SuiteChange(kind="added", test_id="t::new", reason="a new operation in the spec")],
)


@pytest.fixture
def client() -> TestClient:
    return TestClient(create_app(MemoryStore(OLD, NEW)), base_url="http://localhost")


def test_runs_are_listed_newest_first_with_their_counts(client: TestClient) -> None:
    response = client.get("/api/runs")
    assert response.status_code == 200
    runs = response.json()
    assert [run["run_id"] for run in runs] == ["new", "old"]
    assert runs[0]["failed"] == 1 and runs[0]["passed"] == 1 and runs[0]["added"] == 1


def test_the_run_list_is_capped_and_the_cap_can_be_set() -> None:
    def started(index: int) -> str:  # strictly increasing, so a higher index is newer
        return f"2026-01-01T00:{index // 60:02d}:{index % 60:02d}+00:00"

    runs = [
        make_record(f"run-{index:03d}", started_at=started(index))
        for index in range(DEFAULT_RUN_LIMIT + 5)
    ]
    client = TestClient(create_app(MemoryStore(*runs)), base_url="http://localhost")
    everything = client.get("/api/runs").json()
    assert len(everything) == DEFAULT_RUN_LIMIT
    assert everything[0]["run_id"] == f"run-{DEFAULT_RUN_LIMIT + 4:03d}"  # the newest survive
    assert [run["run_id"] for run in client.get("/api/runs?limit=2").json()] == [
        "run-204",
        "run-203",
    ]
    assert client.get("/api/runs?limit=0").status_code == 422
    assert client.get(f"/api/runs?limit={MAX_RUN_LIMIT + 1}").status_code == 422


def test_the_page_sets_its_title_once_and_only_for_the_response_it_is_showing() -> None:
    # A superseded fetch that resolves last must not rename the tab, so the title is set next to
    # the check that drops stale responses, not inside the render functions.
    script = (STATIC_DIR / "app.js").read_text()
    assert script.count("document.title =") == 1
    assert script.index("document.title =") > script.index("current === navigation")


def test_the_pass_rate_never_reads_one_hundred_for_a_run_with_a_failure() -> None:
    script = (STATIC_DIR / "app.js").read_text()
    assert "Math.round(rate" not in script  # 1999 of 2000 rounds to 100 beside a red bar


def test_the_page_asks_for_the_same_limit_the_server_defaults_to() -> None:
    script = (STATIC_DIR / "app.js").read_text()
    match = re.search(r"const RUN_LIMIT = (\d+);", script)
    assert match is not None and int(match.group(1)) == DEFAULT_RUN_LIMIT


def test_a_run_comes_back_with_its_results_and_changes(client: TestClient) -> None:
    response = client.get("/api/services/default/runs/new")
    assert response.status_code == 200
    body = response.json()
    assert [result["status"] for result in body["results"]] == ["failed", "passed"]
    assert body["changes"][0]["reason"] == "a new operation in the spec"


def test_a_run_is_addressed_by_its_service_and_the_same_id_can_repeat() -> None:
    first = make_record("run-1", service="alpha", started_at="2026-01-01T00:00:00+00:00")
    second = make_record("run-1", service="beta", started_at="2026-01-02T00:00:00+00:00")
    client = TestClient(create_app(MemoryStore(first, second)), base_url="http://localhost")
    assert client.get("/api/services/alpha/runs/run-1").json()["service"] == "alpha"
    assert client.get("/api/services/gamma/runs/run-1").status_code == 404
    assert client.get("/api/services/Bad%20Name/runs/run-1").status_code == 404


def test_the_runs_of_every_service_are_listed_with_their_service() -> None:
    first = make_record("a-run", service="alpha", started_at="2026-01-01T00:00:00+00:00")
    second = make_record("b-run", service="beta", started_at="2026-01-02T00:00:00+00:00")
    client = TestClient(create_app(MemoryStore(first, second)), base_url="http://localhost")
    assert [r["service"] for r in client.get("/api/runs").json()] == ["beta", "alpha"]


def test_the_hosts_answered_to_by_default_are_loopback_as_a_host_header_writes_them() -> None:
    assert LOOPBACK_HOSTS == ("localhost", "127.0.0.1", "[::1]")


def test_the_page_links_runs_by_service_and_id() -> None:
    script = (STATIC_DIR / "app.js").read_text()
    assert "/api/services/" in script and "run.service" in script


def test_the_page_reads_only_the_service_routes() -> None:
    # A server that keeps the history of many services is addressed through /api/services, so the
    # page must not depend on the flat /api/runs alias.
    script = (STATIC_DIR / "app.js").read_text()
    assert "/api/runs" not in script
    assert "/api/services" in script


def test_the_page_keeps_the_route_patterns_for_the_list_a_service_and_a_run() -> None:
    # Only the source is checked: the page has no JavaScript test harness, so how the patterns
    # are tried is checked by hand in a browser until a smoke test exists.
    script = (STATIC_DIR / "app.js").read_text()
    assert r"#\/services\/([^/]+)\/runs\/([^/]+)" in script  # a run
    assert r"#\/services\/([^/]+)$" in script  # one service's history
    assert r"#\/runs\/([^/]+)\/([^/]+)" in script  # the older address of a run keeps working
    assert "renderServices" in script  # anything else is the services list


def test_the_service_listing_the_page_draws_carries_what_each_row_shows() -> None:
    first = make_record("a-1", service="alpha", started_at="2026-01-01T00:00:00+00:00")
    second = make_record("a-2", service="alpha", started_at="2026-01-02T00:00:00+00:00")
    other = make_record("b-1", service="beta", started_at="2026-01-03T00:00:00+00:00")
    client = TestClient(create_app(MemoryStore(first, second, other)), base_url="http://localhost")
    rows = {row["service"]: row for row in client.get("/api/services").json()}
    assert rows["alpha"]["runs"] == 2
    assert rows["alpha"]["latest"]["run_id"] == "a-2"
    assert rows["beta"]["latest"]["service"] == "beta"
    # the trend column is drawn from the listing itself: newest first, one request for all
    assert [r["run_id"] for r in rows["alpha"]["recent"]] == ["a-2", "a-1"]
    assert [r["run_id"] for r in rows["beta"]["recent"]] == ["b-1"]


def test_the_page_draws_as_many_runs_in_a_trend_as_the_server_sends() -> None:
    script = (STATIC_DIR / "app.js").read_text()
    match = re.search(r"const TREND_RUNS = (\d+);", script)
    assert match is not None and int(match.group(1)) == RECENT_RUNS


def test_the_file_store_serves_the_same_api(tmp_path: Path) -> None:
    store = FileRunStore(tmp_path / "runs")
    store.save(OLD)
    store.save(NEW)
    client = TestClient(create_app(store), base_url="http://localhost")
    assert [run["run_id"] for run in client.get("/api/runs").json()] == ["new", "old"]
    assert client.get("/api/services/default/runs/old").json()["run_id"] == "old"


@pytest.mark.parametrize("run_id", ["nope", "index", ".hidden"])
def test_an_unknown_or_malformed_run_id_is_not_found(tmp_path: Path, run_id: str) -> None:
    client = TestClient(create_app(FileRunStore(tmp_path / "runs")), base_url="http://localhost")
    response = client.get(f"/api/services/default/runs/{run_id}")
    assert response.status_code == 404
    assert response.json() == {"detail": "run not found"}


@pytest.mark.parametrize("run_id", ["..%2Fsecret", "a%2Fb", "..%5Csecret"])
def test_an_id_that_names_a_path_never_reaches_the_store(tmp_path: Path, run_id: str) -> None:
    # The router decodes `%2F` to a slash, so no route matches and the id is never used.
    (tmp_path / "secret.json").write_text("{}")
    client = TestClient(create_app(FileRunStore(tmp_path / "runs")), base_url="http://localhost")
    assert client.get(f"/api/services/default/runs/{run_id}").status_code == 404


def test_an_unreadable_store_is_a_500_that_leaks_nothing(caplog: pytest.LogCaptureFixture) -> None:
    client = TestClient(create_app(BrokenStore()), base_url="http://localhost")
    with caplog.at_level(logging.WARNING, logger="hermetic.web"):
        listing = client.get("/api/runs")
        detail = client.get("/api/services/default/runs/any")
    for response in (listing, detail):
        assert response.status_code == 500
        assert response.json() == {"detail": "the run history could not be read"}
        assert SECRET not in response.text
    # The operator still learns that it failed and how, but not what the stored record held.
    assert "run history could not be read (RunStoreError)" in caplog.text
    assert SECRET not in caplog.text


def test_an_unexpected_error_is_a_500_that_still_carries_the_security_headers(
    caplog: pytest.LogCaptureFixture,
) -> None:
    class Crashing(MemoryStore):
        def list(self, service: str | None = None) -> Summaries:
            raise RuntimeError(SECRET)

    client = TestClient(
        create_app(Crashing()), base_url="http://localhost", raise_server_exceptions=False
    )
    with caplog.at_level(logging.WARNING, logger="hermetic.web"):
        response = client.get("/api/runs")
    assert response.status_code == 500 and SECRET not in response.text
    assert SECRET not in caplog.text and "RuntimeError at test_web.py:" in caplog.text
    assert "in list" in caplog.text  # the function it was raised in
    assert response.headers["Content-Security-Policy"].startswith("default-src 'self'")
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["Cache-Control"] == "no-store"


def test_a_corrupt_run_file_is_a_500_without_its_content(tmp_path: Path) -> None:
    root = tmp_path / "runs"
    (root / "default").mkdir(parents=True)
    (root / "default" / "bad.json").write_text(f'{{"{SECRET}": 1}}')
    client = TestClient(create_app(FileRunStore(root)), base_url="http://localhost")
    response = client.get("/api/services/default/runs/bad")
    assert response.status_code == 500
    assert SECRET not in response.text


@pytest.mark.parametrize("path", ["/", "/api/runs", "/static/app.js"])
def test_an_unexpected_host_header_is_refused(client: TestClient, path: str) -> None:
    # A web page that rebinds its own domain to 127.0.0.1 sends that domain as the host.
    response = client.get(path, headers={"host": "evil.example"})
    assert response.status_code == 421
    assert "Content-Security-Policy" in response.headers  # refusals carry the headers too


@pytest.mark.parametrize(
    "host", ["localhost", "localhost:8000", "LOCALHOST:8000", "127.0.0.1:9", "[::1]:8000", "[::1]"]
)
def test_loopback_host_headers_are_accepted(client: TestClient, host: str) -> None:
    assert client.get("/api/runs", headers={"host": host}).status_code == 200


@pytest.mark.parametrize("entry", ["", " ", ":8000"])
def test_an_empty_allowed_host_is_refused_rather_than_matching_a_missing_header(
    entry: str,
) -> None:
    with pytest.raises(ValueError, match="cannot be empty"):
        create_app(MemoryStore(), ["localhost", entry])


def test_an_unbracketed_ipv6_allowed_host_matches_its_bracketed_header() -> None:
    served = TestClient(create_app(MemoryStore(), ["fd00::7"]))
    assert served.get("/api/runs", headers={"host": "[fd00::7]:8000"}).status_code == 200


def test_the_accepted_hosts_can_be_set_or_opened_up() -> None:
    store = MemoryStore()
    named = TestClient(create_app(store, ["example.test"]))
    assert named.get("/api/runs", headers={"host": "example.test:8080"}).status_code == 200
    with_port = TestClient(create_app(store, ["Example.Test:8080"]))  # the port is not compared
    assert with_port.get("/api/runs", headers={"host": "example.test:9999"}).status_code == 200
    assert with_port.get("/api/runs", headers={"host": "evil.example:8080"}).status_code == 421
    assert named.get("/api/runs", headers={"host": "localhost"}).status_code == 421
    anything = TestClient(create_app(store, ["*"]))
    assert anything.get("/api/runs", headers={"host": "whatever.example"}).status_code == 200


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        ("", ""),
        ("localhost", "localhost"),
        (" Example.Test:8080 ", "example.test"),
        ("[::1]:8000", "[::1]"),
        ("[fe80::1]", "[fe80::1]"),
    ],
)
def test_hostname_drops_the_port_and_keeps_ipv6_brackets(header: str, expected: str) -> None:
    assert hostname(header) == expected


@pytest.mark.parametrize(
    "path", ["/", "/api/runs", "/api/services/default/runs/new", "/static/app.js"]
)
def test_every_response_carries_the_security_headers(client: TestClient, path: str) -> None:
    headers = client.get(path).headers
    policy = headers["Content-Security-Policy"]
    assert "default-src 'self'" in policy and "frame-ancestors 'none'" in policy
    assert "unsafe-inline" not in policy and "unsafe-eval" not in policy
    assert headers["X-Content-Type-Options"] == "nosniff"
    assert headers["X-Frame-Options"] == "DENY"
    assert headers["Referrer-Policy"] == "no-referrer"


def test_only_the_api_is_marked_uncacheable(client: TestClient) -> None:
    assert client.get("/api/runs").headers["Cache-Control"] == "no-store"
    assert "Cache-Control" not in client.get("/static/style.css").headers


def test_the_page_and_its_assets_are_served(client: TestClient) -> None:
    page = client.get("/")
    assert page.status_code == 200 and "text/html" in page.headers["content-type"]
    assert '<script type="module" src="/static/app.js">' in page.text
    assert "javascript" in client.get("/static/app.js").headers["content-type"]
    assert "text/css" in client.get("/static/style.css").headers["content-type"]
    # Without a page icon every browser asks for /favicon.ico, which is a 404 in its console.
    assert 'rel="icon"' in page.text
    assert "image/svg+xml" in client.get("/static/favicon.svg").headers["content-type"]


def test_the_interactive_docs_are_off_but_the_schema_is_there(client: TestClient) -> None:
    assert client.get("/docs").status_code == 404
    assert client.get("/redoc").status_code == 404
    assert "/api/runs" in client.get("/api/openapi.json").json()["paths"]


def test_the_ui_is_read_only(client: TestClient) -> None:
    for method in ("post", "put", "delete", "patch"):
        response = getattr(client, method)("/api/runs")
        assert response.status_code == 405
        assert "Content-Security-Policy" in response.headers


def test_the_page_never_builds_markup_from_run_data() -> None:
    script = (STATIC_DIR / "app.js").read_text()
    for forbidden in (
        "innerHTML",
        "outerHTML",
        "insertAdjacentHTML",
        "document.write",
        "eval(",
        "new Function",
    ):
        assert forbidden not in script
    page = (STATIC_DIR / "index.html").read_text()
    assert not re.search(r"<script(?![^>]*\bsrc=)", page), "an inline script"
    assert not re.search(r"\son[a-z]+\s*=", page), "an inline event handler"
    assert not re.search(r"style\s*=", page), "an inline style"
    assert "http://" not in page and "https://" not in page, "a resource from another origin"


# --- the service listings and run ingest ---

TOKEN = "ingest-token-9c1"
BEARER = {"Authorization": f"Bearer {TOKEN}"}


def ingesting(store: MemoryStore | None = None, token: str | None = TOKEN) -> TestClient:
    app = create_app(store or MemoryStore(), ingest_token=token)
    return TestClient(app, base_url="http://localhost")


def post_run(client: TestClient, record: RunRecord, headers: dict[str, str] | None = None) -> Any:
    return client.post(
        f"/api/services/{record.service}/runs", content=record.model_dump_json(), headers=headers
    )


def test_the_services_and_their_runs_are_listed(client: TestClient) -> None:
    (only,) = client.get("/api/services").json()
    assert only["service"] == "default" and only["runs"] == 2 and only["latest"]["run_id"] == "new"
    assert [r["run_id"] for r in client.get("/api/services/default/runs").json()] == ["new", "old"]
    assert [r["run_id"] for r in client.get("/api/services/default/runs?limit=1").json()] == ["new"]
    assert client.get("/api/services/other/runs").json() == []
    assert client.get("/api/services/default/runs?limit=0").status_code == 422


def test_a_run_is_accepted_with_the_token_and_can_be_read_back() -> None:
    store = MemoryStore()
    client = ingesting(store)
    record = make_record("r1", service="alpha", results=[make_result("passed")])
    response = post_run(client, record, headers=BEARER)
    assert response.status_code == 201
    assert response.json()["run_id"] == "r1" and response.json()["passed"] == 1
    assert client.get("/api/services/alpha/runs/r1").json()["service"] == "alpha"
    assert store.get("alpha", "r1") == record


def test_ingest_is_off_unless_a_token_is_configured() -> None:
    for token in (None, ""):
        client = ingesting(token=token)
        response = post_run(client, make_record("r1"), headers=BEARER)
        assert response.status_code == 403
        assert "HERMETIC_API_TOKEN" in response.text
        assert client.get("/api/services").json() == []  # and nothing was stored


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"Authorization": "Bearer wrong"},
        {"Authorization": TOKEN},
        {"Authorization": f"Basic {TOKEN}"},
        {"Authorization": "Bearer "},
        {"Authorization": f"Bearer {TOKEN} "},
    ],
)
def test_a_missing_or_wrong_token_is_refused_and_nothing_is_stored(
    headers: dict[str, str],
) -> None:
    client = ingesting()
    response = post_run(client, make_record("r1"), headers=headers)
    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"] == "Bearer"
    assert client.get("/api/services").json() == []


def test_the_token_is_checked_before_the_body_is_read() -> None:
    # An unauthenticated client must not make the server read (or parse) a large body.
    client = ingesting()
    big = b"x" * (MAX_INGEST_BYTES + 1)
    assert client.post("/api/services/alpha/runs", content=big).status_code == 401


def test_a_scheme_is_case_insensitive_but_the_token_is_not() -> None:
    client = ingesting()
    ok = post_run(client, make_record("r1"), headers={"Authorization": f"bearer {TOKEN}"})
    assert ok.status_code == 201
    bad = post_run(client, make_record("r2"), headers={"Authorization": f"Bearer {TOKEN.upper()}"})
    assert bad.status_code == 401


def test_a_run_for_another_service_than_the_address_is_refused() -> None:
    client = ingesting()
    record = make_record("r1", service="alpha")
    response = client.post(
        "/api/services/beta/runs", content=record.model_dump_json(), headers=BEARER
    )
    assert response.status_code == 422 and "does not match" in response.text
    assert client.get("/api/services").json() == []


@pytest.mark.parametrize("run_id", ["index", "Index", "../x", ".hidden", "a/b", ""])
def test_a_run_id_the_store_cannot_use_is_a_client_error_not_a_server_error(run_id: str) -> None:
    client = ingesting()
    body = make_record("r1").model_dump_json().replace('"run_id":"r1"', f'"run_id":"{run_id}"')
    response = client.post("/api/services/default/runs", content=body, headers=BEARER)
    assert response.status_code == 422 and "run id" in response.text
    assert client.get("/api/services").json() == []


@pytest.mark.parametrize(
    "body", ["not json", "[]", "{}", '{"run_id": 1}', '{"service": "Bad Name"}', "\xff"]
)
def test_a_body_that_is_not_a_run_is_refused_without_echoing_it(body: str) -> None:
    client = ingesting()
    marked = body if body != "{}" else f'{{"{SECRET}": 1}}'
    response = client.post("/api/services/default/runs", content=marked, headers=BEARER)
    assert response.status_code == 422 and SECRET not in response.text
    assert isinstance(response.json()["detail"], list)  # where and what, nothing quoted


def test_a_value_the_model_rejects_is_not_quoted_back() -> None:
    client = ingesting()
    record = make_record("r1").model_dump(mode="json")
    record["target"] = None
    record["started_at"] = SECRET * 3
    record["spec_hash"] = {SECRET: 1}
    response = client.post("/api/services/default/runs", content=json.dumps(record), headers=BEARER)
    assert response.status_code == 422 and SECRET not in response.text


def test_a_dict_key_in_the_body_is_not_echoed_in_the_error_location() -> None:
    client = ingesting()
    record = make_record("r1", results=[make_result("passed")]).model_dump(mode="json")
    record["doc_hashes"] = {SECRET: 1}  # invalid value under a client-chosen key
    record["results"][0]["exchanges"] = [
        {"method": "GET", "url": "u", "request_headers": {SECRET + "2": 1}}
    ]
    response = client.post("/api/services/default/runs", content=json.dumps(record), headers=BEARER)
    assert response.status_code == 422 and SECRET not in response.text
    locations = [problem["loc"] for problem in response.json()["detail"]]
    assert ["doc_hashes", "*"] in locations  # where, without the key
    assert ["results", 0, "exchanges", 0, "request_headers", "*"] in locations  # positions stay


def test_a_body_over_the_limit_is_refused_by_its_declared_length() -> None:
    client = ingesting()
    big = b"x" * (MAX_INGEST_BYTES + 1)
    response = client.post("/api/services/default/runs", content=big, headers=BEARER)
    assert response.status_code == 413 and "too large" in response.text
    assert response.headers["Content-Security-Policy"]  # the headers are on error replies too


def test_a_body_over_the_limit_is_refused_while_streaming_when_no_length_is_declared() -> None:
    client = ingesting()

    def chunks() -> Iterator[bytes]:
        for _ in range(MAX_INGEST_BYTES // 65_536 + 2):
            yield b"x" * 65_536

    response = client.post("/api/services/default/runs", content=chunks(), headers=BEARER)
    assert response.status_code == 413


def test_a_run_just_under_the_limit_is_accepted() -> None:
    client = ingesting()
    padding = "x" * (MAX_INGEST_BYTES - 4_096)
    record = make_record("r1", doc_hashes={"padding": padding})
    assert post_run(client, record, headers=BEARER).status_code == 201


def test_a_run_replaces_an_earlier_one_with_the_same_id() -> None:
    client = ingesting()
    post_run(client, make_record("r1", target="http://first"), headers=BEARER)
    post_run(client, make_record("r1", target="http://second"), headers=BEARER)
    assert client.get("/api/services/default/runs/r1").json()["target"] == "http://second"
    assert len(client.get("/api/services/default/runs").json()) == 1


def test_a_store_that_cannot_save_is_a_500_that_leaks_nothing(
    caplog: pytest.LogCaptureFixture,
) -> None:
    client = TestClient(
        create_app(BrokenStore(), ingest_token=TOKEN),
        base_url="http://localhost",
    )
    with caplog.at_level(logging.WARNING, logger="hermetic.web"):
        response = post_run(client, make_record("r1"), headers=BEARER)
    assert response.status_code == 500 and SECRET not in response.text
    assert "could not be saved" in response.text  # not "could not be read"
    assert "run could not be saved (RunStoreError)" in caplog.text and SECRET not in caplog.text


def test_concurrent_ingests_are_saved_one_at_a_time() -> None:
    class SlowStore(MemoryStore):
        active = 0
        most = 0

        def save(self, record: RunRecord) -> None:
            SlowStore.active += 1
            SlowStore.most = max(SlowStore.most, SlowStore.active)
            time.sleep(0.03)  # long enough for the other requests to arrive
            super().save(record)
            SlowStore.active -= 1

    store = SlowStore()
    client = ingesting(store)
    records = [make_record(f"r{i}") for i in range(6)]
    with ThreadPoolExecutor(max_workers=6) as pool:
        replies = list(pool.map(lambda record: post_run(client, record, headers=BEARER), records))
    assert [reply.status_code for reply in replies] == [201] * 6
    assert SlowStore.most == 1 and len(store.list()) == 6


def test_ingest_replies_carry_the_security_headers_and_the_host_check_applies() -> None:
    client = ingesting()
    response = post_run(client, make_record("r1"), headers=BEARER)
    assert response.headers["Cache-Control"] == "no-store"
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    evil = post_run(client, make_record("r2"), headers={**BEARER, "host": "evil.example"})
    assert evil.status_code == 421
    assert client.get("/api/services/default/runs/r2").status_code == 404
