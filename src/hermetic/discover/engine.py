"""Discovery: what the documents say, checked, merged with what a person has written down."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from hermetic.discover.hints import find_hints
from hermetic.discover.infer import DependencyInferrer, InferRequest, Proposal, Quote
from hermetic.discover.models import (
    KIND_ORDER,
    MAX_DEPENDENCIES,
    MAX_EVIDENCE,
    MIN_QUOTE_CHARS,
    Dependency,
    DependencySet,
    Evidence,
)
from hermetic.ingest.models import DocFile, ServiceModel


class DiscoverError(Exception):
    """Discovery could not run to the end, or found more than a service can plausibly have."""


@dataclass(frozen=True)
class Discovery:
    dependencies: DependencySet
    changes: list[tuple[str, str]]  # ("added" | "changed" | "removed", dependency name)
    notes: list[str]  # what was set aside, and why


def docs_hash(docs: list[DocFile]) -> str:
    """A fingerprint of the documents discovery read."""
    hasher = hashlib.sha256()
    for doc in sorted(docs, key=lambda d: d.path):
        hasher.update(f"{doc.path}\0{doc.sha256}\0".encode())
    return hasher.hexdigest()


def _squash(text: str) -> str:
    return " ".join(text.split())


def _locate(lines: dict[str, list[str]], quote: Quote) -> Evidence | None:
    """Where in the named document a line holds the quote, or None when none does.

    The saved evidence is that place, not the text: a document can hold anything (a password
    in a connection string), and a committed file should not copy it. A reader opens the
    document at the line. A quote too short to mean anything, or one that runs across lines,
    proves nothing and is not found.
    """
    needle = _squash(quote.quote)
    if len(needle) < MIN_QUOTE_CHARS:
        return None
    for number, line in enumerate(lines.get(quote.source, []), start=1):
        if needle in line:
            return Evidence(source=quote.source, line=number)
    return None


def _from_proposal(proposal: Proposal, lines: dict[str, list[str]]) -> Dependency | None:
    found = [_locate(lines, quote) for quote in proposal.quotes]
    evidence = list(dict.fromkeys(e for e in found if e is not None))[:MAX_EVIDENCE]
    if not evidence:
        return None
    return Dependency(name=proposal.name, kind=proposal.kind, origin="agent", evidence=evidence)


def discover(
    model: ServiceModel,
    service: str,
    inferrer: DependencyInferrer,
    existing: DependencySet | None,
) -> Discovery:
    """Hints from the documents, then the agent's proposals, each kept only if a document says it.

    A proposal must quote the document it relies on, and code checks the quote is there, so one
    the agent made up has nothing to stand on and is dropped. That shows a document says it, not
    that the document is right. Entries a person wrote (origin "manual") are kept untouched and
    win over any found name.
    """
    hints = find_hints(model.docs)
    request = InferRequest(
        service_title=model.title,
        servers=model.servers,
        operations=[f"{operation.method} {operation.path}" for operation in model.operations],
        docs=model.docs,
        hints=hints,
    )
    try:
        inferred = inferrer.infer(request)
    except Exception as exc:  # the agent can fail in any way: a timeout, a bad reply, no network
        raise DiscoverError(f"dependency inference failed: {exc}") from exc

    lines = {doc.path: [_squash(line) for line in doc.text.splitlines()] for doc in model.docs}
    before = {d.name: d for d in existing.dependencies} if existing else {}
    manual = {name: d for name, d in before.items() if d.origin == "manual"}
    notes: list[str] = list(inferred.notes)
    found: dict[str, Dependency] = {}
    candidates: list[tuple[str, Dependency | None]] = [(h.name, h) for h in hints]
    candidates += [(p.name, _from_proposal(p, lines)) for p in inferred.proposals]
    for name, dependency in candidates:
        if name in manual:
            notes.append(f"{name}: kept the entry you wrote")
        elif dependency is None:
            notes.append(f"{name}: dropped, no document says it")
        elif (current := found.get(name)) is None:
            found[name] = dependency
        elif current.kind != dependency.kind:
            notes.append(f"{name}: dropped as {dependency.kind}, already found as {current.kind}")
        else:
            unique = list(dict.fromkeys([*current.evidence, *dependency.evidence]))
            found[name] = current.model_copy(update={"evidence": unique[:MAX_EVIDENCE]})

    everything = {**manual, **found}
    if len(everything) > MAX_DEPENDENCIES:
        raise DiscoverError(
            f"found {len(everything)} dependencies; a service has at most {MAX_DEPENDENCIES}. "
            "Narrow the documents, or edit dependencies.json by hand"
        )
    ordered = sorted(everything.values(), key=lambda d: (KIND_ORDER.index(d.kind), d.name))
    result = DependencySet(
        service=service,
        prompt_version=inferrer.prompt_version,
        docs_hash=docs_hash(model.docs),
        dependencies=ordered,
    )
    return Discovery(result, _changes(before, everything), notes)


def _changes(before: dict[str, Dependency], after: dict[str, Dependency]) -> list[tuple[str, str]]:
    changes = [("added", name) for name in sorted(after.keys() - before.keys())]
    changes += [("removed", name) for name in sorted(before.keys() - after.keys())]
    changes += [
        ("changed", name)
        for name in sorted(after.keys() & before.keys())
        if before[name] != after[name]
    ]
    return changes
