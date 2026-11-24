"""The test manifest: which generated test covers which operation, and from what."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from pathlib import Path

from pydantic import BaseModel, ConfigDict, ValidationError

MANIFEST_VERSION = 1
MAX_MANIFEST_BYTES = 8 * 1024 * 1024


def file_name_for(operation_id: str) -> str:
    """A safe, collision-free file name: the slug alone could clash (`a-b` and `a_b`).

    The manifest is a committed file, so a name read back from it must equal this one before
    it is used to write or delete anything.
    """
    slug = re.sub(r"[^A-Za-z0-9_]", "_", operation_id)[:80]
    digest = hashlib.sha256(operation_id.encode("utf-8")).hexdigest()[:8]
    return f"test_{slug}_{digest}.py"


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
    except (OSError, ValueError, ValidationError, RecursionError) as exc:
        raise ManifestError(f"manifest {path} is not valid: {exc}") from exc
    if manifest.version != MANIFEST_VERSION:
        raise ManifestError(
            f"manifest {path} has version {manifest.version}; expected {MANIFEST_VERSION}"
        )
    for key, entry in manifest.entries.items():
        if key != entry.operation_id or entry.test_file != file_name_for(key):
            raise ManifestError(f"manifest {path} has an invalid entry for {key!r}")
    return manifest


def manifest_text(manifest: Manifest) -> str:
    """The file's content, with stable key order so it diffs cleanly in git.

    `save_manifest` writes exactly this, so its size is the size the file will have.
    """
    payload = manifest.model_dump(mode="json")
    payload["entries"] = dict(sorted(payload["entries"].items()))
    return json.dumps(payload, indent=2, ensure_ascii=False) + "\n"


def save_manifest(path: Path, manifest: Manifest) -> None:
    """Write the manifest atomically."""
    text = manifest_text(manifest)
    size = len(text.encode("utf-8"))
    if size > MAX_MANIFEST_BYTES:  # never write what `load_manifest` would refuse to read back
        raise ManifestError(
            f"the manifest would be {size} bytes, over the {MAX_MANIFEST_BYTES} byte limit"
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".manifest-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.chmod(tmp, 0o644)  # mkstemp makes it 0600; the manifest is committed and shared
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
