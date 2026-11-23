"""Run history: records of suite executions and the stores that keep them."""

from pathlib import Path

from hermetic.history.file_store import FileRunStore
from hermetic.history.models import (
    Exchange,
    RunRecord,
    RunSummary,
    SuiteChange,
    TestResult,
    summarize,
)
from hermetic.history.store import RunNotFoundError, RunStore, RunStoreError


def open_store(directory: Path) -> RunStore:
    """The run store for a history directory. Callers depend on RunStore, not the class."""
    return FileRunStore(directory)


__all__ = [
    "Exchange",
    "FileRunStore",
    "RunNotFoundError",
    "RunRecord",
    "RunStore",
    "RunStoreError",
    "RunSummary",
    "SuiteChange",
    "TestResult",
    "open_store",
    "summarize",
]
