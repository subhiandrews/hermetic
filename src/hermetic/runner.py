"""Run orchestration: execute a suite, record the run, publish it."""

from __future__ import annotations

import secrets
from datetime import UTC, datetime
from pathlib import Path

from hermetic import __version__
from hermetic.execute import execute_suite
from hermetic.history import RunRecord, RunStore, RunStoreError, SuiteChange
from hermetic.ingest import ServiceModel
from hermetic.redact import redact_url
from hermetic.report import ReportSink, SinkError


def new_run_id(now: datetime) -> str:
    """Sortable by time, with a random tail so two runs in one second do not collide."""
    return f"{now.astimezone(UTC):%Y%m%dT%H%M%SZ}-{secrets.token_hex(3)}"


def run_suite(
    suite: Path,
    target: str,
    store: RunStore,
    sinks: list[ReportSink],
    *,
    model: ServiceModel | None = None,
    changes: list[SuiteChange] | None = None,
    prompt_version: str | None = None,
    timeout: float = 30.0,
) -> RunRecord:
    """Execute the suite, save the run to the store, then publish it to every sink."""
    started = datetime.now(UTC)
    results = execute_suite(suite, target, timeout=timeout)
    finished = datetime.now(UTC)
    record = RunRecord(
        run_id=new_run_id(started),
        started_at=started.isoformat(),
        finished_at=finished.isoformat(),
        target=redact_url(target),
        spec_hash=model.spec_hash if model else None,
        doc_hashes={doc.path: doc.sha256 for doc in model.docs} if model else {},
        prompt_version=prompt_version,
        tool_version=__version__,
        results=results,
        changes=changes or [],
    )
    try:
        store.save(record)
    except RunStoreError as exc:
        exc.record = record
        raise
    failures: list[str] = []
    for sink in sinks:  # one broken sink must not stop the others
        try:
            sink.publish(record)
        except Exception as exc:  # a third-party sink can fail in any way
            failures.append(f"{type(sink).__name__}: {exc}")
    if failures:
        raise SinkError(
            f"run {record.run_id} was saved, but publishing failed: {'; '.join(failures)}",
            record,
        )
    return record
