import json
from pathlib import Path
from typing import Any

import pytest
import yaml

from hermetic.ingest import IngestError, ingest
from hermetic.ingest import openapi as openapi_module
from hermetic.ingest.openapi import (
    build_operations,
    canonical_hash,
    deref,
    load_spec_file,
    validate_spec,
)

PETSTORE = Path(__file__).parent.parent / "fixtures" / "petstore" / "openapi.json"
OK = {"200": {"description": "ok"}}


def make_spec(paths: dict[str, Any], **extra: Any) -> dict[str, Any]:
    return {"openapi": "3.0.3", "info": {"title": "T", "version": "1"}, "paths": paths, **extra}


def fragment_hashes(spec: dict[str, Any]) -> dict[str, str]:
    return {op.operation_id: op.fragment_hash for op in build_operations(spec)}


# --- Petstore fixture -------------------------------------------------------------------


def test_petstore_model() -> None:
    model = ingest(PETSTORE)
    assert model.title == "Swagger Petstore - OpenAPI 3.0"
    assert model.openapi_version.startswith("3.0")
    assert model.servers == ["/api/v3"]
    assert model.docs == []
    ids = {op.operation_id for op in model.operations}
    assert {"addPet", "getPetById", "findPetsByStatus", "deletePet"} <= ids
    assert len(model.operations) == len(ids)  # operation ids are unique


def test_petstore_operation_details() -> None:
    ops = {op.operation_id: op for op in ingest(PETSTORE).operations}
    get_pet = ops["getPetById"]
    assert (get_pet.method, get_pet.path) == ("GET", "/pet/{petId}")
    (param,) = get_pet.parameters
    assert (param.name, param.location, param.required) == ("petId", "path", True)
    assert {r.status for r in get_pet.responses} >= {"200", "400", "404"}
    add_pet = ops["addPet"]
    assert add_pet.request_body is not None
    assert add_pet.request_body.required is True
    schema = add_pet.request_body.content["application/json"]
    assert "$ref" not in json.dumps(schema)  # references were inlined
    assert "name" in schema["properties"]


def test_hash_is_stable_and_tracks_changes() -> None:
    assert ingest(PETSTORE).spec_hash == ingest(PETSTORE).spec_hash
    original = json.loads(PETSTORE.read_text())
    changed = json.loads(PETSTORE.read_text())
    changed["components"]["schemas"]["Pet"]["properties"]["name"]["example"] = "changed"
    before, after = fragment_hashes(original), fragment_hashes(changed)
    # a change to a referenced component changes the operations that use it, and only those
    assert before["addPet"] != after["addPet"]
    assert before["getPetById"] != after["getPetById"]
    assert before["deletePet"] == after["deletePet"]
    assert canonical_hash(original) != canonical_hash(changed)
    assert canonical_hash(original) == ingest(PETSTORE).spec_hash


# --- loading and validation -------------------------------------------------------------


def test_yaml_spec_loads(tmp_path: Path) -> None:
    path = tmp_path / "api.yaml"
    path.write_text(yaml.safe_dump(make_spec({"/a": {"get": {"responses": OK}}})))
    assert [op.operation_id for op in ingest(path).operations] == ["get_a"]


def test_unreadable_spec(tmp_path: Path) -> None:
    with pytest.raises(IngestError, match="cannot read spec"):
        load_spec_file(tmp_path / "missing.yaml")


@pytest.mark.parametrize(("name", "text"), [("a.json", "{nope"), ("a.yaml", "key: [unclosed")])
def test_unparseable_spec(tmp_path: Path, name: str, text: str) -> None:
    path = tmp_path / name
    path.write_text(text)
    with pytest.raises(IngestError, match="cannot parse spec"):
        load_spec_file(path)


def test_spec_must_be_an_object(tmp_path: Path) -> None:
    path = tmp_path / "a.yaml"
    path.write_text("- just\n- a list\n")
    with pytest.raises(IngestError, match="top level"):
        load_spec_file(path)


@pytest.mark.parametrize("spec", [{"swagger": "2.0"}, {"openapi": "2.0"}, {"openapi": 3}])
def test_only_openapi_3_is_supported(spec: dict[str, Any]) -> None:
    with pytest.raises(IngestError, match=r"only OpenAPI 3\.x"):
        validate_spec(spec)


