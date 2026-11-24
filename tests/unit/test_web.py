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
    SuiteChange,
    summarize,
)
from hermetic.web import create_app
from hermetic.web.app import STATIC_DIR, hostname
from tests.factories import make_record, make_result

SECRET = "secret-marker"


class MemoryStore:
    """A RunStore that is not the file store: the web UI must work over the protocol alone."""

    def __init__(self, *records: RunRecord) -> None:
        self._records = {record.run_id: record for record in records}

    def save(self, record: RunRecord) -> None:
        self._records[record.run_id] = record

    def get(self, run_id: str) -> RunRecord:
        try:
            return self._records[run_id]
        except KeyError:
            raise RunNotFoundError(run_id) from None

    def list(self) -> list[RunSummary]:
        summaries = [summarize(record) for record in self._records.values()]
        return sorted(summaries, key=lambda summary: summary.started_at, reverse=True)


class BrokenStore:
    """Every read fails, with a message that quotes stored data."""

    def save(self, record: RunRecord) -> None:
        raise RunStoreError(SECRET)

    def get(self, run_id: str) -> RunRecord:
        raise RunStoreError(f"run {run_id} is corrupt: {SECRET}")

    def list(self) -> list[RunSummary]:
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


def test_a_run_comes_back_with_its_results_and_changes(client: TestClient) -> None:
    response = client.get("/api/runs/new")
    assert response.status_code == 200
    body = response.json()
    assert [result["status"] for result in body["results"]] == ["failed", "passed"]
    assert body["changes"][0]["reason"] == "a new operation in the spec"


def test_the_file_store_serves_the_same_api(tmp_path: Path) -> None:
    store = FileRunStore(tmp_path / "runs")
    store.save(OLD)
    store.save(NEW)
    client = TestClient(create_app(store), base_url="http://localhost")
    assert [run["run_id"] for run in client.get("/api/runs").json()] == ["new", "old"]
    assert client.get("/api/runs/old").json()["run_id"] == "old"


@pytest.mark.parametrize("run_id", ["nope", "index", ".hidden"])
def test_an_unknown_or_malformed_run_id_is_not_found(tmp_path: Path, run_id: str) -> None:
    client = TestClient(create_app(FileRunStore(tmp_path / "runs")), base_url="http://localhost")
    response = client.get(f"/api/runs/{run_id}")
    assert response.status_code == 404
    assert response.json() == {"detail": "run not found"}


@pytest.mark.parametrize("run_id", ["..%2Fsecret", "a%2Fb", "..%5Csecret"])
def test_an_id_that_names_a_path_never_reaches_the_store(tmp_path: Path, run_id: str) -> None:
    # The router decodes `%2F` to a slash, so no route matches and the id is never used.
    (tmp_path / "secret.json").write_text("{}")
    client = TestClient(create_app(FileRunStore(tmp_path / "runs")), base_url="http://localhost")
    assert client.get(f"/api/runs/{run_id}").status_code == 404


def test_an_unreadable_store_is_a_500_that_leaks_nothing(caplog: pytest.LogCaptureFixture) -> None:
    client = TestClient(create_app(BrokenStore()), base_url="http://localhost")
    with caplog.at_level(logging.WARNING, logger="hermetic.web"):
        listing = client.get("/api/runs")
        detail = client.get("/api/runs/any")
    for response in (listing, detail):
        assert response.status_code == 500
        assert response.json() == {"detail": "the run history could not be read"}
        assert SECRET not in response.text
    assert "run history could not be read" in caplog.text  # the operator still learns of it


def test_a_corrupt_run_file_is_a_500_without_its_content(tmp_path: Path) -> None:
    root = tmp_path / "runs"
    root.mkdir()
    (root / "bad.json").write_text(f'{{"{SECRET}": 1}}')
    client = TestClient(create_app(FileRunStore(root)), base_url="http://localhost")
    response = client.get("/api/runs/bad")
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


def test_the_accepted_hosts_can_be_set_or_opened_up() -> None:
    store = MemoryStore()
    named = TestClient(create_app(store, ["example.test"]))
    assert named.get("/api/runs", headers={"host": "example.test:8080"}).status_code == 200
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


@pytest.mark.parametrize("path", ["/", "/api/runs", "/api/runs/new", "/static/app.js"])
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
