"""Discovery: what the documents say, checked, merged with what a person has written down."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

from hermetic.discover.hints import find_hints
from hermetic.discover.infer import DependencyInferrer, InferRequest
from hermetic.discover.models import (
    KIND_ORDER,
    MAX_DEPENDENCIES,
    MAX_EVIDENCE,
    MAX_QUOTE_CHARS,
    MIN_QUOTE_CHARS,
    Dependency,
    DependencySet,
    Evidence,
)
from hermetic.ingest.models import DocFile, ServiceModel
from hermetic.redact import REDACTED, SENSITIVE_KEY_PARTS, redact_url

_URL_IN_TEXT = re.compile(r"[a-z][a-z0-9+.\-]*://\S+", re.IGNORECASE)
_KEYS = "|".join(re.escape(part) for part in SENSITIVE_KEY_PARTS)
# key, an optional closing quote, = or :, an optional opening quote, then the value
_SECRET_PAIR = re.compile(
    rf"((?:{_KEYS}|access[_-]?key)\w*[\"']?\s*[=:]\s*[\"']?)[^\s,;&\"']+", re.IGNORECASE
)
_AUTH_SCHEME = re.compile(r"\b(bearer|basic)(\s+)\S+", re.IGNORECASE)
_BARE_USERINFO = re.compile(r"(?<![\w/])[^\s/@:]+:[^\s/@]+@(?=[\w.\-])")  # user:pw@host, no scheme


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


def _says(texts: dict[str, str], evidence: Evidence) -> bool:
    """Whether the named document holds the quote; an empty or tiny quote proves nothing."""
    quote = _squash(evidence.quote)
    return len(quote) >= MIN_QUOTE_CHARS and quote in texts.get(evidence.source, "")


def _masked(quote: str) -> str:
    """The quote with credentials removed, since the file is meant to be committed."""
    clean = " ".join(_URL_IN_TEXT.sub(lambda m: redact_url(m.group()), quote).split())
    clean = _BARE_USERINFO.sub(REDACTED + "@", clean)
    clean = _AUTH_SCHEME.sub(lambda m: m.group(1) + m.group(2) + REDACTED, clean)
    return _SECRET_PAIR.sub(r"\1" + REDACTED, clean)[:MAX_QUOTE_CHARS]


def discover(
    model: ServiceModel,
    service: str,
    inferrer: DependencyInferrer,
    existing: DependencySet | None,
) -> Discovery:
    """Hints from the documents, then the agent's proposals, each kept only if a document says it.

    Evidence is checked in code: a quote must appear in the document it names, so a proposal
    the agent made up has nothing to stand on and is dropped. That shows a document says it, not
    that the document is right. Kept quotes have URL credentials and secret-looking pairs masked.
    Entries a person wrote (origin "manual") are kept untouched and win over any found name.
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
        proposed = inferrer.infer(request)
    except Exception as exc:  # the agent can fail in any way: a timeout, a bad reply, no network
        raise DiscoverError(f"dependency inference failed: {exc}") from exc

    texts = {doc.path: _squash(doc.text) for doc in model.docs}
    before = {d.name: d for d in existing.dependencies} if existing else {}
    manual = {name: d for name, d in before.items() if d.origin == "manual"}
    notes: list[str] = []
    found: dict[str, Dependency] = {}
    for dependency in [*hints, *proposed]:
        if dependency.name in manual:
            notes.append(f"{dependency.name}: kept the entry you wrote")
            continue
        verified = [
            Evidence(source=e.source, quote=_masked(e.quote))
            for e in dependency.evidence
            if _says(texts, e)
        ]
        if not verified:
            notes.append(f"{dependency.name}: dropped, no document says it")
            continue
        current = found.get(dependency.name)
        if current is None:
            found[dependency.name] = dependency.model_copy(
                update={"evidence": verified[:MAX_EVIDENCE]}
            )
        elif current.kind != dependency.kind:
            notes.append(
                f"{dependency.name}: dropped as {dependency.kind}, already found as {current.kind}"
            )
        else:
            merged = _merged(current.evidence, verified)
            found[dependency.name] = current.model_copy(update={"evidence": merged})

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


def _merged(first: list[Evidence], second: list[Evidence]) -> list[Evidence]:
    unique = list(dict.fromkeys([*first, *second]))
    return unique[:MAX_EVIDENCE]


def _changes(before: dict[str, Dependency], after: dict[str, Dependency]) -> list[tuple[str, str]]:
    changes = [("added", name) for name in sorted(after.keys() - before.keys())]
    changes += [("removed", name) for name in sorted(before.keys() - after.keys())]
    changes += [
        ("changed", name)
        for name in sorted(after.keys() & before.keys())
        if before[name] != after[name]
    ]
    return changes
