"""Dependencies the documents name outright, found without the agent.

A connection string (`postgres://...`) or a pinned image (`redis:7`) is not a guess, so it needs
no model. The agent is asked about what is left.
"""

from __future__ import annotations

import re

from hermetic.discover.models import (
    KIND_ORDER,
    MAX_EVIDENCE,
    MAX_QUOTE_CHARS,
    Dependency,
    DependencyKind,
    Evidence,
)
from hermetic.ingest.models import DocFile

_SCHEMES: dict[str, DependencyKind] = {
    "postgres": "postgres",
    "postgresql": "postgres",
    "mysql": "mysql",
    "redis": "redis",
    "rediss": "redis",
    "mongo": "mongo",
    "mongodb": "mongo",
}
_URL = re.compile(r"\b(postgresql|postgres|mysql|rediss|redis|mongodb(?:\+srv)?)://", re.IGNORECASE)
# An image needs a version, so a sentence like "redis: the cache" is not taken for one.
_IMAGE = re.compile(r"\b(postgres|mysql|redis|mongo):\d[\w.\-]*", re.IGNORECASE)


def _kinds_in(line: str) -> list[DependencyKind]:
    found: list[DependencyKind] = []
    for match in _URL.finditer(line):
        found.append(_SCHEMES[match.group(1).lower().split("+")[0]])
    for match in _IMAGE.finditer(line):
        found.append(_SCHEMES[match.group(1).lower()])
    return found


def find_hints(docs: list[DocFile]) -> list[Dependency]:
    """One dependency per kind the documents name, each with the lines that name it."""
    evidence: dict[DependencyKind, list[Evidence]] = {}
    for doc in docs:
        for line in doc.text.splitlines():
            for kind in dict.fromkeys(_kinds_in(line)):  # once per line, in order
                quotes = evidence.setdefault(kind, [])
                quote = " ".join(line.split())[:MAX_QUOTE_CHARS]
                if len(quotes) < MAX_EVIDENCE:
                    quotes.append(Evidence(source=doc.path, quote=quote))
    return [
        Dependency(name=kind, kind=kind, origin="hint", evidence=evidence[kind])
        for kind in KIND_ORDER
        if kind in evidence
    ]
