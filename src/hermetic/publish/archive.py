"""Pack a generated suite into a zip: its test modules and its manifest, under `<service>/`."""

from __future__ import annotations

import os
import stat
import tempfile
import zipfile
from pathlib import Path

from hermetic.diffing.manifest import MAX_MANIFEST_BYTES
from hermetic.generate.suite import MANIFEST_NAME
from hermetic.generate.validate import MAX_SOURCE_BYTES

# The earliest time a zip can record and a fixed mode, so the same suite always packs to the
# same bytes: the archive does not depend on when the files were written or on the user's umask.
ZIP_EPOCH = (1980, 1, 1, 0, 0, 0)
_FILE_MODE = 0o100644  # a regular file, rw-r--r--
_UNIX = 3  # the "created on" system of every entry, whichever machine packs it


class ExportError(Exception):
    """The suite could not be packed."""


def suite_files(suite_dir: Path) -> list[tuple[str, int]]:
    """The files that make up the suite, as (path inside the suite, size limit), sorted.

    That is every `test_*.py` module in the suite directory and its manifest. A symlink is not
    followed, since it could name a file outside the suite, and anything else in the directory
    (a cache, a helper, a note) is not part of what was generated.
    """
    found: list[tuple[str, int]] = []
    if suite_dir.is_dir():
        found.extend(
            (path.name, MAX_SOURCE_BYTES)
            for path in suite_dir.glob("test_*.py")
            if path.is_file() and not path.is_symlink()
        )
        manifest = suite_dir / MANIFEST_NAME
        if manifest.is_file() and not manifest.is_symlink() and not manifest.parent.is_symlink():
            found.append((MANIFEST_NAME.as_posix(), MAX_MANIFEST_BYTES))
    return sorted(found)


def _read_limited(path: Path, name: str, limit: int) -> bytes:
    """Read a regular file of at most `limit` bytes without following a symlink.

    The checks run on the open handle, so a file swapped for a link or a huge file after
    `suite_files` listed it is refused, and at most `limit + 1` bytes are ever read.
    """
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        os.close(fd)
        raise ExportError(f"{name} is not a regular file")
    with os.fdopen(fd, "rb", closefd=True) as handle:
        data = handle.read(limit + 1)
    if len(data) > limit:
        raise ExportError(f"{name} is larger than {limit} bytes; it was not generated")
    return data


def write_suite_zip(service: str, suite_dir: Path, destination: Path) -> list[str]:
    """Write the suite to `destination` and return the names packed, in archive order.

    The entries are `<service>/<file>`, so unpacking cannot scatter files into the current
    directory. The archive is written beside its destination and moved into place, so a failure
    leaves no half-written file and an existing archive is replaced whole.
    """
    files = suite_files(suite_dir)
    if not any(name.startswith("test_") for name, _ in files):
        raise ExportError(f"no generated tests in {suite_dir}; run `hermetic generate` first")
    contents: list[tuple[str, bytes]] = []
    for name, limit in files:
        contents.append((name, _read_limited(suite_dir / name, name, limit)))

    fd, tmp = tempfile.mkstemp(dir=destination.parent, prefix=".export-", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as handle, zipfile.ZipFile(handle, "w") as archive:
            for name, data in contents:
                entry = zipfile.ZipInfo(f"{service}/{name}", date_time=ZIP_EPOCH)
                entry.compress_type = zipfile.ZIP_DEFLATED
                entry.create_system = _UNIX
                entry.external_attr = _FILE_MODE << 16
                archive.writestr(entry, data, compresslevel=9)
        os.chmod(tmp, 0o644)  # mkstemp makes it 0600; the archive is meant to be shared
        os.replace(tmp, destination)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return [f"{service}/{name}" for name, _ in contents]
