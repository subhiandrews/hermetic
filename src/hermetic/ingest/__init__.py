"""Ingest stage: spec and docs in, ServiceModel out."""

from __future__ import annotations

from pathlib import Path

from hermetic.ingest.docs import load_docs
from hermetic.ingest.errors import IngestError
from hermetic.ingest.models import ServiceModel
from hermetic.ingest.openapi import (
    build_operations,
    canonical_hash,
    load_spec_file,
    validate_spec,
)

__all__ = ["IngestError", "ServiceModel", "ingest"]


def ingest(spec_path: Path, docs_dir: Path | None = None) -> ServiceModel:
    """Build a ServiceModel from an OpenAPI file and an optional docs directory."""
    spec = load_spec_file(spec_path)
    validate_spec(spec)
    return ServiceModel(
        title=spec["info"]["title"],
        version=spec["info"]["version"],
        openapi_version=spec["openapi"],
        servers=[server["url"] for server in spec.get("servers", [])],
        operations=build_operations(spec),
        docs=load_docs(docs_dir) if docs_dir is not None else [],
        spec_hash=canonical_hash(spec),
    )
