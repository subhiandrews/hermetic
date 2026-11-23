"""Execute stage: run a pytest suite against a target and record every request."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from hermetic.execute.plugin import HermeticPlugin, OriginError
from hermetic.history.models import TestResult

__all__ = ["ExecuteError", "OriginError", "execute_suite"]


class ExecuteError(Exception):
    """The suite could not be run at all (as opposed to tests failing)."""


def execute_suite(suite: Path, target: str, *, timeout: float = 30.0) -> list[TestResult]:
    """Run the pytest suite at `suite` against `target` and return one result per test.

    Tests get an `hermetic_client` fixture (an httpx client for the target). It refuses any
    request to another origin. Failing tests are results, not errors; ExecuteError means
    the run itself broke (bad path, no tests, internal pytest error).
    """
    if not suite.exists():
        raise ExecuteError(f"suite not found: {suite}")
    plugin = HermeticPlugin(target, timeout=timeout)
    root = suite if suite.is_dir() else suite.parent
    args = [
        str(suite),
        "--rootdir",
        str(root),
        "--import-mode=importlib",
        "-p",
        "no:cacheprovider",
        "-p",
        "no:terminal",
    ]
    try:
        code = pytest.main(args, plugins=[plugin])
    finally:
        _forget_modules_under(root)
    if code == pytest.ExitCode.NO_TESTS_COLLECTED:
        raise ExecuteError(f"no tests found in {suite}")
    if code in (pytest.ExitCode.INTERNAL_ERROR, pytest.ExitCode.USAGE_ERROR):
        raise ExecuteError(f"pytest could not run the suite (exit code {int(code)})")
    return plugin.results


def _forget_modules_under(root: Path) -> None:
    """Drop modules imported from the suite so the next run in this process re-imports.

    Pytest names a test module after its path relative to the rootdir, so two suites that
    both have `test_api.py` would otherwise share one cached module.
    """
    resolved = root.resolve()
    for name, module in list(sys.modules.items()):
        file = getattr(module, "__file__", None)
        if file is not None and Path(file).resolve().is_relative_to(resolved):
            del sys.modules[name]
