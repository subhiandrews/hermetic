"""WireMock stubs for a downstream service, written by code from its OpenAPI spec.

Each operation with a success response becomes one stub that answers with an example from the
spec (`example`, `default`, `enum`, or a value made from the schema's types). Nothing here asks
an agent: the same spec always gives the same files, and the files hold only what the spec
says. Stubs made here carry a marker so a later run replaces them and leaves hand-written
ones alone.

An operation that also declares concrete 4xx or 5xx responses gets a WireMock scenario named
like its stub file. The scenario starts in `Started`, where the success stub answers, and has
one more state per declared status (`404`) with a stub that answers with that error. A client
of the mock's admin API moves the scenario between states; nothing here does. A stub that is
part of a scenario names it and its state in `metadata`, so the scenarios can be read back from
the files. Ranges (`4XX`), `default`, and 1xx and 3xx codes are left out and reported.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import tempfile
from pathlib import Path
from typing import Any, TypeGuard
from urllib.parse import urlparse

from hermetic.ingest.models import Json, Operation, Response, ServiceModel
from hermetic.provision.compose import ProvisionError, refuse_links

MAX_STUBS = 500
MAX_STUB_BYTES = 131_072  # a whole stub file as written; also the most `_ours` will read
MAX_DEPTH = 8
MAX_KEYS = 64  # properties kept per object, so a wide schema cannot grow a body without bound
METHODS = frozenset({"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"})
MARKER = "hermetic"
# Both are used with `fullmatch`: `$` would also accept a key that ends in a newline.
_SUCCESS = re.compile(r"2(\d\d|XX)")  # OpenAPI also allows the range "2XX"
_ERROR = re.compile(r"[45]\d\d")  # a concrete code; "4XX" and "default" have none to answer with
START_STATE = "Started"  # WireMock's own name for a scenario's first state
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


def _is_number(value: object) -> TypeGuard[int | float]:
    return isinstance(value, int | float) and not isinstance(value, bool)


def _bound(schema: Json, inclusive: str, exclusive: str, *, lower: bool) -> tuple[Any, bool]:
    """A bound and whether it excludes its own value, in the 3.0 and the 3.1 spelling."""
    marked = schema.get(exclusive)
    value = schema.get(inclusive)
    if _is_number(marked):  # 3.1: the exclusive key holds the number
        if _is_number(value) and (value > marked if lower else value < marked):
            return value, False  # the inclusive bound is the stricter one
        return marked, True
    if _is_number(value):
        return value, marked is True  # 3.0: a flag beside `minimum`
    return None, False


def _number(schema: Json, *, whole: bool) -> int | float:
    """A number inside the schema's bounds, as near zero as they allow."""
    low, low_open = _bound(schema, "minimum", "exclusiveMinimum", lower=True)
    high, high_open = _bound(schema, "maximum", "exclusiveMaximum", lower=False)
    try:
        if whole:
            first = None if low is None else (math.floor(low) + 1 if low_open else math.ceil(low))
            last = (
                None if high is None else (math.ceil(high) - 1 if high_open else math.floor(high))
            )
            value = 0
            if first is not None and value < first:
                value = first
            if last is not None and value > last:
                value = last
            return value
        if low is not None and (low > 0 or (low_open and low == 0)):
            if not low_open:
                return float(low)
            return float(low / 2 + high / 2 if high is not None else low + 1)
        if high is not None and (high < 0 or (high_open and high == 0)):
            if not high_open:
                return float(high)
            return float(low / 2 + high / 2 if low is not None else high - 1)
        return 0.0
    except OverflowError as exc:  # an integer bound too large for a float
        raise ProvisionError("a bound in the spec is too large to sample") from exc


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
    try:
        path = urlparse(model.servers[0]).path.rstrip("/")
    except ValueError as exc:  # e.g. unbalanced IPv6 brackets
        raise ProvisionError(f"the first server URL cannot be parsed: {exc}") from exc
    return path if path.startswith("/") else ""


def _json_schema(response: Response) -> Json | bool | None:
    for media_type, schema in response.content.items():
        if _is_json(media_type):
            return schema
    return None


def _success(operation: Operation) -> tuple[str, Json | bool | None] | None:
    codes = sorted(r.status for r in operation.responses if _SUCCESS.fullmatch(r.status))
    if not codes:
        return None
    response = next(r for r in operation.responses if r.status == codes[0])
    return codes[0], _json_schema(response)


def _errors(operation: Operation) -> tuple[list[tuple[str, Json | bool | None]], list[str]]:
    """The concrete 4xx and 5xx responses in status order, and the other non-2xx codes."""
    wanted: list[tuple[str, Json | bool | None]] = []
    left_out: list[str] = []
    for response in sorted(operation.responses, key=lambda r: r.status):
        if _SUCCESS.fullmatch(response.status):
            continue
        if _ERROR.fullmatch(response.status):
            wanted.append((response.status, _json_schema(response)))
        else:  # "default", a range such as "4XX", or a 1xx or 3xx code
            left_out.append(response.status)
    return wanted, left_out


