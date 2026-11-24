"""WireMock stubs for a downstream service, written by code from its OpenAPI spec.

Each operation with a success response becomes one stub that answers with an example from the
spec (`example`, `default`, `enum`, or a value made from the schema's types). Nothing here asks
an agent: the same spec always gives the same files, and the files hold only what the spec
says. Stubs made here carry a marker so a later run replaces them and leaves hand-written
ones alone.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from hermetic.ingest.models import Json, Operation, ServiceModel
from hermetic.provision.compose import ProvisionError, refuse_links

MAX_STUBS = 500
MAX_BODY_BYTES = 65_536
MAX_DEPTH = 8
MAX_KEYS = 64  # properties kept per object, so a wide schema cannot grow a body without bound
METHODS = frozenset({"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"})
MARKER = "hermetic"
_SUCCESS = re.compile(r"^2\d\d$")
_PARAM = re.compile(r"\{[^{}/]+\}")
_REGEX_SPECIAL = set(".\\+*?^$()[]{}|")
_FORMATS = {
    "date-time": "2020-01-01T00:00:00Z",
    "date": "2020-01-01",
    "uuid": "00000000-0000-0000-0000-000000000000",
    "email": "user@example.com",
    "uri": "http://example.com/",
}


def _is_json(media_type: str) -> bool:
    base = media_type.split(";", 1)[0].strip().lower()
    return base == "application/json" or base.endswith("+json")


def _number(schema: Json, *, whole: bool) -> int | float:
    low = schema.get("minimum")
    high = schema.get("maximum")
    value: float = 0
    if isinstance(low, int | float) and not isinstance(low, bool) and low > value:
        value = low
    if isinstance(high, int | float) and not isinstance(high, bool) and high < value:
        value = high
    return int(value) if whole else float(value)


def sample(schema: Json | bool, depth: int = 0) -> Any:
    """A value that fits the schema, the same one every time."""
    if not isinstance(schema, dict) or depth > MAX_DEPTH or "$ref" in schema:
        return None  # a boolean schema, too deep, or a recursive reference
    for key in ("example", "default", "const"):
        if key in schema:
            return schema[key]
    examples = schema.get("examples")
    if isinstance(examples, list) and examples:
        return examples[0]
    enum = schema.get("enum")
    if isinstance(enum, list) and enum:
        return enum[0]
    for key in ("oneOf", "anyOf"):
        options = schema.get(key)
        if isinstance(options, list) and options:
            return sample(options[0], depth + 1)
    parts = schema.get("allOf")
    if isinstance(parts, list) and parts:
        merged: dict[str, Any] = {}
        for part in parts:
            value = sample(part, depth + 1)
            if isinstance(value, dict):
                merged.update(value)
            elif not merged:
                return value
        return merged
    kind = schema.get("type")
    if isinstance(kind, list):  # OpenAPI 3.1: ["string", "null"]
        kind = next((k for k in kind if k != "null"), "null")
    if kind is None and "properties" in schema:
        kind = "object"
    if kind == "object":
        properties = schema.get("properties")
        if not isinstance(properties, dict):
            return {}
        names = list(properties)[:MAX_KEYS]
        return {name: sample(properties[name], depth + 1) for name in names}
    if kind == "array":
        return [sample(schema.get("items", {}), depth + 1)]
    if kind == "string":
        return _FORMATS.get(str(schema.get("format")), "string")
    if kind in ("integer", "number"):
        return _number(schema, whole=kind == "integer")
    if kind == "boolean":
        return True
    return None


def _regex_for(path: str) -> str:
    """A Java regex that matches the path template and nothing else."""
    out: list[str] = []
    position = 0
    for match in _PARAM.finditer(path):
        out.append(_escape(path[position : match.start()]))
        out.append("[^/]+")
        position = match.end()
    out.append(_escape(path[position:]))
    return "".join(out)


def _escape(text: str) -> str:
    return "".join("\\" + c if c in _REGEX_SPECIAL else c for c in text)


def _prefix(model: ServiceModel) -> str:
    """The path part of the spec's first server, which callers put before every path."""
    if not model.servers:
        return ""
    path = urlparse(model.servers[0]).path.rstrip("/")
    return path if path.startswith("/") else ""


def _success(operation: Operation) -> tuple[str, Json | bool | None] | None:
    codes = sorted(r.status for r in operation.responses if _SUCCESS.match(r.status))
    if not codes:
        return None
    response = next(r for r in operation.responses if r.status == codes[0])
    for media_type, schema in response.content.items():
        if _is_json(media_type):
            return codes[0], schema
    return codes[0], None


def _file_name(operation: Operation) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", operation.operation_id.lower()).strip("-")[:48] or "op"
    digest = hashlib.sha256(f"{operation.method} {operation.path}".encode()).hexdigest()[:8]
    return f"{slug}-{digest}.json"


def build_stubs(model: ServiceModel) -> tuple[dict[str, dict[str, Any]], list[str]]:
    """File name to mapping, and a line for each operation that got no stub and why."""
    prefix = _prefix(model)
    stubs: dict[str, dict[str, Any]] = {}
    skipped: list[str] = []
    for operation in model.operations:
        label = f"{operation.method} {operation.path}"
        found = _success(operation)
        if operation.method not in METHODS:
            skipped.append(f"{label}: method not supported")
        elif found is None:
            skipped.append(f"{label}: no 2xx response")
        elif len(stubs) >= MAX_STUBS:
            skipped.append(f"{label}: more than {MAX_STUBS} operations")
        else:
            status, schema = found
            response: dict[str, Any] = {"status": int(status)}
            if schema is not None:
                body = sample(schema)
                encoded = json.dumps(body, sort_keys=True)
                if len(encoded.encode()) > MAX_BODY_BYTES:
                    skipped.append(f"{label}: example larger than {MAX_BODY_BYTES} bytes")
                    continue
                response["headers"] = {"Content-Type": "application/json"}
                response["jsonBody"] = body
            stubs[_file_name(operation)] = {
                "priority": min(1 + len(_PARAM.findall(operation.path)), 9),
                "request": {
                    "method": operation.method,
                    "urlPathPattern": _regex_for(prefix + operation.path),
                },
                "response": response,
                "metadata": {MARKER: True, "operation": operation.operation_id},
            }
    return stubs, skipped


def _ours(path: Path) -> bool:
    """Whether a file in the folder is a stub an earlier run made."""
    if path.suffix != ".json" or path.is_symlink() or not path.is_file():
        return False
    try:
        if path.stat().st_size > MAX_BODY_BYTES * 2:
            return False
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    metadata = document.get("metadata") if isinstance(document, dict) else None
    return isinstance(metadata, dict) and metadata.get(MARKER) is True


def write_stubs(folder: Path, stubs: dict[str, dict[str, Any]]) -> None:
    """Replace the stubs an earlier run made with these, leaving every other file alone."""
    refuse_links(folder / "x")  # the folder itself and everything above it
    folder.mkdir(parents=True, exist_ok=True)
    for old in folder.iterdir():
        if _ours(old):
            old.unlink()
    for name, mapping in stubs.items():
        target = folder / name
        if target.exists() or target.is_symlink():
            raise ProvisionError(f"{target} exists and is not a stub Hermetic made")
        fd, tmp = tempfile.mkstemp(dir=folder, prefix=".stub-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(mapping, handle, indent=2, sort_keys=True)
                handle.write("\n")
            os.chmod(tmp, 0o644)
            os.replace(tmp, target)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
