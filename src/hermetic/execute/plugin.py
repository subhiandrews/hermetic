"""Pytest plugin that runs a suite against a target and records what happened."""

from __future__ import annotations

import threading
import time
from collections.abc import Iterator
from typing import Any
from urllib.parse import urlsplit

import httpx
import pytest

from hermetic.execute.mocks import Declared, MockControl, Mocks
from hermetic.history.models import Exchange, TestResult, TestStatus
from hermetic.redact import (
    MAX_BODY_CHARS,
    capture_body,
    mask_secrets,
    redact_body,
    redact_headers,
    redact_url,
    secret_values,
    url_secrets,
)
from hermetic.report.curl import build_curl

MAX_MESSAGE_CHARS = 4000


class OriginError(Exception):
    """A test tried to reach a host other than the target. Tests may only call the target."""


def origin_of(url: str) -> tuple[str, str, int]:
    parts = urlsplit(url)
    scheme = parts.scheme.lower()
    port = parts.port or {"http": 80, "https": 443}.get(scheme, 0)
    return scheme, (parts.hostname or "").lower(), port


class _Recording:
    """Collects exchanges for the test that is running and enforces the origin allowlist."""

    def __init__(self, allowed: set[tuple[str, str, int]]) -> None:
        self.allowed = allowed
        self.exchanges: list[Exchange] = []
        self._lock = threading.Lock()  # a test may share the client across threads
        self.secrets: set[str] = set()  # raw credential values seen, to mask failure text

    def on_request(self, request: httpx.Request) -> None:
        if origin_of(str(request.url)) not in self.allowed:
            raise OriginError(
                f"refusing {request.method} {redact_url(str(request.url))}: "
                "tests may only call the target"
            )
        try:
            body: bytes | None = request.content
        except httpx.RequestNotRead:
            body = None
        self._learn(secret_values(request.headers) | url_secrets(str(request.url)))
        request.extensions["hermetic_started"] = time.perf_counter()
        known = self.known()
        captured = capture_body(body or b"", known)
        self._learn(known)  # includes what capture_body found in this body
        exchange = Exchange(
            method=request.method,
            url=mask_secrets(redact_url(str(request.url)), known),
            request_headers=self._headers(request.headers),
            request_body=captured.text,
            request_body_complete=captured.complete,
            status_code=None,
            response_headers={},
            response_body=None,
            duration_ms=0.0,
        )
        with self._lock:
            # The response hook finds its exchange through the request, not by position.
            request.extensions["hermetic_index"] = len(self.exchanges)
            self.exchanges.append(exchange)

    def _learn(self, values: set[str]) -> None:
        with self._lock:
            self.secrets |= values

    def known(self) -> set[str]:
        """A snapshot: another thread may add secrets while this one is masking."""
        with self._lock:
            return set(self.secrets)

    def final_exchanges(self) -> list[Exchange]:
        """The exchanges re-masked with every credential the test eventually saw.

        A credential learned late (a token read from one response, sent in the next request)
        was unknown when the earlier exchanges were recorded.
        """
        known = self.known()
        return [
            e.model_copy(
                update={
                    "url": mask_secrets(e.url, known),
                    "request_headers": {
                        k: mask_secrets(v, known) for k, v in e.request_headers.items()
                    },
                    "request_body": None
                    if e.request_body is None
                    else mask_secrets(e.request_body, known),
                    "response_headers": {
                        k: mask_secrets(v, known) for k, v in e.response_headers.items()
                    },
                    "response_body": None
                    if e.response_body is None
                    else mask_secrets(e.response_body, known),
                }
            )
            for e in self.exchanges
        ]

    def _headers(self, headers: httpx.Headers) -> dict[str, str]:
        """Headers for storage: sensitive ones masked, then any credential already seen."""
        known = self.known()
        return {name: mask_secrets(value, known) for name, value in redact_headers(headers).items()}

    def on_response(self, response: httpx.Response) -> None:
        self._learn(secret_values(response.headers))
        # Read only a body that is announced and small: reading here would otherwise make
        # client.stream() block on an endless or huge response.
        # A compressed body is skipped: reading decodes it, so its size is not bounded.
        length = response.headers.get("content-length", "")
        encoding = response.headers.get("content-encoding", "identity").lower()
        body = None
        if (
            length.isascii()
            and length.isdigit()
            and int(length) <= MAX_BODY_CHARS
            and encoding == "identity"
        ):
            response.read()
            known = self.known()
            body = redact_body(response.content, known)
            self._learn(known)
        started = response.request.extensions["hermetic_started"]
        index = response.request.extensions["hermetic_index"]
        pending = self.exchanges[index]
        self.exchanges[index] = pending.model_copy(
            update={
                "status_code": response.status_code,
                "response_headers": self._headers(response.headers),
                "response_body": body,
                "duration_ms": (time.perf_counter() - started) * 1000,
            }
        )


