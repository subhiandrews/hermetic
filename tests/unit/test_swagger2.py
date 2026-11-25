"""Swagger 2.0 files are converted to OpenAPI 3.0.3 when they are read."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

import hermetic.ingest as ingest_module
from hermetic.ingest import IngestError, ingest
from hermetic.ingest.openapi import validate_spec, validate_swagger2
from hermetic.ingest.swagger2 import upgrade

OK = {"200": {"description": "ok"}}
TOKEN_URL = "https://a.example/token"
AUTH_URL = "https://a.example/auth"


def doc(**extra: Any) -> dict[str, Any]:
    return {"swagger": "2.0", "info": {"title": "T", "version": "1"}, "paths": {}, **extra}


def converted(paths: dict[str, Any], **extra: Any) -> dict[str, Any]:
    """Convert a document, checking both ends: the input is valid 2.0 and the output valid 3.x."""
    spec = doc(paths=paths, **extra)
    validate_swagger2(spec)
    out = upgrade(spec)
    validate_spec(out)
    return out


def operation(**extra: Any) -> dict[str, Any]:
    return {"responses": OK, **extra}


def only_operation(out: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = out["paths"]["/a"]["get"]
    return result


def with_parameters(*parameters: dict[str, Any], **extra: Any) -> dict[str, Any]:
    return converted({"/a": {"get": operation(parameters=list(parameters), **extra)}})


# --- the document ----------------------------------------------------------------------------


def test_the_version_and_info_are_converted() -> None:
    out = converted({})
    assert out["openapi"] == "3.0.3" and out["info"] == {"title": "T", "version": "1"}
    assert "servers" not in out and "components" not in out


@pytest.mark.parametrize(
    ("extra", "servers"),
    [
        (
            {"host": "shop.example", "basePath": "/api/", "schemes": ["https", "wss"]},
            ["https://shop.example/api", "wss://shop.example/api"],
        ),
        ({"host": "shop.example"}, ["http://shop.example"]),
        ({"basePath": "/api"}, ["/api"]),
    ],
)
def test_servers_come_from_host_base_path_and_schemes(
    extra: dict[str, Any], servers: list[str]
) -> None:
    assert [s["url"] for s in converted({}, **extra)["servers"]] == servers


@pytest.mark.parametrize("extra", [{"basePath": "/"}, {}])
def test_no_host_and_no_real_base_path_means_no_servers(extra: dict[str, Any]) -> None:
    assert "servers" not in converted({}, **extra)


def test_extensions_tags_security_and_external_docs_are_kept() -> None:
    out = converted(
        {"x-paths-note": {"n": 1}},
        **{
            "x-top": {"a": 1},
            "tags": [{"name": "t"}],
            "externalDocs": {"url": "https://example.com"},
            "securityDefinitions": {"key": {"type": "apiKey", "name": "k", "in": "header"}},
            "security": [{"key": []}],
        },
    )
    assert out["x-top"] == {"a": 1} and out["tags"] == [{"name": "t"}]
    assert out["externalDocs"] == {"url": "https://example.com"}
    assert out["security"] == [{"key": []}] and out["paths"] == {"x-paths-note": {"n": 1}}


# --- schemas ---------------------------------------------------------------------------------

PET = {
    "type": "object",
    "properties": {
        "friend": {"$ref": "#/definitions/Pet"},
        "tags": {"type": "array", "items": {"$ref": "#/definitions/Tag"}},
        "extra": {"type": "object", "additionalProperties": {"type": "string"}},
        "open": {"type": "object", "additionalProperties": True},
        "both": {"allOf": [{"$ref": "#/definitions/Tag"}, {"type": "object"}]},
    },
}
TAG = {"type": "string", "x-nullable": True, "default": "a", "example": {"type": "file"}}


def test_definitions_become_component_schemas_and_references_follow() -> None:
    body = {"in": "body", "name": "b", "schema": {"$ref": "#/definitions/Pet"}}
    out = converted(
        {"/a": {"get": operation(parameters=[body])}}, definitions={"Pet": PET, "Tag": TAG}
    )
    pet = out["components"]["schemas"]["Pet"]["properties"]
    assert pet["friend"] == {"$ref": "#/components/schemas/Pet"}
    assert pet["tags"]["items"] == {"$ref": "#/components/schemas/Tag"}
    assert pet["extra"]["additionalProperties"] == {"type": "string"}
    assert pet["open"]["additionalProperties"] is True
    assert pet["both"]["allOf"][0] == {"$ref": "#/components/schemas/Tag"}
    tag = out["components"]["schemas"]["Tag"]
    assert tag["nullable"] is True and tag["example"] == {"type": "file"}  # data is left alone
    schema = only_operation(out)["requestBody"]["content"]["application/json"]["schema"]
    assert schema == {"$ref": "#/components/schemas/Pet"}


def test_a_reference_outside_definitions_is_left_as_it_is() -> None:
    shared = {"type": "string"}
    out = converted({}, definitions={"X": {"$ref": "#/x-shared"}}, **{"x-shared": shared})
    assert out["components"]["schemas"]["X"] == {"$ref": "#/x-shared"}


def test_a_discriminator_name_becomes_an_object() -> None:
    pet = {"type": "object", "discriminator": "kind", "properties": {"kind": {"type": "string"}}}
    out = converted({}, definitions={"Pet": pet})
    assert out["components"]["schemas"]["Pet"]["discriminator"] == {"propertyName": "kind"}


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("string", {"type": "string"}),
        ("null", {"nullable": True}),
        (["string", "null"], {"type": "string", "nullable": True}),
        (["string", "integer"], {"anyOf": [{"type": "string"}, {"type": "integer"}]}),
    ],
)
def test_draft_4_types_become_3_0_types(raw: Any, expected: dict[str, Any]) -> None:
    out = converted({}, definitions={"X": {"type": raw}})
    assert out["components"]["schemas"]["X"] == expected


# --- parameters ------------------------------------------------------------------------------


def test_plain_parameters_move_their_type_under_schema() -> None:
    path = {"in": "path", "name": "id", "required": True, "type": "string", "format": "uuid"}
    query = {"in": "query", "name": "n", "type": "integer", "minimum": 1, "allowEmptyValue": True}
    header = {"in": "header", "name": "h", "type": "string", "enum": ["a"], "default": "a"}
    out = converted(
        {"/a/{id}": {"get": operation(parameters=[{**path, "description": "d"}, query, header])}}
    )
    got = out["paths"]["/a/{id}"]["get"]["parameters"]
    assert got[0] == {
        "name": "id",
        "in": "path",
        "required": True,
        "description": "d",
        "schema": {"type": "string", "format": "uuid"},
    }
    assert got[1]["schema"] == {"type": "integer", "minimum": 1}
    assert got[1]["allowEmptyValue"] is True
    assert got[2]["schema"] == {"type": "string", "enum": ["a"], "default": "a"}


@pytest.mark.parametrize(
    ("collection", "expected"),
    [
        (None, {"style": "form", "explode": False}),
        ("csv", {"style": "form", "explode": False}),
        ("ssv", {"style": "spaceDelimited", "explode": False}),
        ("pipes", {"style": "pipeDelimited", "explode": False}),
        ("multi", {}),
        ("tsv", {}),
    ],
)
def test_array_query_parameters_keep_their_collection_format(
    collection: str | None, expected: dict[str, Any]
) -> None:
    items = {"type": "string", "maxLength": 3}
    parameter: dict[str, Any] = {"in": "query", "name": "q", "type": "array", "items": items}
    if collection:
        parameter["collectionFormat"] = collection
    got = only_operation(with_parameters(parameter))["parameters"][0]
    assert got["schema"] == {"type": "array", "items": items}
    assert {k: v for k, v in got.items() if k in ("style", "explode")} == expected


def test_a_body_becomes_a_request_body_for_every_media_type_the_operation_consumes() -> None:
    body = {"in": "body", "name": "b", "required": True, "description": "the pet", "schema": {}}
    out = converted(
        {"/a": {"get": operation(parameters=[body], consumes=["application/json", "text/xml"])}},
        consumes=["text/plain"],
    )
    request = only_operation(out)["requestBody"]
    assert request["required"] is True and request["description"] == "the pet"
    assert set(request["content"]) == {"application/json", "text/xml"}


def test_a_body_uses_the_documents_consumes_and_is_optional_by_default() -> None:
    body = {"in": "body", "name": "b", "schema": {"type": "string"}}
    out = converted({"/a": {"get": operation(parameters=[body])}}, consumes=["text/plain"])
    request = only_operation(out)["requestBody"]
    assert request == {"required": False, "content": {"text/plain": {"schema": {"type": "string"}}}}


def test_form_fields_become_one_object_schema() -> None:
    name = {
        "in": "formData",
        "name": "name",
        "type": "string",
        "required": True,
        "description": "w",
    }
    age = {"in": "formData", "name": "age", "type": "integer"}
    request = only_operation(with_parameters(name, age))["requestBody"]
    assert request["required"] is True
    schema = request["content"]["application/x-www-form-urlencoded"]["schema"]
    assert schema["required"] == ["name"]
    assert schema["properties"] == {
        "name": {"type": "string", "description": "w"},
        "age": {"type": "integer"},
    }


def test_optional_form_fields_leave_the_body_optional() -> None:
    age = {"in": "formData", "name": "age", "type": "integer"}
    request = only_operation(with_parameters(age))["requestBody"]
    schema = request["content"]["application/x-www-form-urlencoded"]["schema"]
    assert request["required"] is False and "required" not in schema


@pytest.mark.parametrize(
    ("collection", "encoding"),
    [
        (None, {"style": "form", "explode": False}),
        ("pipes", {"style": "pipeDelimited", "explode": False}),
        ("multi", None),
    ],
)
def test_array_form_fields_keep_their_collection_format(
    collection: str | None, encoding: dict[str, Any] | None
) -> None:
    field: dict[str, Any] = {
        "in": "formData",
        "name": "ids",
        "type": "array",
        "items": {"type": "string"},
    }
    if collection:
        field["collectionFormat"] = collection
    media = only_operation(with_parameters(field))["requestBody"]["content"]
    entry = media["application/x-www-form-urlencoded"]
    assert entry.get("encoding") == ({"ids": encoding} if encoding else None)


def test_a_file_field_makes_the_body_multipart() -> None:
    upload = {"in": "formData", "name": "f", "type": "file"}
    content = only_operation(with_parameters(upload))["requestBody"]["content"]
    assert list(content) == ["multipart/form-data"]
    assert content["multipart/form-data"]["schema"]["properties"]["f"] == {
        "type": "string",
        "format": "binary",
    }


def test_a_multipart_consumes_makes_the_body_multipart() -> None:
    field = {"in": "formData", "name": "f", "type": "string"}
    out = with_parameters(field, consumes=["multipart/form-data"])
    assert list(only_operation(out)["requestBody"]["content"]) == ["multipart/form-data"]


def test_named_parameters_are_inlined_where_they_are_used() -> None:
    uses = [{"$ref": "#/parameters/limit"}, {"$ref": "#/parameters/payload"}]
    out = converted(
        {"/a": {"get": operation(parameters=uses)}},
        parameters={
            "limit": {"in": "query", "name": "limit", "type": "integer"},
            "payload": {"in": "body", "name": "payload", "schema": {"type": "object"}},
        },
    )
    got = only_operation(out)
    assert got["parameters"] == [{"name": "limit", "in": "query", "schema": {"type": "integer"}}]
    assert "application/json" in got["requestBody"]["content"]
    assert "parameters" not in out.get("components", {})


def test_a_reference_to_somewhere_else_is_refused() -> None:
    elsewhere = [{"$ref": "#/paths/~1b/get/parameters/0"}]
    spec = doc(paths={"/a": {"get": operation(parameters=elsewhere)}})
    with pytest.raises(IngestError, match="unsupported parameter reference"):
        upgrade(spec)


def test_path_level_parameters_apply_to_every_operation_and_operations_override_them() -> None:
    item = {
        "parameters": [
            {"in": "query", "name": "q", "type": "string"},
            {"in": "query", "name": "p", "type": "string"},
        ],
        "get": operation(parameters=[{"in": "query", "name": "q", "type": "integer"}]),
        "put": operation(),
    }
    out = converted({"/a": item})
    assert "parameters" not in out["paths"]["/a"]
    get = {p["name"]: p["schema"]["type"] for p in out["paths"]["/a"]["get"]["parameters"]}
    assert get == {"q": "integer", "p": "string"}
    assert len(out["paths"]["/a"]["put"]["parameters"]) == 2


# --- operations and responses ----------------------------------------------------------------


def test_operation_fields_are_kept_and_an_operation_may_have_no_parameters() -> None:
    fields = {
        "operationId": "listA",
        "summary": "s",
        "description": "d",
        "tags": ["t"],
        "deprecated": True,
        "externalDocs": {"url": "https://example.com"},
        "security": [],
        "x-op": True,
    }
    out = converted({"/a": {"x-item": 1, "get": operation(**fields)}})
    got = only_operation(out)
    assert got["operationId"] == "listA" and got["deprecated"] is True and got["security"] == []
    assert got["x-op"] is True and out["paths"]["/a"]["x-item"] == 1
    assert "parameters" not in got and "requestBody" not in got


def test_a_path_item_reference_is_passed_through() -> None:
    out = upgrade(doc(paths={"/a": {"$ref": "#/paths/~1b"}, "/b": {"get": operation()}}))
    assert out["paths"]["/a"] == {"$ref": "#/paths/~1b"}


def test_a_response_schema_is_offered_in_every_media_type_the_operation_produces() -> None:
    response = {
        "description": "ok",
        "schema": {"type": "string"},
        "examples": {"text/plain": "hi", "text/csv": "a,b"},
    }
    get = {"produces": ["application/json", "text/plain"], "responses": {"200": response}}
    content = only_operation(converted({"/a": {"get": get}}))["responses"]["200"]["content"]
    assert list(content) == ["application/json", "text/plain", "text/csv"]
    assert content["text/plain"] == {"schema": {"type": "string"}, "example": "hi"}
    assert content["application/json"] == {"schema": {"type": "string"}}


def test_response_extensions_are_kept_and_not_read_as_responses() -> None:
    get = {"responses": {"200": {"description": "ok"}, "x-note": {"a": 1}}}
    got = only_operation(converted({"/a": {"get": get}}))["responses"]
    assert got == {"200": {"description": "ok"}, "x-note": {"a": 1}}


def test_a_response_without_a_schema_has_no_content() -> None:
    got = only_operation(converted({"/a": {"get": operation()}}))["responses"]["200"]
    assert got == {"description": "ok"}


def test_response_headers_get_a_schema() -> None:
    headers = {"X-A": {"type": "integer", "description": "a"}, "X-B": {"type": "string"}}
    get = {"responses": {"200": {"description": "ok", "headers": headers}}}
    got = only_operation(converted({"/a": {"get": get}}))["responses"]["200"]
    assert got["headers"] == {
        "X-A": {"description": "a", "schema": {"type": "integer"}},
        "X-B": {"schema": {"type": "string"}},
    }


def test_named_responses_become_component_responses() -> None:
    out = converted(
        {"/a": {"get": {"responses": {"404": {"$ref": "#/responses/NotFound"}}}}},
        responses={"NotFound": {"description": "gone", "schema": {"$ref": "#/definitions/Err"}}},
        definitions={"Err": {"type": "object"}},
    )
    assert only_operation(out)["responses"]["404"] == {"$ref": "#/components/responses/NotFound"}
    content = out["components"]["responses"]["NotFound"]["content"]["application/json"]
    assert content["schema"] == {"$ref": "#/components/schemas/Err"}


# --- security --------------------------------------------------------------------------------


def test_security_schemes_are_converted() -> None:
    out = converted(
        {},
        securityDefinitions={
            "b": {"type": "basic", "description": "login"},
            "k": {"type": "apiKey", "name": "X-Key", "in": "header"},
            "i": {
                "type": "oauth2",
                "flow": "implicit",
                "authorizationUrl": AUTH_URL,
                "scopes": {"r": "read"},
            },
            "p": {"type": "oauth2", "flow": "password", "tokenUrl": TOKEN_URL},
            "a": {"type": "oauth2", "flow": "application", "tokenUrl": TOKEN_URL, "scopes": {}},
            "c": {
                "type": "oauth2",
                "flow": "accessCode",
                "authorizationUrl": AUTH_URL,
                "tokenUrl": TOKEN_URL,
                "scopes": {},
            },
        },
    )
    schemes = out["components"]["securitySchemes"]
    assert schemes["b"] == {"type": "http", "scheme": "basic", "description": "login"}
    assert schemes["k"] == {"type": "apiKey", "name": "X-Key", "in": "header"}
    assert schemes["i"]["flows"] == {
        "implicit": {"authorizationUrl": AUTH_URL, "scopes": {"r": "read"}}
    }
    assert schemes["p"]["flows"] == {"password": {"tokenUrl": TOKEN_URL, "scopes": {}}}
    assert list(schemes["a"]["flows"]) == ["clientCredentials"]
    assert list(schemes["c"]["flows"]["authorizationCode"]) == [
        "authorizationUrl",
        "tokenUrl",
        "scopes",
    ]


# --- through ingest --------------------------------------------------------------------------

CUSTOMER = {"in": "path", "name": "customerId", "required": True, "type": "string"}
CART = {"$ref": "#/definitions/Cart"}
SWAGGER_FILE = {
    "swagger": "2.0",
    "info": {"title": "Carts", "version": "0.1"},
    "host": "carts",
    "basePath": "/",
    "paths": {
        "/carts/{customerId}": {
            "get": {
                "operationId": "getCart",
                "parameters": [CUSTOMER],
                "produces": ["application/json"],
                "responses": {"200": {"description": "ok", "schema": CART}},
            },
            "post": {
                "operationId": "addItem",
                "parameters": [
                    CUSTOMER,
                    {"in": "body", "name": "item", "required": True, "schema": CART},
                ],
                "responses": {"201": {"description": "created"}},
            },
        }
    },
    "definitions": {"Cart": {"type": "object", "properties": {"id": {"type": "string"}}}},
}


def write(tmp_path: Path, spec: dict[str, Any], name: str = "spec.yaml") -> Path:
    path = tmp_path / name
    path.write_text(yaml.safe_dump(spec))
    return path


def test_a_swagger_file_is_read_as_a_service_model(tmp_path: Path) -> None:
    model = ingest(write(tmp_path, SWAGGER_FILE))
    assert (model.title, model.openapi_version) == ("Carts", "3.0.3")
    assert model.servers == ["http://carts"]
    operations = {o.operation_id: o for o in model.operations}
    get, post = operations["getCart"], operations["addItem"]
    assert get.responses[0].content["application/json"] == {
        "type": "object",
        "properties": {"id": {"type": "string"}},
    }
    assert post.request_body is not None and post.request_body.required is True
    assert [p.name for p in post.parameters] == ["customerId"]


def test_the_same_swagger_file_gives_the_same_model(tmp_path: Path) -> None:
    path = write(tmp_path, SWAGGER_FILE)
    assert ingest(path) == ingest(path)


def test_an_invalid_swagger_file_is_rejected_before_conversion(tmp_path: Path) -> None:
    path = write(tmp_path, {"swagger": "2.0", "info": {"title": "T"}, "paths": {}})
    with pytest.raises(IngestError, match="invalid OpenAPI document"):
        ingest(path)


@pytest.mark.parametrize("version", ["1.2", "3.0", 2])
def test_only_swagger_2_0_is_converted(tmp_path: Path, version: Any) -> None:
    path = write(
        tmp_path, {"swagger": version, "info": {"title": "T", "version": "1"}, "paths": {}}
    )
    with pytest.raises(IngestError, match=r"only Swagger 2\.0 is supported"):
        ingest(path)


def test_a_document_the_converter_does_not_expect_is_a_clear_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def explode(spec: dict[str, Any]) -> dict[str, Any]:
        raise KeyError("definitions")

    monkeypatch.setattr(ingest_module, "upgrade", explode)
    with pytest.raises(IngestError, match=r"cannot convert the Swagger 2\.0 document \(KeyError"):
        ingest(write(tmp_path, SWAGGER_FILE))


def test_a_converted_document_that_is_not_valid_3_x_is_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The validator checks the converter's output, so its mistakes cannot reach the model."""
    monkeypatch.setattr(ingest_module, "upgrade", lambda spec: {"openapi": "3.0.3"})
    with pytest.raises(IngestError, match="invalid OpenAPI document"):
        ingest(write(tmp_path, SWAGGER_FILE))


