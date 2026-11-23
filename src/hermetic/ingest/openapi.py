"""Parse and validate an OpenAPI 3.x document into operations."""

from __future__ import annotations

import datetime
import hashlib
import json
import re
from pathlib import Path
from typing import Any

import yaml
from openapi_spec_validator import validate
from openapi_spec_validator.exceptions import OpenAPISpecValidatorError
from openapi_spec_validator.validation.exceptions import OpenAPIValidationError
from referencing.exceptions import Unresolvable

from hermetic.ingest.errors import IngestError
from hermetic.ingest.models import Json, Operation, Parameter, RequestBody, Response

HTTP_METHODS = ("get", "put", "post", "delete", "options", "head", "patch", "trace")

# Node caps. A few KB of YAML can expand into billions of nodes (alias bombs, or schemas that
# reference each other with fan-out), so every traversal that copies the document is bounded.
MAX_SPEC_NODES = 1_000_000  # the whole document, after YAML aliases are expanded
MAX_EXPANDED_NODES = 500_000  # one path item, after its $refs are inlined
MAX_TOTAL_EXPANDED_NODES = 2_000_000  # all path items together: bounds total memory


class _Budget:
    def __init__(self, limit: int, what: str, parent: _Budget | None = None) -> None:
        self._limit = limit
        self._left = limit
        self._what = what
        self._parent = parent  # every node spent here is also spent against the parent

    def spend(self) -> None:
        if self._parent is not None:
            self._parent.spend()
        self._left -= 1
        if self._left < 0:
            raise IngestError(
                f"{self._what} is too large or has too much shared structure "
                f"(more than {self._limit} nodes)"
            )


def canonical_hash(value: Any) -> str:
    """SHA-256 of a JSON-serialisable value, independent of key order and formatting."""
    try:
        blob = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        # UnicodeEncodeError (a ValueError) comes from here: a JSON "\ud800" escape loads fine
        # but is not encodable.
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()
    except (TypeError, ValueError, RecursionError) as exc:
        raise IngestError(f"cannot hash document: {exc}") from exc


def _normalise(node: Any, budget: _Budget) -> Any:
    """Turn what YAML can produce into plain JSON types: keys become strings (``200:`` next
    to ``default:``), dates become ISO strings, anything else non-JSON is rejected."""
    budget.spend()
    if isinstance(node, dict):
        return {str(key): _normalise(value, budget) for key, value in node.items()}
    if isinstance(node, list):
        return [_normalise(value, budget) for value in node]
    if isinstance(node, datetime.date):  # datetime.datetime is a subclass
        return node.isoformat()
    if node is None or isinstance(node, str | int | float | bool):
        return node
    raise IngestError(f"unsupported value of type {type(node).__name__} in spec")


def load_spec_file(path: Path) -> Json:
    """Read a JSON or YAML OpenAPI file into a dict of plain JSON types. Does not validate it."""
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise IngestError(f"cannot read spec {path}: {exc}") from exc
    try:
        data = json.loads(text) if path.suffix.lower() == ".json" else yaml.safe_load(text)
        if not isinstance(data, dict):
            raise IngestError(f"spec {path} must be a JSON/YAML object at the top level")
        normalised: Json = _normalise(data, _Budget(MAX_SPEC_NODES, "spec"))
    except (json.JSONDecodeError, yaml.YAMLError, RecursionError) as exc:
        raise IngestError(f"cannot parse spec {path}: {exc}") from exc
    return normalised


def _reject_external_refs(spec: Json) -> None:
    """Refuse any ``$ref`` that is not a local ``#/...`` pointer. This must run before the
    validator, whose resolver would otherwise fetch http(s) URLs or read local files."""
    stack: list[Any] = [spec]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            ref = node.get("$ref")
            if isinstance(ref, str) and not ref.startswith("#/"):
                raise IngestError(
                    f"unsupported $ref {ref!r}: only local '#/...' references are supported"
                )
            stack.extend(node.values())
        elif isinstance(node, list):
            stack.extend(node)


def validate_spec(spec: Json) -> None:
    """Reject anything that is not a valid OpenAPI 3.x document."""
    version = spec.get("openapi")
    if not isinstance(version, str) or not version.startswith("3."):
        raise IngestError(f"only OpenAPI 3.x is supported (found openapi={version!r})")
    _reject_external_refs(spec)
    try:
        validate(spec)
    except (OpenAPIValidationError, OpenAPISpecValidatorError, Unresolvable, RecursionError) as exc:
        raise IngestError(f"invalid OpenAPI document: {exc}") from exc
    except Exception as exc:
        # The validator is third-party code reading untrusted input and can fail on unusual
        # documents (a scalar `x-` entry under `paths` raises a bare KeyError).
        raise IngestError(
            f"cannot validate OpenAPI document ({type(exc).__name__}: {exc})"
        ) from exc


def _resolve_pointer(root: Json, ref: str) -> Any:
    node: Any = root
    for part in ref[2:].split("/"):
        key = part.replace("~1", "/").replace("~0", "~")
        if isinstance(node, dict) and key in node:
            node = node[key]
        else:
            raise IngestError(f"unresolvable $ref {ref!r}")
    return node


def deref(
    node: Any,
    root: Json,
    _stack: tuple[str, ...] = (),
    _budget: _Budget | None = None,
) -> Any:
    """Inline local ``$ref`` pointers. A reference to a schema already being expanded
    (a recursive schema) is left as ``{"$ref": ...}`` instead of looping forever, and the
    total size of the expansion is capped at ``MAX_EXPANDED_NODES``."""
    budget = _budget or _Budget(MAX_EXPANDED_NODES, "operation after inlining references")
    budget.spend()
    if isinstance(node, dict):
        ref = node.get("$ref")
        if isinstance(ref, str):
            if not ref.startswith("#/"):
                raise IngestError(
                    f"unsupported $ref {ref!r}: only local '#/...' references are supported"
                )
            if ref in _stack:
                return {"$ref": ref}
            return deref(_resolve_pointer(root, ref), root, (*_stack, ref), budget)
        return {key: deref(value, root, _stack, budget) for key, value in node.items()}
    if isinstance(node, list):
        return [deref(value, root, _stack, budget) for value in node]
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
    total = _Budget(MAX_TOTAL_EXPANDED_NODES, "document after inlining references")
    for path, raw_item in spec.get("paths", {}).items():
        if not path.startswith("/"):
            continue  # a specification extension (`x-...`), not a path item
        try:
            item = deref(
                raw_item,
                spec,
                _budget=_Budget(MAX_EXPANDED_NODES, "operation after inlining references", total),
            )
        except RecursionError as exc:
            raise IngestError(f"{path}: references are nested too deeply") from exc
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
                        if not status.startswith("x-")  # specification extension
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
