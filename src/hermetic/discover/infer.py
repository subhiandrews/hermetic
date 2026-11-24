"""The interface between discovery and whatever reads documents and names dependencies."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from hermetic.discover.models import Dependency
from hermetic.ingest.models import DocFile


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

    def infer(self, request: InferRequest) -> list[Dependency]: ...