def test_invalid_document_is_rejected() -> None:
    with pytest.raises(IngestError, match="invalid OpenAPI document"):
        validate_spec({"openapi": "3.0.3", "info": {"title": "T"}, "paths": {}})


# --- $ref handling ----------------------------------------------------------------------


def test_deref_inlines_local_refs() -> None:
    root = {"components": {"schemas": {"A": {"type": "string"}}}}
    node = {"items": [{"$ref": "#/components/schemas/A"}], "n": 1}
    assert deref(node, root) == {"items": [{"type": "string"}], "n": 1}


def test_deref_leaves_recursive_refs_in_place() -> None:
    child = {"$ref": "#/components/schemas/Node"}
    root = {"components": {"schemas": {"Node": {"properties": {"child": child}}}}}
    result = deref({"$ref": "#/components/schemas/Node"}, root)
    assert result["properties"]["child"] == child


def test_deref_property_named_ref_is_not_a_reference() -> None:
    node = {"properties": {"$ref": {"type": "string"}}}
    assert deref(node, {}) == node


def test_deref_rejects_external_refs() -> None:
    with pytest.raises(IngestError, match="only local"):
        deref({"$ref": "other.yaml#/X"}, {})


@pytest.mark.parametrize("ref", ["#/components/missing", "#/a/b"])
def test_deref_rejects_unresolvable_refs(ref: str) -> None:
    with pytest.raises(IngestError, match="unresolvable"):
        deref({"$ref": ref}, {"a": {"b2": 1}})


def test_deref_handles_escaped_pointer_segments() -> None:
    root = {"paths": {"/a~b": {"x": 1}}}
    assert deref({"$ref": "#/paths/~1a~0b/x"}, root) == 1


# --- operation building -----------------------------------------------------------------


def test_synthesized_ids_and_duplicates() -> None:
    spec = make_spec({"/pets/{id}": {"get": {"responses": OK}}})
    assert build_operations(spec)[0].operation_id == "get_pets_id"
    clash = make_spec({"/a-b": {"get": {"responses": OK}}, "/a_b": {"get": {"responses": OK}}})
    with pytest.raises(IngestError, match="duplicate operationId"):
        build_operations(clash)


def test_path_parameters_are_merged_and_overridden() -> None:
    shared = {"name": "id", "in": "path", "schema": {"type": "string"}}
    q = {"name": "q", "in": "query", "schema": {"type": "string"}}
    override = {**q, "required": True}
    spec = make_spec(
        {
            "/x/{id}": {
                "parameters": [shared, q],
                "get": {"operationId": "x", "parameters": [override], "responses": OK},
            }
        }
    )
    params = {p.name: p for p in build_operations(spec)[0].parameters}
    assert params["id"].required is True  # path params are always required
    assert params["q"].required is True  # operation-level override wins
    assert params["id"].json_schema == {"type": "string"}


def test_optional_parameter_without_schema() -> None:
    spec = make_spec(
        {"/x": {"get": {"parameters": [{"name": "h", "in": "header"}], "responses": OK}}}
    )
    (param,) = build_operations(spec)[0].parameters
    assert (param.required, param.json_schema) == (False, {})


def test_security_defaults_and_overrides() -> None:
    spec = make_spec(
        {
            "/a": {"get": {"responses": OK}},
            "/b": {"get": {"responses": OK, "security": []}},
        },
        security=[{"key": []}],
        components={"securitySchemes": {"key": {"type": "apiKey", "name": "k", "in": "header"}}},
    )
    a, b = build_operations(spec)
    assert a.security == [{"key": []}]
    assert b.security == []


def test_request_body_without_required_and_deprecated_flag() -> None:
    body: dict[str, Any] = {"content": {"text/plain": {}}}
    spec = make_spec({"/x": {"post": {"requestBody": body, "deprecated": True, "responses": OK}}})
    (op,) = build_operations(spec)
    assert op.deprecated is True
    assert op.request_body is not None
    assert (op.request_body.required, op.request_body.content) == (False, {"text/plain": {}})


def test_operation_without_body_and_non_method_keys_ignored() -> None:
    spec = make_spec({"/x": {"summary": "not a method", "get": {"responses": OK}}})
    (op,) = build_operations(spec)
    assert op.request_body is None
    assert op.tags == []


# --- hostile and awkward input ----------------------------------------------------------

YAML_WITH_DATES_AND_INT_KEYS = """
openapi: 3.0.3
info: {title: T, version: '1'}
paths:
  /a:
    get:
      responses:
        200:
          description: ok
          content:
            application/json:
              schema: {type: string, example: 2024-01-01}
        default: {description: error}
"""


