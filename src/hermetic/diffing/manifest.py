"""The test manifest: which generated test covers which operation, and from what."""

from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path

from pydantic import BaseModel, ConfigDict, ValidationError

MANIFEST_VERSION = 1
MAX_MANIFEST_BYTES = 8 * 1024 * 1024
# The only shape of file name the generator writes. The manifest is a committed file, so a
# name read back from it is checked before it is used to write or delete anything.
TEST_FILE_PATTERN = re.compile(r"test_[A-Za-z0-9_]{1,120}\.py")


class ManifestError(Exception):
    """The manifest cannot be read, or holds something the generator would not have written."""


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True)


class ManifestEntry(_Frozen):
    operation_id: str
    test_file: str  # a file name inside the suite directory
    fragment_hash: str
    fragment: str  # the operation as it was when the test was written, for before/after
    prompt_version: str
    reason: str  # why the test was last (re)written


class Manifest(_Frozen):
    version: int = MANIFEST_VERSION
    entries: dict[str, ManifestEntry] = {}  # operation_id -> entry


def load_manifest(path: Path) -> Manifest:
    """Read the manifest; a missing file is an empty manifest (nothing generated yet)."""
    try:
        size = path.stat().st_size
    except FileNotFoundError:
        return Manifest()
    except OSError as exc:
        raise ManifestError(f"cannot read manifest {path}: {exc}") from exc
    if size > MAX_MANIFEST_BYTES:
        raise ManifestError(f"manifest {path} is larger than {MAX_MANIFEST_BYTES} bytes")
    try:
        manifest = Manifest.model_validate(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError, ValidationError) as exc:
        raise ManifestError(f"manifest {path} is not valid: {exc}") from exc
    if manifest.version != MANIFEST_VERSION:
        raise ManifestError(
            f"manifest {path} has version {manifest.version}; expected {MANIFEST_VERSION}"
        )
    for key, entry in manifest.entries.items():
        if key != entry.operation_id or not TEST_FILE_PATTERN.fullmatch(entry.test_file):
            raise ManifestError(f"manifest {path} has an invalid entry for {key!r}")
    return manifest


def save_manifest(path: Path, manifest: Manifest) -> None:
    """Write the manifest atomically, with stable key order so it diffs cleanly in git."""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = manifest.model_dump(mode="json")
    payload["entries"] = dict(sorted(payload["entries"].items()))
    text = json.dumps(payload, indent=2, ensure_ascii=False) + "\n"
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".manifest-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.chmod(tmp, 0o644)  # mkstemp makes it 0600; the manifest is committed and shared
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
