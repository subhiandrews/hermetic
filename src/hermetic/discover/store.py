"""Reading and writing `dependencies.json` beside a service's tests."""

from __future__ import annotations

import json
import os
import stat
import tempfile
from pathlib import Path

from pydantic import ValidationError

from hermetic.discover.models import DependencySet

DEPENDENCIES_NAME = Path(".hermetic") / "dependencies.json"
MAX_FILE_BYTES = 1_000_000


class DependenciesError(Exception):
    """The dependencies file cannot be read or written."""


def load_dependencies(suite_dir: Path) -> DependencySet | None:
    """The saved dependencies, or None when none were saved yet."""
    path = suite_dir / DEPENDENCIES_NAME
    if not path.exists() and not path.is_symlink():
        return None
    if path.parent.is_symlink():
        raise DependenciesError(f"{path.parent} is a symlink; not following it")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        with os.fdopen(os.open(path, flags), "rb") as handle:
            if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                raise DependenciesError(f"{path} is not a regular file")
            raw = handle.read(MAX_FILE_BYTES + 1)
    except OSError as exc:  # a symlink, unreadable, or removed meanwhile
        raise DependenciesError(f"cannot read {path}: {exc.strerror}") from exc
    if len(raw) > MAX_FILE_BYTES:
        raise DependenciesError(f"{path} is larger than {MAX_FILE_BYTES} bytes")
    try:
        return DependencySet.model_validate_json(raw)
    except ValidationError as exc:
        raise DependenciesError(f"{path} is not valid: {exc.error_count()} problem(s)") from exc


def write_dependencies(suite_dir: Path, dependencies: DependencySet) -> Path:
    """Save them as stable, reviewable JSON. A failure leaves the old file as it was."""
    path = suite_dir / DEPENDENCIES_NAME
    if path.parent.is_symlink():
        raise DependenciesError(f"{path.parent} is a symlink; not writing through it")
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(dependencies.model_dump(mode="json"), indent=2, sort_keys=True) + "\n"
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".deps-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.chmod(tmp, 0o644)  # mkstemp makes it 0600; this file is committed and shared
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return path
