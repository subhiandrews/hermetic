"""Run records: what one execution of a suite produced."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from hermetic.service import DEFAULT_SERVICE, SERVICE_PATTERN

TestStatus = Literal["passed", "failed", "error", "skipped"]
ChangeKind = Literal["added", "modified", "dropped"]
RECENT_RUNS = 20  # how many of a service's newest runs its summary carries


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True)


class Exchange(_Frozen):
    """One HTTP request and, if one arrived, its response. Secrets are already redacted."""

    method: str
    url: str
    request_headers: dict[str, str]
    request_body: str | None
    # False when request_body only describes the body (binary) or is cut short, so it cannot
    # be replayed.
    request_body_complete: bool = True
    status_code: int | None  # None when no response arrived (connection error, timeout)
    response_headers: dict[str, str]
    response_body: str | None
    duration_ms: float


class TestResult(_Frozen):
    __test__ = False  # not a pytest class, despite the name

    test_id: str
    operation_id: str | None  # from @pytest.mark.operation("..."), if the test has one
    status: TestStatus
    duration_ms: float
    message: str | None  # failure or error text, as pytest reports it
    exchanges: list[Exchange]
    curl: str | None  # repro for the failing request; only set for failed and error tests


class SuiteChange(_Frozen):
    """A test added, modified or dropped since the previous run, and why."""

    kind: ChangeKind
    test_id: str
    reason: str
    before: str | None = None  # spec fragment before the change (modified only)
    after: str | None = None  # spec fragment after the change (modified only)


class RunRecord(_Frozen):
    # Records written before services existed have neither field; they read as version 1 of the
    # default service.
    schema_version: int = 1
    service: str = Field(default=DEFAULT_SERVICE, pattern=SERVICE_PATTERN)
    run_id: str
    started_at: str  # ISO 8601, UTC
    finished_at: str
    target: str
    spec_hash: str | None
    doc_hashes: dict[str, str]  # docs-relative path -> sha256
    prompt_version: str | None
    tool_version: str
    results: list[TestResult]
    changes: list[SuiteChange] = Field(default_factory=list)


class RunSummary(_Frozen):
    """The index entry for a run: enough to list it without reading the full record."""

    service: str = Field(default=DEFAULT_SERVICE, pattern=SERVICE_PATTERN)
    run_id: str
    started_at: str
    target: str
    spec_hash: str | None
    passed: int
    failed: int
    errors: int
    skipped: int
    added: int
    modified: int
    dropped: int


class ServiceSummary(_Frozen):
    """One service in the history: how many runs it has and the most recent one."""

    service: str
    runs: int
    latest: RunSummary
    # Its newest runs, newest first, at most RECENT_RUNS (the first is `latest`). The page draws
    # its trend from this, so listing the services is one request and not one per service.
    recent: list[RunSummary] = Field(default_factory=list)


def summarize(record: RunRecord) -> RunSummary:
    """Reduce a run record to its index entry."""

    def count(status: TestStatus) -> int:
        return sum(1 for result in record.results if result.status == status)

    def changed(kind: ChangeKind) -> int:
        return sum(1 for change in record.changes if change.kind == kind)

    return RunSummary(
        service=record.service,
        run_id=record.run_id,
        started_at=record.started_at,
        target=record.target,
        spec_hash=record.spec_hash,
        passed=count("passed"),
        failed=count("failed"),
        errors=count("error"),
        skipped=count("skipped"),
        added=changed("added"),
        modified=changed("modified"),
        dropped=changed("dropped"),
    )
