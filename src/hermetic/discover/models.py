"""What a service depends on, as found in its documents and kept beside its tests."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from hermetic.service import SERVICE_PATTERN

DependencyKind = Literal["postgres", "mysql", "redis", "mongo", "http"]
KIND_ORDER: tuple[DependencyKind, ...] = ("postgres", "mysql", "redis", "mongo", "http")
# A dependency name becomes a compose service name and part of an environment variable later,
# so it cannot be free text.
NAME_PATTERN = r"^[a-z][a-z0-9-]{0,31}$"
MAX_DEPENDENCIES = 32
MAX_EVIDENCE = 5
MAX_QUOTE_CHARS = 300
MAX_SOURCE_CHARS = 300
MIN_QUOTE_CHARS = 6  # shorter than a pinned image like `redis:7` is not a passage


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class Evidence(_Frozen):
    """A passage of a document that says the service has this dependency."""

    source: str = Field(
        min_length=1, max_length=MAX_SOURCE_CHARS
    )  # the document's path under the docs root
    quote: str = Field(min_length=1, max_length=MAX_QUOTE_CHARS)


class Dependency(_Frozen):
    name: str = Field(pattern=NAME_PATTERN)
    kind: DependencyKind
    # Who put it here. A "manual" entry is kept as it is when discovery runs again; the others
    # are replaced by what the documents say that time.
    origin: Literal["hint", "agent", "manual"]
    evidence: list[Evidence] = Field(default_factory=list, max_length=MAX_EVIDENCE)


class DependencySet(_Frozen):
    """The file `tests/<service>/.hermetic/dependencies.json`. Committed, reviewed, editable."""

    version: Literal[1] = 1
    service: str = Field(pattern=SERVICE_PATTERN)
    prompt_version: str  # of the inferrer that ran; tells a reader how stale the guesses are
    docs_hash: str  # of the documents discovery read, so a later step can say they changed
    dependencies: list[Dependency] = Field(max_length=MAX_DEPENDENCIES)

    @model_validator(mode="after")
    def _names_are_unique(self) -> DependencySet:
        names = [dependency.name for dependency in self.dependencies]
        if len(names) != len(set(names)):
            raise ValueError("two dependencies share a name")
        return self
