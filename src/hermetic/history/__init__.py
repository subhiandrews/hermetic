"""Run history: records of suite executions and the stores that keep them."""

import os
from pathlib import Path

from hermetic.history.file_store import FileRunStore
from hermetic.history.http_store import HttpRunStore
from hermetic.history.models import (
    Exchange,
    RunRecord,
    RunSummary,
    ServiceSummary,
    SuiteChange,
    TestResult,
    summarize,
)
from hermetic.history.store import RunNotFoundError, RunStore, RunStoreError, is_valid_run_id

API_TOKEN_ENV = "HERMETIC_API_TOKEN"


def open_store(location: Path | str) -> RunStore:
    """The run store at `location`: a Hermetic server's address (http:// or https://) or a
    history directory. Callers depend on RunStore, not the class. A server's API token comes
    from the HERMETIC_API_TOKEN environment variable."""
    if str(location).startswith(("http://", "https://")):
        return HttpRunStore(str(location), token=os.environ.get(API_TOKEN_ENV) or None)
    return FileRunStore(Path(location))


__all__ = [
    "API_TOKEN_ENV",
    "Exchange",
    "FileRunStore",
    "HttpRunStore",
    "RunNotFoundError",
    "RunRecord",
    "RunStore",
    "RunStoreError",
    "RunSummary",
    "ServiceSummary",
    "SuiteChange",
    "TestResult",
    "is_valid_run_id",
    "open_store",
    "summarize",
]
