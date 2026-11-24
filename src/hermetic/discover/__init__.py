"""Discover stage: the service model in, the dependencies it needs mocked out."""

from __future__ import annotations

from hermetic.discover.engine import DiscoverError, Discovery, discover, docs_hash
from hermetic.discover.hints import find_hints
from hermetic.discover.infer import (
    DependencyInferrer,
    InferRequest,
    InferResult,
    Proposal,
    Quote,
)
from hermetic.discover.models import (
    Dependency,
    DependencyKind,
    DependencySet,
    Evidence,
)
from hermetic.discover.store import (
    DEPENDENCIES_NAME,
    DependenciesError,
    load_dependencies,
    write_dependencies,
)

__all__ = [
    "DEPENDENCIES_NAME",
    "DependenciesError",
    "Dependency",
    "DependencyInferrer",
    "DependencyKind",
    "DependencySet",
    "DiscoverError",
    "Discovery",
    "Evidence",
    "InferRequest",
    "InferResult",
    "Proposal",
    "Quote",
    "discover",
    "docs_hash",
    "find_hints",
    "load_dependencies",
    "write_dependencies",
]
