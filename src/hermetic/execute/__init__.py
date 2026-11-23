"""Execute stage: run a pytest suite against a target and record every request."""

from __future__ import annotations

import sys
from pathlib import Path
from urllib.parse import urlsplit

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
    _check_target(target)
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
    before = set(sys.modules)
    try:
        code = pytest.main(args, plugins=[plugin])
    finally:
        _forget_new_modules_under(root, before)
    # A collection error also ends the session as INTERRUPTED; that one is reported as a result.
    if code == pytest.ExitCode.INTERRUPTED and not plugin.collection_failed:
        raise ExecuteError("the run was interrupted before every test ran")
    if code == pytest.ExitCode.NO_TESTS_COLLECTED:
        raise ExecuteError(f"no tests found in {suite}")
    if code in (pytest.ExitCode.INTERNAL_ERROR, pytest.ExitCode.USAGE_ERROR):
        raise ExecuteError(f"pytest could not run the suite (exit code {int(code)})")
    return plugin.results


def _check_target(target: str) -> None:
    try:
        parts = urlsplit(target)
        parts.port  # noqa: B018  (raises ValueError on a bad port)
    except ValueError as exc:
        raise ExecuteError(f"invalid --target {target!r}: {exc}") from exc
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ExecuteError(f"invalid --target {target!r}: expected http(s)://host[:port]")


def _forget_new_modules_under(root: Path, before: set[str]) -> None:
    """Drop modules the run imported from the suite so the next run re-imports them.

    Pytest names a test module after its path relative to the rootdir, so two suites that
    both have `test_api.py` would otherwise share one cached module. Modules that were
    loaded before the run (this package, pytest, httpx) and installed packages are kept,
    even when the suite lives inside a project that has an in-tree virtualenv.
    """
    resolved = root.resolve()
    for name, module in list(sys.modules.items()):
        file = getattr(module, "__file__", None)
        if name in before or file is None:
            continue
        path = Path(file).resolve()
        if path.is_relative_to(resolved) and not {"site-packages", "dist-packages"} & set(
            path.parts
        ):
            del sys.modules[name]