def test_yaml_dates_and_int_keys_are_normalised(tmp_path: Path) -> None:
    path = tmp_path / "api.yaml"
    path.write_text(YAML_WITH_DATES_AND_INT_KEYS)
    model = ingest(path)  # used to crash in canonical_hash with a TypeError
    (op,) = model.operations
    assert [r.status for r in op.responses] == ["200", "default"]
    assert op.responses[0].content["application/json"]["example"] == "2024-01-01"
    assert len(model.spec_hash) == 64


def test_yaml_datetimes_become_iso_strings(tmp_path: Path) -> None:
    path = tmp_path / "x.yaml"
    path.write_text("when: 2024-01-01 10:30:00\n")
    assert load_spec_file(path) == {"when": "2024-01-01T10:30:00"}


@pytest.mark.parametrize("tag", ["!!binary aGk=", "!!set {a, b}"])
def test_non_json_yaml_values_are_rejected(tmp_path: Path, tag: str) -> None:
    path = tmp_path / "x.yaml"
    path.write_text(f"value: {tag}\n")
    with pytest.raises(IngestError, match="unsupported value"):
        load_spec_file(path)


def test_yaml_alias_bomb_is_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(openapi_module, "MAX_SPEC_NODES", 10_000)
    lines = ["l0: &l0 [x, x, x, x, x, x, x, x, x, x]"]
    for level in range(1, 8):  # each level is 10 references to the previous one
        refs = ", ".join([f"*l{level - 1}"] * 10)
        lines.append(f"l{level}: &l{level} [{refs}]")
    path = tmp_path / "bomb.yaml"
    path.write_text("\n".join(lines) + "\n")
    with pytest.raises(IngestError, match="too large"):
        load_spec_file(path)


def test_deeply_nested_document_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "deep.json"
    path.write_text("[" * 20_000 + "]" * 20_000)
    with pytest.raises(IngestError, match="cannot parse spec"):
        load_spec_file(path)


def test_canonical_hash_reports_unhashable_values() -> None:
    circular: dict[str, Any] = {}
    circular["self"] = circular
    deep: Any = []
    for _ in range(20_000):  # above the C JSON encoder's recursion limit
        deep = [deep]
    for bad in ({"a": object()}, {1: "a", "b": 2}, circular, deep):
        with pytest.raises(IngestError, match="cannot hash document"):
            canonical_hash(bad)


