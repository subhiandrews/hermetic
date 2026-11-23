"""Pytest plugin that runs a suite against a target and records what happened."""

from __future__ import annotations

import time
from collections.abc import Iterator
from typing import Any
from urllib.parse import urlsplit

import httpx
import pytest

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
        self.secrets |= secret_values(request.headers) | url_secrets(str(request.url))
        request.extensions["hermetic_started"] = time.perf_counter()
        captured = capture_body(body or b"", self.secrets)
        self.exchanges.append(
            Exchange(
                method=request.method,
                url=redact_url(str(request.url)),
                request_headers=self._headers(request.headers),
                request_body=captured.text,
                request_body_complete=captured.complete,
                status_code=None,
                response_headers={},
                response_body=None,
                duration_ms=0.0,
            )
        )

    def _headers(self, headers: httpx.Headers) -> dict[str, str]:
        """Headers for storage: sensitive ones masked, then any credential already seen."""
        return {
            name: mask_secrets(value, self.secrets)
            for name, value in redact_headers(headers).items()
        }

    def on_response(self, response: httpx.Response) -> None:
        self.secrets |= secret_values(response.headers)
        # Read only a body that is announced and small: reading here would otherwise make
        # client.stream() block on an endless or huge response.
        # A compressed body is skipped: reading decodes it, so its size is not bounded.
        length = response.headers.get("content-length", "")
        encoding = response.headers.get("content-encoding", "identity").lower()
        body = None
        if length.isdigit() and int(length) <= MAX_BODY_CHARS and encoding == "identity":
            response.read()
            body = redact_body(response.content, self.secrets)
        started = response.request.extensions["hermetic_started"]
        pending = self.exchanges[-1]
        self.exchanges[-1] = pending.model_copy(
            update={
                "status_code": response.status_code,
                "response_headers": self._headers(response.headers),
                "response_body": body,
                "duration_ms": (time.perf_counter() - started) * 1000,
            }
        )


class HermeticPlugin:
    """Gives tests an `hermetic_client` fixture and turns pytest reports into TestResults."""

    def __init__(self, target: str, *, timeout: float) -> None:
        self._target = target
        self._timeout = timeout
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
                teardown = f"{existing.message}\n\nteardown error: {teardown}"
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
        return _message_of(report, recording.secrets if recording else set())

    def _result(self, nodeid: str, status: TestStatus, message: str | None) -> TestResult:
        exchanges = list(self._recordings.get(nodeid, _Recording(self._allowed)).exchanges)
        return TestResult(
            test_id=nodeid,
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
        return str(longrepr[2]) if isinstance(longrepr, tuple) and len(longrepr) == 3 else None
    if not report.failed:
        return None
    crash = getattr(longrepr, "reprcrash", None)
    text = str(crash.message) if crash is not None else str(longrepr)
    # Mask before cutting: a credential that straddles the cut would no longer match.
    return mask_secrets(text, secrets or set())[:MAX_MESSAGE_CHARS]


def _repro(status: TestStatus, exchanges: list[Exchange]) -> str | None:
    """A curl for the last request the test made, when the test did not pass."""
    if status in ("failed", "error") and exchanges:
        return build_curl(exchanges[-1])
    return None
