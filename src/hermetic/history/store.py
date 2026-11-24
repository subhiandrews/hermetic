"""The run-history interface the CLI and web UI depend on."""

from __future__ import annotations

import re
from typing import Protocol

from hermetic.history.models import RunRecord, RunSummary, ServiceSummary

# A run id becomes a file name and a URL segment, so it must not be able to name a path. "index"
# is reserved for the file store's index, in any case.
_RUN_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}")
_RESERVED_RUN_ID = "index"


def is_valid_run_id(run_id: str) -> bool:
    return _RUN_ID.fullmatch(run_id) is not None and run_id.lower() != _RESERVED_RUN_ID


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
