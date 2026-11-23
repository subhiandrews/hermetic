"""Run orchestration: execute a suite, record the run, publish it."""

from __future__ import annotations

import secrets
from datetime import UTC, datetime
from pathlib import Path

from hermetic import __version__
from hermetic.execute import execute_suite
from hermetic.history import RunRecord, RunStore
from hermetic.ingest import ServiceModel
from hermetic.report import ReportSink


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
        target=target,
        spec_hash=model.spec_hash if model else None,
        doc_hashes={doc.path: doc.sha256 for doc in model.docs} if model else {},
        prompt_version=None,
        tool_version=__version__,
        results=results,
    )
    store.save(record)
    for sink in sinks:
        sink.publish(record)
    return record