@pytest.mark.parametrize(
    "ref", ["file:///etc/hostname", "http://127.0.0.1:9/x.yaml#/A", "other.yaml"]
)
def test_external_refs_are_rejected_before_the_validator_runs(
    ref: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    def must_not_run(spec: Any) -> None:
        raise AssertionError("the validator would have resolved the reference")

    monkeypatch.setattr(openapi_module, "validate", must_not_run)
    schema = {"allOf": [{"$ref": ref}]}  # nested inside a list
    content = {"application/json": {"schema": schema}}
    spec = make_spec(
        {"/a": {"get": {"responses": {"200": {"description": "ok", "content": content}}}}}
    )
    with pytest.raises(IngestError, match="only local"):
        validate_spec(spec)


def test_unresolvable_local_ref_is_a_clean_error() -> None:
    schema = {"$ref": "#/components/schemas/Nope"}
    content = {"application/json": {"schema": schema}}
    spec = make_spec(
        {"/a": {"get": {"responses": {"200": {"description": "ok", "content": content}}}}}
    )
    with pytest.raises(IngestError, match="invalid OpenAPI document"):
        validate_spec(spec)


def test_validator_recursion_error_is_a_clean_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def too_deep(spec: Any) -> None:
        raise RecursionError

    monkeypatch.setattr(openapi_module, "validate", too_deep)
    with pytest.raises(IngestError, match="invalid OpenAPI document"):
        validate_spec(make_spec({}))


def fan_out_spec(levels: int) -> dict[str, Any]:
    """Each schema references the previous one twice, so inlining doubles at every level."""
    schemas: dict[str, Any] = {"S0": {"type": "string"}}
    for i in range(1, levels):
        prev = {"$ref": f"#/components/schemas/S{i - 1}"}
        schemas[f"S{i}"] = {"type": "object", "properties": {"a": prev, "b": prev}}
    schema = {"$ref": f"#/components/schemas/S{levels - 1}"}
    content = {"application/json": {"schema": schema}}
    paths = {"/a": {"get": {"responses": {"200": {"description": "ok", "content": content}}}}}
    return make_spec(paths, components={"schemas": schemas})


def test_reference_fan_out_is_capped(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(openapi_module, "MAX_EXPANDED_NODES", 5_000)
    with pytest.raises(IngestError, match="too large"):
        build_operations(fan_out_spec(30))  # would expand to about 2**30 nodes


def test_modest_fan_out_still_works() -> None:
    (op,) = build_operations(fan_out_spec(6))
    assert op.responses[0].content["application/json"]["type"] == "object"


def test_deep_reference_chain_is_a_clean_error() -> None:
    schemas: dict[str, Any] = {"S2000": {"type": "string"}}
    for i in range(2000):
        schemas[f"S{i}"] = {"properties": {"x": {"$ref": f"#/components/schemas/S{i + 1}"}}}
    content = {"application/json": {"schema": {"$ref": "#/components/schemas/S0"}}}
    paths = {"/a": {"get": {"responses": {"200": {"description": "ok", "content": content}}}}}
    with pytest.raises(IngestError, match="nested too deeply"):
        build_operations(make_spec(paths, components={"schemas": schemas}))


def test_lone_surrogate_in_json_is_a_clean_error() -> None:
    document = json.loads('{"a": "\\ud800"}')  # loads fine, cannot be encoded as UTF-8
    with pytest.raises(IngestError, match="cannot hash document"):
        canonical_hash(document)


def test_total_expansion_is_capped_across_path_items(monkeypatch: pytest.MonkeyPatch) -> None:
    base = fan_out_spec(6)
    shared = base["paths"]["/a"]
    spec = {**base, "paths": {f"/p{i}": shared for i in range(50)}}
    assert len(build_operations(spec)) == 50  # every path item is far below the per-item cap
    monkeypatch.setattr(openapi_module, "MAX_TOTAL_EXPANDED_NODES", 2_000)
    with pytest.raises(IngestError, match="document after inlining"):
        build_operations(spec)


def test_specification_extensions_are_ignored() -> None:
    spec = make_spec(
        {"x-internal": {"note": "ext"}, "/a": {"get": {"responses": {**OK, "x-note": "text"}}}}
    )
    validate_spec(spec)  # valid per the OpenAPI schema
    (op,) = build_operations(spec)
    assert [r.status for r in op.responses] == ["200"]


@pytest.mark.parametrize("value", [True, None])
def test_validator_crash_on_scalar_extension_is_a_clean_error(value: Any) -> None:
    spec = make_spec({"x-internal": value, "/a": {"get": {"responses": OK}}})
    with pytest.raises(IngestError, match=r"cannot validate OpenAPI document \(KeyError"):
        validate_spec(spec)
    (op,) = build_operations(spec)  # the builder itself skips the extension
    assert op.path == "/a"


@pytest.mark.parametrize("key", ["on", "yes", "null", "~"])
def test_yaml_keyword_keys_are_rejected_not_renamed(tmp_path: Path, key: str) -> None:
    path = tmp_path / "x.yaml"
    path.write_text(f"{key}: 1\n")  # YAML 1.1 reads these as True / None
    with pytest.raises(IngestError, match="quote it"):
        load_spec_file(path)


def test_quoted_keyword_keys_stay_text(tmp_path: Path) -> None:
    path = tmp_path / "x.yaml"
    path.write_text('"on": 1\n"null": 2\n404: 3\n')
    assert load_spec_file(path) == {"on": 1, "null": 2, "404": 3}


@pytest.mark.parametrize(
    ("name", "text"),
    [("a.json", '{"a": ' + "1" * 5000 + "}"), ("a.yaml", "a: " + "1" * 5000)],
)
def test_huge_integer_literals_are_a_clean_error(tmp_path: Path, name: str, text: str) -> None:
    path = tmp_path / name
    path.write_text(text)  # int() refuses more than 4300 digits with a plain ValueError
    with pytest.raises(IngestError, match="cannot parse spec"):
        load_spec_file(path)


def auth_spec() -> dict[str, Any]:
    scheme = {"type": "apiKey", "in": "header"}
    return make_spec(
        {
            "/a": {"get": {"responses": OK}},  # inherits the document-level security
            "/b": {"get": {"responses": OK, "security": []}},  # opts out
        },
        security=[{"key": []}],
        components={
            "securitySchemes": {"key": {**scheme, "name": "k"}, "unused": {**scheme, "name": "u"}}
        },
    )


def test_fragment_hash_tracks_inherited_security() -> None:
    base = fragment_hashes(auth_spec())

    default_changed = auth_spec()
    default_changed["security"] = [{"unused": []}]
    after = fragment_hashes(default_changed)
    assert after["get_a"] != base["get_a"]  # inherits the changed default
    assert after["get_b"] == base["get_b"]  # explicit security is untouched

    scheme_changed = auth_spec()
    scheme_changed["components"]["securitySchemes"]["key"]["name"] = "renamed"
    after = fragment_hashes(scheme_changed)
    assert after["get_a"] != base["get_a"]  # the scheme it uses changed
    assert after["get_b"] == base["get_b"]

    unrelated = auth_spec()
    unrelated["components"]["securitySchemes"]["unused"]["name"] = "renamed"
    assert fragment_hashes(unrelated) == base  # a scheme nobody uses marks nothing


def test_security_requirement_naming_an_undefined_scheme_is_tolerated() -> None:
    spec = auth_spec()
    spec["security"] = [{"missing": []}]
    assert fragment_hashes(spec)["get_a"]  # hashed without a definition, no crash


def test_parameter_described_with_content_keeps_its_schema() -> None:
    param = {
        "name": "filter",
        "in": "query",
        "content": {"application/json": {"schema": {"type": "object"}}},
    }
    spec = make_spec({"/x": {"get": {"parameters": [param], "responses": OK}}})
    (parameter,) = build_operations(spec)[0].parameters
    assert (parameter.json_schema, parameter.media_type) == ({"type": "object"}, "application/json")


def test_parameter_content_without_schema_and_plain_schema() -> None:
    bare = {"name": "a", "in": "query", "content": {"text/plain": {}}}
    plain = {"name": "b", "in": "query", "schema": {"type": "string"}}
    spec = make_spec({"/x": {"get": {"parameters": [bare, plain], "responses": OK}}})
    a, b = build_operations(spec)[0].parameters
    assert (a.json_schema, a.media_type) == ({}, "text/plain")
    assert (b.json_schema, b.media_type) == ({"type": "string"}, None)


def chain_schemas(length: int) -> dict[str, Any]:
    schemas: dict[str, Any] = {f"S{length}": {"type": "string"}}
    for i in range(length):
        schemas[f"S{i}"] = {"properties": {"x": {"$ref": f"#/components/schemas/S{i + 1}"}}}
    return schemas


def test_security_scheme_fan_out_counts_toward_the_total(monkeypatch: pytest.MonkeyPatch) -> None:
    spec = auth_spec()
    spec["components"]["schemas"] = fan_out_spec(6)["components"]["schemas"]
    spec["components"]["securitySchemes"]["key"]["x-shape"] = {"$ref": "#/components/schemas/S5"}
    assert fragment_hashes(spec)  # fine under the default caps
    monkeypatch.setattr(openapi_module, "MAX_TOTAL_EXPANDED_NODES", 100)
    with pytest.raises(IngestError, match="too large"):
        build_operations(spec)


def test_security_scheme_deep_reference_chain_is_a_clean_error() -> None:
    spec = auth_spec()
    spec["components"]["schemas"] = chain_schemas(2000)
    spec["components"]["securitySchemes"]["key"]["x-deep"] = {"$ref": "#/components/schemas/S0"}
    with pytest.raises(IngestError, match="security schemes: references are nested too deeply"):
        build_operations(spec)


def server_spec(document_url: str) -> dict[str, Any]:
    return make_spec(
        {
            "/a": {"get": {"responses": OK}},  # inherits the document's servers
            "/b": {"servers": [{"url": "https://path.example"}], "get": {"responses": OK}},
            "/c": {"get": {"responses": OK, "servers": [{"url": "https://op.example"}]}},
        },
        servers=[{"url": document_url}],
    )


def test_fragment_hash_tracks_inherited_servers() -> None:
    before = fragment_hashes(server_spec("https://one.example"))
    after = fragment_hashes(server_spec("https://two.example"))
    assert after["get_a"] != before["get_a"]  # inherits the changed document-level server
    assert after["get_b"] == before["get_b"]  # path-item servers override it
    assert after["get_c"] == before["get_c"]  # so do operation-level servers