def _file_name(operation: Operation) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", operation.operation_id.lower()).strip("-")[:48] or "op"
    digest = hashlib.sha256(f"{operation.method} {operation.path}".encode()).hexdigest()[:8]
    return f"{slug}-{digest}.json"


def render(mapping: dict[str, Any]) -> str:
    """The text of a stub file."""
    return json.dumps(mapping, indent=2, sort_keys=True) + "\n"


def _mapping(
    operation: Operation,
    prefix: str,
    status: int,
    schema: Json | bool | None,
    scenario: tuple[str, str] | None,
) -> dict[str, Any]:
    """One stub: the operation's request answered with `status`, in a scenario state if given."""
    response: dict[str, Any] = {"status": status}
    if schema is not None:
        response["headers"] = {"Content-Type": "application/json"}
        response["jsonBody"] = sample(schema)
    metadata: dict[str, Any] = {MARKER: True, "operation": operation.operation_id}
    mapping: dict[str, Any] = {
        "priority": min(1 + len(_PARAM.findall(operation.path)), 9),
        "request": {
            "method": operation.method,
            "urlPathPattern": _regex_for(prefix + operation.path),
        },
        "response": response,
        "metadata": metadata,
    }
    if scenario is not None:
        name, state = scenario
        mapping["scenarioName"] = name
        mapping["requiredScenarioState"] = state
        metadata["scenario"] = name
        metadata["state"] = state
    return mapping


def _too_big(mapping: dict[str, Any]) -> bool:
    # measured exactly as it will be written, so what is made is what `_ours` can read
    return len(render(mapping).encode()) > MAX_STUB_BYTES


def build_stubs(model: ServiceModel) -> tuple[dict[str, dict[str, Any]], list[str]]:
    """File name to mapping, and a line for each stub or response that was left out and why."""
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
            skipped.append(f"{label}: more than {MAX_STUBS} stubs")
        else:
            status, schema = found
            file_name = _file_name(operation)
            scenario = file_name.removesuffix(".json")
            errors, left_out = _errors(operation)
            candidates: list[tuple[str, dict[str, Any]]] = []
            for code, error_schema in errors:
                error = _mapping(operation, prefix, int(code), error_schema, (scenario, code))
                if _too_big(error):
                    skipped.append(
                        f"{label}: response {code} is larger than {MAX_STUB_BYTES} bytes"
                    )
                else:
                    candidates.append((code, error))
            room = max(MAX_STUBS - len(stubs) - 1, 0)  # the success stub takes one place
            kept, dropped = candidates[:room], candidates[room:]
            main = _mapping(
                operation,
                prefix,
                200 if status == "2XX" else int(status),
                schema,
                (scenario, START_STATE) if kept else None,
            )
            if _too_big(main):
                skipped.append(f"{label}: stub larger than {MAX_STUB_BYTES} bytes")
                continue
            stubs[file_name] = main
            for code, error in kept:
                stubs[f"{scenario}.{code}.json"] = error
            skipped.extend(
                f"{label}: response {code} not mocked, more than {MAX_STUBS} stubs"
                for code, _ in dropped
            )
            if left_out:
                skipped.append(f"{label}: response {', '.join(left_out)} not mocked")
    return stubs, skipped


def _ours(path: Path) -> bool:
    """Whether a file in the folder is a stub an earlier run made."""
    if path.suffix != ".json" or path.is_symlink() or not path.is_file():
        return False
    try:
        if path.stat().st_size > MAX_STUB_BYTES:
            return False
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, RecursionError):
        return False
    metadata = document.get("metadata") if isinstance(document, dict) else None
    return isinstance(metadata, dict) and metadata.get(MARKER) is True


def write_stubs(folder: Path, stubs: dict[str, dict[str, Any]]) -> None:
    """Make the folder hold these stubs, replacing the ones an earlier run made.

    Hand-written files are left alone, and a name that one of them already has is refused
    before anything changes. Old stubs go last, so a failed write never leaves the folder bare.
    """
    refuse_links(folder / "x")  # the folder itself and everything above it
    folder.mkdir(parents=True, exist_ok=True)
    for name in stubs:
        target = folder / name
        if (target.exists() or target.is_symlink()) and not _ours(target):
            raise ProvisionError(f"{target} exists and is not a stub Hermetic made")
    for name, mapping in stubs.items():
        fd, tmp = tempfile.mkstemp(dir=folder, prefix=".stub-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(render(mapping))
            os.chmod(tmp, 0o644)
            os.replace(tmp, folder / name)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
    for old in folder.iterdir():
        if old.name not in stubs and _ours(old):
            old.unlink()
