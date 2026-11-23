"""The interface between the generator and whatever writes test source."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from hermetic.ingest.models import DocFile, Operation


@dataclass(frozen=True)
class AuthorRequest:
    operation: Operation
    service_title: str
    servers: list[str]
    docs: list[DocFile]
    reason: str  # why this test is being written now
    previous_source: str | None  # the test being replaced, if any


@dataclass(frozen=True)
class AuthoredTest:
    source: str  # a complete pytest module
    note: str  # one line saying what the test covers or what changed


class SuiteAuthor(Protocol):
    """Writes one test module for one operation. The Claude agent is one implementation."""

    @property
    def prompt_version(self) -> str: ...

    def write_test(self, request: AuthorRequest) -> AuthoredTest: ...
