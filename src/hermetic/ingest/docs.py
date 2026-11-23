"""Load design and architecture documents that sit next to the spec."""

from __future__ import annotations

import hashlib
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
MAX_DOC_BYTES = 256_000


def load_docs(root: Path) -> list[DocFile]:
    """Read every recognised document under ``root``, sorted by path.

    Hidden files and directories and symlinks are skipped (a symlink could point outside
    the docs directory). A file over ``MAX_DOC_BYTES`` or that is not UTF-8 is an error
    rather than a silent omission, so the agent never reasons from a partial picture."""
    if not root.is_dir():
        raise IngestError(f"docs path {root} is not a directory")
    docs: list[DocFile] = []
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root)
        if (
            any(part.startswith(".") for part in relative.parts)
            or path.is_symlink()
            or not path.is_file()
            or path.suffix.lower() not in KINDS
        ):
            continue
        if path.stat().st_size > MAX_DOC_BYTES:
            raise IngestError(f"doc {relative.as_posix()} is larger than {MAX_DOC_BYTES} bytes")
        try:
            text = path.read_text(encoding="utf-8")
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
