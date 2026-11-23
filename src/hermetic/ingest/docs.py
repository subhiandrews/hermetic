"""Load design and architecture documents that sit next to the spec."""

from __future__ import annotations

import hashlib
import os
import stat
from pathlib import Path
from typing import Literal

from hermetic.ingest.errors import IngestError
from hermetic.ingest.models import DocFile

Kind = Literal["markdown", "text", "diagram"]

KINDS: dict[str, Kind] = {
    ".md": "markdown",
    ".markdown": "markdown",
    ".txt": "text",
    ".rst": "text",
    ".adoc": "text",
    ".puml": "diagram",  # diagrams as text: PlantUML and Mermaid
    ".plantuml": "diagram",
    ".mmd": "diagram",
    ".mermaid": "diagram",
}
MAX_DOC_BYTES = 256_000  # per file
MAX_DOC_FILES = 500
MAX_TOTAL_DOC_BYTES = 8_000_000


def _candidates(root: Path) -> list[Path]:
    """Recognised document files under ``root``. Hidden directories are pruned before they
    are entered (so pointing at a repository root never walks ``.git`` or ``.venv``), and
    symlinks are not followed (a link could point outside the docs directory)."""

    def fail(exc: OSError) -> None:
        # os.walk skips a directory it cannot list unless told otherwise, which would drop
        # its docs silently.
        raise IngestError(f"cannot read docs directory {exc.filename}: {exc.strerror}") from exc

    found: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root, onerror=fail, followlinks=False):
        dirnames[:] = [name for name in dirnames if not name.startswith(".")]
        for name in filenames:
            path = Path(dirpath, name)
            if (
                name.startswith(".")
                or path.suffix.lower() not in KINDS
                or path.is_symlink()
                or not path.is_file()
            ):
                continue
            found.append(path)
            if len(found) > MAX_DOC_FILES:  # stop walking, e.g. a docs path at a repo root
                raise IngestError(
                    f"more than {MAX_DOC_FILES} documents under {root}; "
                    "point --docs at a narrower directory"
                )
    return found


def _read_regular_file(path: Path, name: str) -> bytes:
    """Read a document, never more than ``MAX_DOC_BYTES + 1`` bytes.

    The file is opened without following a symlink or waiting on a FIFO, and the opened
    descriptor (not the path) is checked, so something swapped in after ``_candidates``
    listed the file cannot get around the checks, and a file that grows after it was
    listed is still capped."""
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    with os.fdopen(os.open(path, flags), "rb") as handle:
        if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
            raise IngestError(f"doc {name} is not a regular file")
        raw = handle.read(MAX_DOC_BYTES + 1)
    if len(raw) > MAX_DOC_BYTES:
        raise IngestError(f"doc {name} is larger than {MAX_DOC_BYTES} bytes")
    return raw


def load_docs(root: Path) -> list[DocFile]:
    """Read every recognised document under ``root``, sorted by path.

    A file over ``MAX_DOC_BYTES`` or that is not UTF-8 is an error rather than a silent
    omission, so the agent never reasons from a partial picture."""
    if not root.is_dir():
        raise IngestError(f"docs path {root} is not a directory")
    docs: list[DocFile] = []
    total_bytes = 0
    for path in sorted(_candidates(root), key=lambda p: p.relative_to(root).parts):
        relative = path.relative_to(root)
        try:
            raw = _read_regular_file(path, relative.as_posix())
            total_bytes += len(raw)
            if total_bytes > MAX_TOTAL_DOC_BYTES:
                raise IngestError(
                    f"docs under {root} are larger than {MAX_TOTAL_DOC_BYTES} bytes in total; "
                    "point --docs at a narrower directory"
                )
            text = raw.decode("utf-8")
        except OSError as exc:  # unreadable, symlinked after listing, or removed meanwhile
            raise IngestError(f"cannot read doc {relative.as_posix()}: {exc}") from exc
        except UnicodeDecodeError as exc:
            raise IngestError(f"doc {relative.as_posix()} is not valid UTF-8") from exc
        docs.append(
            DocFile(
                path=relative.as_posix(),
                kind=KINDS[path.suffix.lower()],
                sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
                text=text,
            )
        )
    return docs
