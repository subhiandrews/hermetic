"""Parse and validate an OpenAPI 3.x document into operations."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any, cast

import yaml
from openapi_spec_validator import validate
from openapi_spec_validator.validation.exceptions import OpenAPIValidationError

from hermetic.ingest.errors import IngestError
from hermetic.ingest.models import Json, Operation, Parameter, RequestBody, Response

HTTP_METHODS = ("get", "put", "post", "delete", "options", "head", "patch", "trace")


def canonical_hash(value: Any) -> str:
    """SHA-256 of a JSON-serialisable value, independent of key order and formatting."""
    blob = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def load_spec_file(path: Path) -> Json:
    """Read a JSON or YAML OpenAPI file into a dict. Does not validate it."""
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise IngestError(f"cannot read spec {path}: {exc}") from exc
    try:
        data = json.loads(text) if path.suffix.lower() == ".json" else yaml.safe_load(text)
    except (json.JSONDecodeError, yaml.YAMLError) as exc:
        raise IngestError(f"cannot parse spec {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise IngestError(f"spec {path} must be a JSON/YAML object at the top level")
    return cast(Json, data)


def validate_spec(spec: Json) -> None:
    """Reject anything that is not a valid OpenAPI 3.x document."""
    version = spec.get("openapi")
    if not isinstance(version, str) or not version.startswith("3."):
        raise IngestError(f"only OpenAPI 3.x is supported (found openapi={version!r})")
    try:
        validate(spec)
    except OpenAPIValidationError as exc:
        raise IngestError(f"invalid OpenAPI document: {exc}") from exc


def _resolve_pointer(root: Json, ref: str) -> Any:
    node: Any = root
    for part in ref[2:].split("/"):
        key = part.replace("~1", "/").replace("~0", "~")
        if isinstance(node, dict) and key in node:
            node = node[key]
        else:
            raise IngestError(f"unresolvable $ref {ref!r}")
    return node


def deref(node: Any, root: Json, _stack: tuple[str, ...] = ()) -> Any:
    """Inline local ``$ref`` pointers. A reference to a schema already being expanded
    (a recursive schema) is left as ``{"$ref": ...}`` instead of looping forever."""
    if isinstance(node, dict):
        ref = node.get("$ref")
        if isinstance(ref, str):
            if not ref.startswith("#/"):
                raise IngestError(
                    f"unsupported $ref {ref!r}: only local '#/...' references are supported"
                )
            if ref in _stack:
                return {"$ref": ref}
            return deref(_resolve_pointer(root, ref), root, (*_stack, ref))
        return {key: deref(value, root, _stack) for key, value in node.items()}
    if isinstance(node, list):
        return [deref(value, root, _stack) for value in node]
    return node


def _synthesized_id(method: str, path: str) -> str:
    return f"{method}_{re.sub(r'[^a-zA-Z0-9]+', '_', path).strip('_')}"


def _parameters(path_level: list[Json], op_level: list[Json]) -> list[Parameter]:
    merged: dict[tuple[str, str], Json] = {}
    for raw in (*path_level, *op_level):  # operation-level overrides path-level
        merged[(raw["in"], raw["name"])] = raw
    return [
        Parameter(
            name=raw["name"],
            location=raw["in"],
            # path parameters are always required in OpenAPI
            required=bool(raw.get("required", False)) or raw["in"] == "path",
            json_schema=raw.get("schema", {}),
        )
        for raw in merged.values()
    ]


def _content(raw: Json) -> dict[str, Json]:
    return {media: body.get("schema", {}) for media, body in raw.items()}


def build_operations(spec: Json) -> list[Operation]:
    """One Operation per path+method, in document order, with references resolved."""
    operations: list[Operation] = []
    seen_ids: set[str] = set()
    default_security = spec.get("security", [])
    for path, raw_item in spec.get("paths", {}).items():
        item = deref(raw_item, spec)
        for method in HTTP_METHODS:
            if method not in item:
                continue
            raw = item[method]
            op_id = raw.get("operationId") or _synthesized_id(method, path)
            if op_id in seen_ids:
                raise IngestError(f"duplicate operationId {op_id!r} ({method.upper()} {path})")
            seen_ids.add(op_id)
            body = raw.get("requestBody")
            operations.append(
                Operation(
                    operation_id=op_id,
                    method=method.upper(),
                    path=path,
                    summary=raw.get("summary"),
                    description=raw.get("description"),
                    tags=raw.get("tags", []),
                    deprecated=bool(raw.get("deprecated", False)),
                    parameters=_parameters(item.get("parameters", []), raw.get("parameters", [])),
                    request_body=(
                        RequestBody(
                            required=bool(body.get("required", False)),
                            content=_content(body.get("content", {})),
                        )
                        if body
                        else None
                    ),
                    responses=[
                        Response(
                            status=str(status),
                            description=resp.get("description", ""),
                            content=_content(resp.get("content", {})),
                        )
                        for status, resp in raw.get("responses", {}).items()
                    ],
                    security=raw.get("security", default_security),
                    fragment_hash=canonical_hash(
                        {
                            "method": method,
                            "path": path,
                            "path_parameters": item.get("parameters", []),
                            "operation": raw,
                        }
                    ),
                )
            )
    return operations
