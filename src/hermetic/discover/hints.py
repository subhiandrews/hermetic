"""Dependencies the documents name outright, found without the agent.

A connection string (`postgres://...`) or a pinned image (`redis:7`) is not a guess, so it needs
no model. The agent is asked about what is left. Evidence is where the line is, never its text.
"""

from __future__ import annotations

import re

from hermetic.discover.models import (
    KIND_ORDER,
    MAX_EVIDENCE,
    MAX_SOURCE_CHARS,
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
        if len(doc.path) > MAX_SOURCE_CHARS:  # Evidence cannot name it; the agent may still
            continue
        for number, line in enumerate(doc.text.splitlines(), start=1):
            for kind in dict.fromkeys(_kinds_in(line)):  # once per line, in order
                places = evidence.setdefault(kind, [])
                if len(places) < MAX_EVIDENCE:
                    places.append(Evidence(source=doc.path, line=number))
    return [
        Dependency(name=kind, kind=kind, origin="hint", evidence=evidence[kind])
        for kind in KIND_ORDER
        if kind in evidence
    ]
