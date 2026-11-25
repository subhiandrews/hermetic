"""Convert a Swagger 2.0 document to OpenAPI 3.0.3, so the rest of ingest reads one format.

The input is validated as Swagger 2.0 before this runs, and the output is validated as OpenAPI 3.x
after it, so a converter bug shows up as an invalid document and not as a wrong model. Pure
functions over plain JSON types: no I/O.

What is and is not carried over is listed in docs/architecture.md (Ingest).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from hermetic.ingest.errors import IngestError
from hermetic.ingest.models import Json
from hermetic.ingest.openapi import HTTP_METHODS, MAX_SPEC_NODES, Budget

JSON_MEDIA = "application/json"
FORM_MEDIA = "application/x-www-form-urlencoded"
MULTIPART_MEDIA = "multipart/form-data"

# The keywords a non-body parameter, a header or an `items` object carries; in 3.0 they move
# under `schema`.
_SCHEMA_KEYWORDS = (
    "type",
    "format",
    "enum",
    "default",
    "maximum",
    "exclusiveMaximum",
    "minimum",
    "exclusiveMinimum",
    "maxLength",
    "minLength",
    "pattern",
    "maxItems",
    "minItems",
    "uniqueItems",
    "multipleOf",
)
# 2.0 defaults an array query parameter to comma-separated; 3.0 defaults to one key per item
# (`multi`), which is why that one needs nothing.
_QUERY_STYLES: dict[str, Json] = {
    "csv": {"style": "form", "explode": False},
    "ssv": {"style": "spaceDelimited", "explode": False},
    "pipes": {"style": "pipeDelimited", "explode": False},
}
_REF_PREFIXES = (
    ("#/definitions/", "#/components/schemas/"),
    ("#/responses/", "#/components/responses/"),
)
_PARAMETER_REF = "#/parameters/"
# The operation fields that mean the same in 3.0 and are copied as they are.
_OPERATION_KEYS = (
    "tags",
    "summary",
    "description",
    "externalDocs",
    "operationId",
    "deprecated",
    "security",
)
_FLOWS = {
    "implicit": ("implicit", ("authorizationUrl",)),
    "password": ("password", ("tokenUrl",)),
    "application": ("clientCredentials", ("tokenUrl",)),
    "accessCode": ("authorizationCode", ("authorizationUrl", "tokenUrl")),
}


@dataclass(frozen=True)
class _Context:
    parameters: Json  # the document's named parameters, inlined where an operation uses one
    consumes: list[str]
    produces: list[str]
    # A named body parameter used by many operations is converted once, not once per use.
    bodies: dict[int, Json] = field(default_factory=dict)
    # Spent as media-type entries and parameters are created, so a small file that fans out
    # (a huge `produces` list times many responses) stops early instead of after the fact.
    budget: Budget = field(default_factory=lambda: Budget(MAX_SPEC_NODES, "the converted document"))

    def body_schema(self, body: Json) -> Json:
        key = id(body)
        if key not in self.bodies:
            self.bodies[key] = _schema(body["schema"])
        return self.bodies[key]


def _check_size(out: Json) -> None:
    """Bound the converted document. A schema is converted once and shared by every media type
    that offers it, so the copies are cheap here but every later walk visits each one."""
    budget = Budget(MAX_SPEC_NODES, "the converted document")
    stack: list[object] = [out]
    while stack:
        node = stack.pop()
        budget.spend()
        if isinstance(node, dict):
            stack.extend(node.values())
        elif isinstance(node, list):
            stack.extend(node)


def upgrade(spec: Json) -> Json:
    """The OpenAPI 3.0.3 form of a valid Swagger 2.0 document."""
    context = _Context(
        spec.get("parameters", {}),
        spec.get("consumes") or [JSON_MEDIA],
        spec.get("produces") or [JSON_MEDIA],
    )
    out: Json = {"openapi": "3.0.3", "info": spec["info"]}
    servers = _servers(spec)
    if servers:
        out["servers"] = servers
    out["paths"] = {
        path: _path_item(item, context) if path.startswith("/") else item
        for path, item in spec.get("paths", {}).items()
    }
    components: Json = {}
    if "definitions" in spec:
        components["schemas"] = {name: _schema(s) for name, s in spec["definitions"].items()}
    if "responses" in spec:
        components["responses"] = {
            name: _response(r, context.produces, context) for name, r in spec["responses"].items()
        }
    if "securityDefinitions" in spec:
        components["securitySchemes"] = {
            name: _security_scheme(s) for name, s in spec["securityDefinitions"].items()
        }
    if components:
        out["components"] = components
    for key, value in spec.items():
        if key in ("security", "tags", "externalDocs") or key.startswith("x-"):
            out[key] = value
    _check_size(out)
    return out


def _servers(spec: Json) -> list[Json]:
    base = spec.get("basePath", "").rstrip("/")
    if "host" not in spec:
        return [{"url": base}] if base else []
    # With no `schemes` the spec says "the scheme used to access the document", which a file
    # does not know; http is what a local service answers on.
    schemes = spec.get("schemes") or ["http"]
    return [{"url": f"{scheme}://{spec['host']}{base}"} for scheme in schemes]


def _ref(ref: str) -> str:
    for old, new in _REF_PREFIXES:
        if ref.startswith(old):
            return new + ref[len(old) :]
    return ref


def _schema(node: Json) -> Json:
    """A Swagger 2.0 schema object as a 3.0 one."""
    out: Json = {}
    for key, value in node.items():
        if key == "$ref":
            out[key] = _ref(value)
        elif key == "x-nullable":
            out["nullable"] = value
        elif key == "discriminator":
            out[key] = {"propertyName": value}
        elif key == "properties":
            out[key] = {name: _schema(s) for name, s in value.items()}
        elif key == "items":
            out[key] = _schema(value)
        elif key == "allOf":
            out[key] = [_schema(s) for s in value]
        elif key == "additionalProperties":
            out[key] = _schema(value) if isinstance(value, dict) else value
        elif key == "type":
            _set_type(out, value)
        else:
            out[key] = value
    return out


def _set_type(out: Json, value: str | list[str]) -> None:
    """2.0 takes the JSON Schema draft 4 `type`: a list of names, `null` among them. 3.0 takes
    one name and marks null with `nullable`. (`file` is valid only for a form field, which
    `_simple_schema` handles; the validator refuses it in a schema, a response's included.)"""
    names = value if isinstance(value, list) else [value]
    if "null" in names:
        out["nullable"] = True
    kinds = [name for name in names if name != "null"]
    if len(kinds) == 1:
        out["type"] = kinds[0]
    elif kinds:
        out["anyOf"] = [{"type": kind} for kind in kinds]


def _simple_schema(raw: Json, budget: Budget) -> Json:
    """The schema of a non-body parameter, a header or an `items` object. Each dict built is
    spent against the budget, so a deeply nested `items` reused by many operations stops early."""
    budget.spend()
    schema = {key: raw[key] for key in _SCHEMA_KEYWORDS if key in raw}
    if "items" in raw:
        schema["items"] = _simple_schema(raw["items"], budget)
    if schema.get("type") == "file":
        schema["type"], schema["format"] = "string", "binary"
    return schema


def _resolve_parameter(raw: Json, context: _Context) -> Json:
    ref = raw.get("$ref")
    if ref is None:
        return raw
    if not ref.startswith(_PARAMETER_REF):
        raise IngestError(f"unsupported parameter reference {ref!r}: only {_PARAMETER_REF}... is")
    resolved: Json = context.parameters[ref[len(_PARAMETER_REF) :]]
    return resolved


def _parameter(raw: Json, context: _Context) -> Json:
    context.budget.spend()
    out: Json = {"name": raw["name"], "in": raw["in"]}
    for key in ("description", "required", "allowEmptyValue"):
        if key in raw:
            out[key] = raw[key]
    out["schema"] = _simple_schema(raw, context.budget)
    if raw["in"] == "query":
        out.update(_array_style(raw))
    return out


def _array_style(raw: Json) -> Json:
    """The 3.0 `style` and `explode` that say how an array is written, or nothing where the 3.0
    default (one key per item) is what the file means."""
    if raw.get("type") != "array":
        return {}
    return _QUERY_STYLES.get(raw.get("collectionFormat", "csv"), {})


def _form_body(fields: list[Json], consumes: list[str], budget: Budget) -> Json:
    is_multipart = MULTIPART_MEDIA in consumes or any(f.get("type") == "file" for f in fields)
    properties: Json = {}
    for parameter in fields:
        schema = _simple_schema(parameter, budget)
        if "description" in parameter:
            schema["description"] = parameter["description"]
        properties[parameter["name"]] = schema
    body_schema: Json = {"type": "object", "properties": properties}
    required = [f["name"] for f in fields if f.get("required")]
    if required:
        body_schema["required"] = required
    media = MULTIPART_MEDIA if is_multipart else FORM_MEDIA
    entry: Json = {"schema": body_schema}
    encoding = {f["name"]: style for f in fields if (style := _array_style(f))}
    if encoding:
        entry["encoding"] = encoding
    return {"required": bool(required), "content": {media: entry}}


def _operation(raw: Json, path_parameters: list[Json], context: _Context) -> Json:
    consumes = raw.get("consumes", context.consumes)  # an empty list clears the global value
    produces = raw.get("produces", context.produces)
    merged: dict[tuple[str, str], Json] = {}
    for item in (*path_parameters, *raw.get("parameters", [])):  # the operation overrides the path
        resolved = _resolve_parameter(item, context)
        merged[(resolved["in"], resolved["name"])] = resolved
    others = [p for p in merged.values() if p["in"] not in ("body", "formData")]
    body = next((p for p in merged.values() if p["in"] == "body"), None)
    fields = [p for p in merged.values() if p["in"] == "formData"]

    out: Json = {}
    for key, value in raw.items():
        if key in _OPERATION_KEYS or key.startswith("x-"):
            out[key] = value
    if others:
        out["parameters"] = [_parameter(p, context) for p in others]
    if body is not None:
        schema = context.body_schema(body)
        request: Json = {
            "required": bool(body.get("required", False)),
            "content": _spend_all({media: {"schema": schema} for media in consumes}, context),
        }
        if "description" in body:
            request["description"] = body["description"]
        out["requestBody"] = request
    elif fields:
        out["requestBody"] = _form_body(fields, consumes, context.budget)
    out["responses"] = {
        str(status): response
        if str(status).startswith("x-")
        else _response(response, produces, context)
        for status, response in raw.get("responses", {}).items()
    }
    return out


def _path_item(item: Json, context: _Context) -> Json:
    out: Json = {}
    path_parameters = item.get("parameters", [])
    for key, value in item.items():
        if key in HTTP_METHODS:
            out[key] = _operation(value, path_parameters, context)
        elif key == "$ref" or key.startswith("x-"):
            out[key] = value
    return out


def _spend_all(content: Json, context: _Context) -> Json:
    for _ in content:
        context.budget.spend()
    return content


def _response(raw: Json, produces: list[str], context: _Context) -> Json:
    if "$ref" in raw:
        return {"$ref": _ref(raw["$ref"])}
    out: Json = {"description": raw["description"]}
    if "schema" in raw:
        examples = raw.get("examples", {})
        media_types = [*produces, *(media for media in examples if media not in produces)]
        schema = _schema(raw["schema"])  # once: every media type offers the same one
        content: Json = {}
        for media in media_types:
            context.budget.spend()
            entry: Json = {"schema": schema}
            if media in examples:
                entry["example"] = examples[media]
            content[media] = entry
        out["content"] = content
    if "headers" in raw:
        out["headers"] = {
            name: {
                **({"description": header["description"]} if "description" in header else {}),
                "schema": _simple_schema(header, context.budget),
            }
            for name, header in raw["headers"].items()
        }
    return out


def _security_scheme(raw: Json) -> Json:
    kind = raw["type"]
    out: Json
    if kind == "basic":
        out = {"type": "http", "scheme": "basic"}
    elif kind == "apiKey":
        out = {"type": "apiKey", "name": raw["name"], "in": raw["in"]}
    else:  # oauth2: 2.0 allows one flow per scheme
        flow, urls = _FLOWS[raw["flow"]]
        out = {
            "type": "oauth2",
            "flows": {
                flow: {
                    **{key: raw[key] for key in urls if key in raw},
                    "scopes": raw.get("scopes", {}),
                }
            },
        }
    if "description" in raw:
        out["description"] = raw["description"]
    return out