class HermeticPlugin:
    """Gives tests an `hermetic_client` fixture and turns pytest reports into TestResults."""

    def __init__(self, target: str, *, timeout: float, mocks: dict[str, str] | None = None) -> None:
        self._target = target
        self._timeout = timeout
        self._mocks = mocks or {}
        self._listings: dict[str, Declared] = {}  # what each mock declares, read once per run
        self._unreset: set[str] = set()  # mocks a test switched and that could not be reset
        # Only the target: the mocks are switched by this plugin (see `hermetic_mocks`), never
        # called by a test.
        self._allowed = {origin_of(target)}
        self._recordings: dict[str, _Recording] = {}
        self._operations: dict[str, str] = {}
        self._durations: dict[str, float] = {}
        self.results: list[TestResult] = []
        self._position: dict[str, int] = {}  # nodeid -> index in results
        self.collection_failed = False

    def pytest_configure(self, config: pytest.Config) -> None:
        config.addinivalue_line(
            "markers", "operation(operation_id): the OpenAPI operationId this test exercises"
        )

    @pytest.fixture
    def hermetic_client(self, request: pytest.FixtureRequest) -> Iterator[httpx.Client]:
        recording = self._recordings.setdefault(request.node.nodeid, _Recording(self._allowed))
        with httpx.Client(
            base_url=self._target,
            timeout=self._timeout,
            follow_redirects=False,
            # Identity keeps bodies readable and bounded, so credentials in them can be
            # collected and masked; a compressed body is never decoded by the hook.
            headers={"Accept-Encoding": "identity"},
            event_hooks={"request": [recording.on_request], "response": [recording.on_response]},
        ) as client:
            yield client

    @pytest.fixture
    def hermetic_mocks(self) -> Iterator[Mocks]:
        """The mocks given with `--mock`; `hermetic_mocks["carts"].answer("getCart", 404)`.

        Whatever a test switched is reset when it ends, also when it failed. The test never
        gets a client or a URL for a mock; only this plugin calls them.
        """
        mocks = Mocks(
            {
                name: MockControl(name, url, timeout=self._timeout, listings=self._listings)
                for name, url in self._mocks.items()
            }
        )
        try:
            yield mocks
        finally:
            try:
                mocks.finish()
            finally:
                self._unreset |= mocks.pending()  # retried before the next test

    def pytest_runtest_setup(self) -> None:
        """Put back a mock whose reset failed, so one failure does not spoil the tests after it.

        A retry that fails too is an error on the test about to run, not a silent leftover.
        """
        for name in sorted(self._unreset):
            control = MockControl(name, self._mocks[name], timeout=self._timeout)
            try:
                control.reset()
            finally:
                control.close()
            self._unreset.discard(name)

    def pytest_collection_modifyitems(self, items: list[pytest.Item]) -> None:
        for item in items:
            marker = item.get_closest_marker("operation")
            if marker is not None and marker.args:
                self._operations[item.nodeid] = str(marker.args[0])

    def pytest_runtest_logreport(self, report: pytest.TestReport) -> None:
        nodeid = report.nodeid
        self._durations[nodeid] = self._durations.get(nodeid, 0.0) + report.duration * 1000
        status = _status_of(report)
        if status is None:
            return
        position = self._position.get(nodeid)
        if position is not None:
            existing = self.results[position]
            # Only a teardown failure can settle a test twice.
            teardown = self._masked(nodeid, report)
            if existing.status == "failed":  # keep the assertion text, add the teardown error
                teardown = f"{existing.message}\n\nteardown error: {teardown}"[:MAX_MESSAGE_CHARS]
            if existing.status != "error":  # an earlier setup error keeps its own message
                self._replace(position, status="error", message=teardown)
            return
        self._position[nodeid] = len(self.results)
        self.results.append(self._result(nodeid, status, self._masked(nodeid, report)))

    def pytest_collectreport(self, report: pytest.CollectReport) -> None:
        if report.failed:
            self.collection_failed = True
            self.results.append(
                TestResult(
                    test_id=report.nodeid,
                    operation_id=None,
                    status="error",
                    duration_ms=0.0,
                    message=_message_of(report, url_secrets(self._target)),
                    exchanges=[],
                    curl=None,
                )
            )

    def _masked(self, nodeid: str, report: pytest.TestReport) -> str | None:
        """The report's message with any credential this test sent or received masked."""
        recording = self._recordings.get(nodeid)
        return _message_of(report, recording.known() if recording else set())

    def _result(self, nodeid: str, status: TestStatus, message: str | None) -> TestResult:
        recording = self._recordings.get(nodeid, _Recording(self._allowed))
        exchanges = recording.final_exchanges()
        return TestResult(
            # Parametrize ids are used verbatim, so one can carry a credential the test also sent.
            test_id=mask_secrets(nodeid, recording.known()),
            operation_id=self._operations.get(nodeid),
            status=status,
            duration_ms=self._durations.get(nodeid, 0.0),
            message=message,
            exchanges=exchanges,
            curl=_repro(status, exchanges),
        )

    def _replace(self, index: int, *, status: TestStatus, message: str | None) -> None:
        old = self.results[index]
        self.results[index] = old.model_copy(
            update={
                "status": status,
                "message": message,
                "duration_ms": self._durations.get(old.test_id, old.duration_ms),
                "curl": _repro(status, old.exchanges),
            }
        )


