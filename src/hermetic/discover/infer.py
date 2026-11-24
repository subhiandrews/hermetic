"""The interface between discovery and whatever reads documents and names dependencies."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field

from hermetic.discover.models import (
    MAX_QUOTE_CHARS,
    MAX_SOURCE_CHARS,
    NAME_PATTERN,
    Dependency,
    DependencyKind,
)
from hermetic.ingest.models import DocFile


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class Quote(_Frozen):
    """A passage the agent says a document holds. Discovery checks it; it is not saved."""

    source: str = Field(min_length=1, max_length=MAX_SOURCE_CHARS)
    quote: str = Field(min_length=1, max_length=MAX_QUOTE_CHARS)


class Proposal(_Frozen):
    """A dependency the agent names, with the passages it relies on."""

    name: str = Field(pattern=NAME_PATTERN)
    kind: DependencyKind
    quotes: list[Quote]


@dataclass(frozen=True)
class InferResult:
    proposals: list[Proposal]
    notes: list[str] = field(default_factory=list)  # what the inferrer itself set aside


@dataclass(frozen=True)
class InferRequest:
    service_title: str
    servers: list[str]
    operations: list[str]  # "METHOD /path", to tell the agent what the service does
    docs: list[DocFile]
    hints: list[Dependency]  # what is already known, so the agent adds rather than repeats


class DependencyInferrer(Protocol):
    """Names the dependencies the documents describe. The Claude agent is one implementation.

    What it returns is a proposal: discovery keeps only what the documents bear out.
    """

    @property
    def prompt_version(self) -> str: ...

    def infer(self, request: InferRequest) -> InferResult: ...
