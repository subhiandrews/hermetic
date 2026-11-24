"""The run-history interface the CLI and web UI depend on."""

from __future__ import annotations

from typing import Protocol

from hermetic.history.models import RunRecord, RunSummary, ServiceSummary


class RunStoreError(Exception):
    """The store could not complete the operation (corrupt data, I/O failure).

    `record` is set by run_suite when saving failed after the suite had run, so the caller
    can still derive the exit code from the results.
    """

    record: RunRecord | None = None


class RunNotFoundError(RunStoreError):
    """No run with that id exists."""


Summaries = list[RunSummary]  # aliases: `list` is shadowed by the method inside the protocol
Services = list[ServiceSummary]


class RunStore(Protocol):
    """Run history, grouped by service. A run id is unique within its service."""

    def save(self, record: RunRecord) -> None:
        """Persist a run under `record.service`, replacing any earlier record with that id."""
        ...

    def get(self, service: str, run_id: str) -> RunRecord:
        """Return the run, or raise RunNotFoundError."""
        ...

    def list(self, service: str | None = None) -> Summaries:
        """Return the summaries of one service's runs, or of every service's when `service`
        is None, newest first."""
        ...

    def services(self) -> Services:
        """Return each service that has runs, with its latest run, sorted by service id."""
        ...