@pytest.mark.parametrize("key", ["swagger", "openapi"])
def test_an_unquoted_version_gets_a_hint(tmp_path: Path, key: str) -> None:
    number = "2.0" if key == "swagger" else "3.0"
    path = tmp_path / "unquoted.yaml"
    path.write_text(f"{key}: {number}\ninfo: {{title: T, version: '1'}}\npaths: {{}}\n")
    with pytest.raises(IngestError, match="put the version in quotes"):
        ingest(path)


def test_a_file_with_no_version_names_both_formats(tmp_path: Path) -> None:
    path = write(tmp_path, {"info": {"title": "T", "version": "1"}, "paths": {}})
    with pytest.raises(IngestError, match=r"only OpenAPI 3\.x or Swagger 2\.0 is supported"):
        ingest(path)


# --- bounds ----------------------------------------------------------------------------------


def test_a_schema_is_converted_once_and_shared_by_every_media_type() -> None:
    body = {"in": "body", "name": "b", "schema": {"type": "object"}}
    out = converted({"/a": {"get": operation(parameters=[body], consumes=["a/b", "c/d"])}})
    content = only_operation(out)["requestBody"]["content"]
    assert content["a/b"]["schema"] is content["c/d"]["schema"]


def test_a_named_body_used_by_two_operations_is_converted_once() -> None:
    use = [{"$ref": "#/parameters/payload"}]
    paths = {"/a": {"get": operation(parameters=use), "put": operation(parameters=use)}}
    out = converted(
        paths, parameters={"payload": {"in": "body", "name": "p", "schema": {"type": "object"}}}
    )
    get, put = out["paths"]["/a"]["get"], out["paths"]["/a"]["put"]
    schema = get["requestBody"]["content"]["application/json"]["schema"]
    assert schema is put["requestBody"]["content"]["application/json"]["schema"]


def test_a_small_file_cannot_expand_into_a_huge_converted_document() -> None:
    schema = {
        "type": "object",
        "properties": {f"p{i}": {"type": "string"} for i in range(500)},
    }
    consumes = [f"application/x-{i}" for i in range(2000)]  # 2000 copies of a 1000-node schema
    body = {"in": "body", "name": "b", "schema": schema}
    spec = doc(paths={"/a": {"get": operation(parameters=[body], consumes=consumes)}})
    with pytest.raises(IngestError, match="converted document is too large"):
        upgrade(spec)