def _status_of(report: pytest.TestReport) -> TestStatus | None:
    """The status this phase settles, or None if the test is still going."""
    if report.when == "call":
        if report.failed:
            return "failed"
        return "skipped" if report.skipped else "passed"
    if report.when == "setup":
        if report.failed:
            return "error"
        return "skipped" if report.skipped else None
    return "error" if report.failed else None  # teardown


def _message_of(
    report: pytest.TestReport | pytest.CollectReport, secrets: set[str] | None = None
) -> str | None:
    """Failure text for a failed report, the reason for a skipped one, else None."""
    longrepr: Any = report.longrepr
    if report.skipped:
        text = str(longrepr[2]) if isinstance(longrepr, tuple) and len(longrepr) == 3 else None
    elif report.failed:
        crash = getattr(longrepr, "reprcrash", None)
        text = str(crash.message) if crash is not None else str(longrepr)
    else:
        text = None
    if text is None:
        return None
    # Mask before cutting: a credential that straddles the cut would no longer match.
    return mask_secrets(text, secrets or set())[:MAX_MESSAGE_CHARS]


def _repro(status: TestStatus, exchanges: list[Exchange]) -> str | None:
    """A curl for the last request the test made, when the test did not pass."""
    if status in ("failed", "error") and exchanges:
        return build_curl(exchanges[-1])
    return None
