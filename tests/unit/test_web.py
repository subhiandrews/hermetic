from __future__ import annotations

import logging
import re
from pathlib import Path

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
from hermetic.web import create_app
from hermetic.web.app import DEFAULT_RUN_LIMIT, MAX_RUN_LIMIT, STATIC_DIR, hostname
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
        summaries = [summarize(record) for record in self._records.values()]
        return sorted(summaries, key=lambda summary: summary.started_at, reverse=True)

    def services(self) -> Services:
        found: dict[str, list[RunSummary]] = {}
        for summary in self.list():
            found.setdefault(summary.service, []).append(summary)
        return [
            ServiceSummary(service=n, runs=len(r), latest=r[0]) for n, r in sorted(found.items())
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


def test_the_page_links_runs_by_service_and_id() -> None:
    script = (STATIC_DIR / "app.js").read_text()
    assert "/api/services/" in script and "run.service" in script


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
    assert SECRET not in caplog.text and "(RuntimeError)" in caplog.text
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
