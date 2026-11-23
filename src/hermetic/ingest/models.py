"""Data model produced by ingest: everything later stages know about the service."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

Json = dict[str, Any]
# JSON schemas in the model have their local `$ref`s inlined. The one exception is recursion:
# a reference back into a schema that is already being expanded stays as
# {"$ref": "#/components/schemas/Name"}. The model carries no components, so a consumer must
# read such a node as "the same shape as the enclosing schema Name" (an ancestor in the same
# tree), not try to resolve it.


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True)


class Parameter(_Frozen):
    name: str
    location: Literal["path", "query", "header", "cookie"]
    required: bool
    json_schema: Json | bool  # OpenAPI 3.1 allows the boolean schemas true and false
    # Set when the parameter is described with `content` instead of `schema` (valid OpenAPI);
    # json_schema then holds that media type's schema.
    media_type: str | None = None


class RequestBody(_Frozen):
    required: bool
    # media type -> JSON schema (references already resolved; may be a boolean in 3.1)
    content: dict[str, Json | bool]


class Response(_Frozen):
    status: str  # "200", "404", "default", ...
    description: str
    content: dict[str, Json | bool]


class Operation(_Frozen):
    operation_id: str
    method: str  # upper-case HTTP method
    path: str
    summary: str | None
    description: str | None
    tags: list[str]
    deprecated: bool
    parameters: list[Parameter]
    request_body: RequestBody | None
    responses: list[Response]
    security: list[dict[str, list[str]]]
    # Hash of the fully resolved operation. Changes whenever anything the operation depends
    # on changes (including referenced component schemas); the diff stage keys on it.
    fragment_hash: str


class DocFile(_Frozen):
    path: str  # relative to the docs root, POSIX separators
    kind: Literal["markdown", "text", "diagram"]
    sha256: str
    text: str


class ServiceModel(_Frozen):
    title: str
    version: str
    openapi_version: str
    servers: list[str]
    operations: list[Operation]
    docs: list[DocFile]
    spec_hash: str
