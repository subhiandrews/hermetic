"""Ingest stage: spec and docs in, ServiceModel out."""

from __future__ import annotations

from pathlib import Path

from pydantic import ValidationError

from hermetic.ingest.docs import load_docs
from hermetic.ingest.errors import IngestError
from hermetic.ingest.models import ServiceModel
from hermetic.ingest.openapi import (
    build_operations,
    canonical_hash,
    load_spec_file,
    validate_spec,
    validate_swagger2,
)
from hermetic.ingest.swagger2 import upgrade

__all__ = ["IngestError", "ServiceModel", "ingest"]


def ingest(spec_path: Path, docs_dir: Path | None = None) -> ServiceModel:
    """Build a ServiceModel from an OpenAPI 3.x or Swagger 2.0 file and an optional docs
    directory (a 2.0 file is converted first; see docs/architecture.md)."""
    spec = load_spec_file(spec_path)
    if "swagger" in spec:
        validate_swagger2(spec)
        try:
            spec = upgrade(spec)
        except (KeyError, TypeError, ValueError, AttributeError, RecursionError) as exc:
            # A valid 2.0 document the converter did not expect; say so, not a traceback.
            raise IngestError(
                f"cannot convert the Swagger 2.0 document ({type(exc).__name__}: {exc})"
            ) from exc
    validate_spec(spec)
    try:
        return ServiceModel(
            title=spec["info"]["title"],
            version=spec["info"]["version"],
            openapi_version=spec["openapi"],
            servers=[server["url"] for server in spec.get("servers", [])],
            operations=build_operations(spec),
            docs=load_docs(docs_dir) if docs_dir is not None else [],
            spec_hash=canonical_hash(spec),
        )
    except ValidationError as exc:  # backstop: a valid spec shape the model does not hold
        raise IngestError(f"the spec does not fit the service model: {exc}") from exc
