"""The run-history interface the CLI and web UI depend on."""

from __future__ import annotations

from typing import Protocol

from hermetic.history.models import RunRecord, RunSummary


class RunStoreError(Exception):
    """The store could not complete the operation (corrupt data, I/O failure)."""


class RunNotFoundError(RunStoreError):
    """No run with that id exists."""


class RunStore(Protocol):
    def save(self, record: RunRecord) -> None:
        """Persist a run, replacing any earlier record with the same id."""
        ...

    def get(self, run_id: str) -> RunRecord:
        """Return the run, or raise RunNotFoundError."""
        ...

    def list(self) -> list[RunSummary]:
        """Return every run's summary, newest first."""
        ...
