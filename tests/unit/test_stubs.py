from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from hermetic.cli import app
from hermetic.discover import Dependency, DependencySet, write_dependencies
from hermetic.ingest import ServiceModel, ingest
from hermetic.ingest.models import Operation, Response
from hermetic.provision import ProvisionError, build_stubs, write_stubs
from hermetic.provision import stubs as stubs_module
from hermetic.provision.stubs import sample

cli_runner = CliRunner()

SPEC = """
openapi: 3.0.3
info: {title: Payments, version: "1"}
servers: [{url: "https://pay.example.com/v1/"}]
paths:
  /charges/{id}:
    get:
      operationId: getCharge
      parameters: [{name: id, in: path, required: true, schema: {type: string}}]
      responses:
        "200":
          description: ok
          content:
            application/json:
              schema:
                type: object
                properties:
                  id: {type: string, format: uuid}
                  amount: {type: integer, minimum: 100}
                  paid: {type: boolean}
                  status: {type: string, enum: [succeeded, failed]}
        "404": {description: missing}
  /charges:
    post:
      operationId: createCharge
      responses:
        "201":
          description: made
          content:
            application/json:
              schema: {type: object, example: {id: ch_1}}
    delete:
      operationId: dropAll
      responses:
        "202": {description: queued, content: {text/plain: {schema: {type: string}}}}
  /charges.json:
    get:
      operationId: dotted
      responses:
        "204": {description: none}
  /broken:
    get:
      operationId: broken
      responses:
        "500": {description: boom}
"""


@pytest.fixture
def model(tmp_path: Path) -> ServiceModel:
    path = tmp_path / "payments.yaml"
    path.write_text(SPEC)
    return ingest(path)


# --- sample values ---


@pytest.mark.parametrize(
    ("schema", "expected"),
    [
        ({"example": {"a": 1}}, {"a": 1}),
        ({"default": 5, "type": "integer"}, 5),
        ({"const": "x"}, "x"),
        ({"examples": ["first", "second"]}, "first"),
        ({"enum": ["b", "a"]}, "b"),
        ({"oneOf": [{"type": "boolean"}, {"type": "string"}]}, True),
        ({"anyOf": [{"type": "string", "format": "date"}]}, "2020-01-01"),
        ({"type": "string"}, "string"),
        ({"type": "string", "format": "uuid"}, "00000000-0000-0000-0000-000000000000"),
        ({"type": "integer"}, 0),
        ({"type": "integer", "minimum": 5}, 5),
        ({"type": "integer", "maximum": -3}, -3),
        ({"type": "number"}, 0.0),
        ({"type": "boolean"}, True),
        ({"type": ["null", "string"]}, "string"),
        ({"type": ["null"]}, None),
        ({"type": "null"}, None),
        ({"type": "object"}, {}),
        ({"properties": {"a": {"type": "integer"}}}, {"a": 0}),
        ({"type": "array", "items": {"type": "boolean"}}, [True]),
        ({"type": "array"}, [None]),
        (
            {"allOf": [{"properties": {"a": {"type": "integer"}}}, {"properties": {"b": {}}}]},
            {"a": 0, "b": None},
        ),
        ({"allOf": [{"type": "string"}]}, "string"),
        ({"allOf": [{"type": "string"}, {"type": "object"}]}, "string"),
        ({"allOf": [{"properties": {"a": {"type": "integer"}}}, {"type": "string"}]}, {"a": 0}),
        ({"$ref": "#/components/schemas/Node"}, None),
        ({}, None),
        (True, None),
    ],
)
def test_sample_values(schema: Any, expected: Any) -> None:
    assert sample(schema) == expected


def test_sample_stops_at_the_depth_cap() -> None:
    schema: dict[str, Any] = {"type": "string"}
    for _ in range(20):
        schema = {"type": "object", "properties": {"n": schema}}
    text = json.dumps(sample(schema))
    assert text.count("{") == stubs_module.MAX_DEPTH + 1
    assert text.endswith("null" + "}" * (stubs_module.MAX_DEPTH + 1))


def test_sample_keeps_a_bounded_number_of_properties() -> None:
    wide = {"type": "object", "properties": {f"p{i}": {} for i in range(500)}}
    assert len(sample(wide)) == stubs_module.MAX_KEYS


# --- building stubs ---


def test_one_stub_per_operation_with_a_success_response(model: ServiceModel) -> None:
    stubs, skipped = build_stubs(model)
    by_op = {m["metadata"]["operation"]: m for m in stubs.values()}
    assert set(by_op) == {"getCharge", "createCharge", "dropAll", "dotted"}
    assert skipped == ["GET /broken: no 2xx response"]
    get = by_op["getCharge"]
    assert get["request"] == {"method": "GET", "urlPathPattern": "/v1/charges/[^/]+"}
    assert get["response"] == {
        "status": 200,
        "headers": {"Content-Type": "application/json"},
        "jsonBody": {
            "id": "00000000-0000-0000-0000-000000000000",
            "amount": 100,
            "paid": True,
            "status": "succeeded",
        },
    }
    assert get["priority"] == 2
    assert get["metadata"] == {"hermetic": True, "operation": "getCharge"}
    assert by_op["createCharge"]["response"]["status"] == 201
    assert by_op["createCharge"]["response"]["jsonBody"] == {"id": "ch_1"}
    assert by_op["createCharge"]["priority"] == 1
    assert by_op["dropAll"]["response"] == {"status": 202}  # not JSON: status only
    assert by_op["dotted"]["request"]["urlPathPattern"] == "/v1/charges\\.json"  # a dot is literal


def test_the_same_spec_gives_the_same_files(model: ServiceModel) -> None:
    first, _ = build_stubs(model)
    second, _ = build_stubs(model)
    assert first == second
    assert all(re.fullmatch(r"[a-z0-9-]+-[0-9a-f]{8}\.json", name) for name in first)


def test_without_servers_there_is_no_prefix(tmp_path: Path) -> None:
    path = tmp_path / "s.yaml"
    path.write_text(
        "openapi: 3.0.3\ninfo: {title: t, version: '1'}\npaths:\n  /a:\n    get:\n"
        "      operationId: a\n      responses: {'200': {description: ok}}\n"
    )
    stubs, _ = build_stubs(ingest(path))
    assert next(iter(stubs.values()))["request"]["urlPathPattern"] == "/a"


def test_a_relative_server_url_is_not_a_prefix(model: ServiceModel) -> None:
    other = model.model_copy(update={"servers": ["relative"]})
    stubs, _ = build_stubs(other)
    assert all(not m["request"]["urlPathPattern"].startswith("/v1") for m in stubs.values())


def _operation(method: str, status: str = "200", media: str = "application/json") -> Operation:
    return Operation(
        operation_id="Weird Name!!",
        method=method,
        path="/x",
        summary=None,
        description=None,
        tags=[],
        deprecated=False,
        parameters=[],
        request_body=None,
        responses=[Response(status=status, description="", content={media: {"type": "string"}})],
        security=[],
        fragment_hash="h",
    )


def test_unsupported_methods_are_skipped(model: ServiceModel) -> None:
    other = model.model_copy(update={"operations": [_operation("TRACE")]})
    stubs, skipped = build_stubs(other)
    assert stubs == {}
    assert skipped == ["TRACE /x: method not supported"]


def test_the_number_of_stubs_is_capped(
    model: ServiceModel, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(stubs_module, "MAX_STUBS", 1)
    stubs, skipped = build_stubs(model)
    assert len(stubs) == 1
    assert any("more than 1 operations" in line for line in skipped)


def test_a_body_over_the_cap_is_skipped(
    model: ServiceModel, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(stubs_module, "MAX_BODY_BYTES", 5)
    stubs, skipped = build_stubs(model)
    assert any("example larger than 5 bytes" in line for line in skipped)
    assert all(m["metadata"]["operation"] != "getCharge" for m in stubs.values())


def test_a_json_flavoured_media_type_counts(model: ServiceModel) -> None:
    other = model.model_copy(
        update={"operations": [_operation("GET", media="application/problem+json; charset=utf-8")]}
    )
    stubs, _ = build_stubs(other)
    assert next(iter(stubs.values()))["response"]["jsonBody"] == "string"
    assert next(iter(stubs)).startswith("weird-name-")


def test_the_lowest_success_code_is_used() -> None:
    operation = _operation("GET", "201").model_copy(
        update={
            "responses": [
                Response(status="204", description="", content={}),
                Response(status="201", description="", content={}),
                Response(status="default", description="", content={}),
            ]
        }
    )
    other = ServiceModel(
        title="t", version="1", openapi_version="3.0.3", servers=[], operations=[operation],
        docs=[], spec_hash="h",
    )  # fmt: skip
    stubs, _ = build_stubs(other)
    assert next(iter(stubs.values()))["response"] == {"status": 201}


def test_an_empty_operation_id_still_gets_a_name() -> None:
    operation = _operation("GET").model_copy(update={"operation_id": "!!!"})
    other = ServiceModel(
        title="t", version="1", openapi_version="3.0.3", servers=[], operations=[operation],
        docs=[], spec_hash="h",
    )  # fmt: skip
    assert next(iter(build_stubs(other)[0])).startswith("op-")


# --- writing ---


def test_write_replaces_its_own_stubs_and_keeps_the_rest(
    model: ServiceModel, tmp_path: Path
) -> None:
    folder = tmp_path / "mocks" / "payments"
    stubs, _ = build_stubs(model)
    write_stubs(folder, stubs)
    stale = folder / "stale-00000000.json"
    stale.write_text(json.dumps({"metadata": {"hermetic": True}}))
    mine = folder / "mine.json"
    mine.write_text(json.dumps({"request": {"url": "/x"}, "response": {"status": 200}}))
    note = folder / "notes.txt"
    note.write_text("hand written")
    write_stubs(folder, stubs)
    assert not stale.exists()
    assert mine.exists() and note.exists()
    assert {p.name for p in folder.glob("*.json")} == set(stubs) | {"mine.json"}
    written = json.loads((folder / next(iter(stubs))).read_text())
    assert written == stubs[next(iter(stubs))]
    assert not list(folder.glob(".stub-*"))


@pytest.mark.parametrize(
    "content",
    [
        "not json",
        "[1, 2]",
        json.dumps({"metadata": "x"}),
        json.dumps({"metadata": {"hermetic": 1}}),
    ],
)
def test_files_that_are_not_stubs_of_ours_are_kept(
    model: ServiceModel, tmp_path: Path, content: str
) -> None:
    folder = tmp_path / "m"
    folder.mkdir()
    other = folder / "other.json"
    other.write_text(content)
    write_stubs(folder, build_stubs(model)[0])
    assert other.read_text() == content


def test_an_oversized_file_is_not_read(
    model: ServiceModel, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    folder = tmp_path / "m"
    folder.mkdir()
    big = folder / "big.json"
    big.write_text(json.dumps({"metadata": {"hermetic": True}, "pad": "x" * 100}))
    monkeypatch.setattr(stubs_module, "MAX_BODY_BYTES", 10)
    write_stubs(folder, {})
    assert big.exists()


def test_a_linked_stub_file_is_left_alone(model: ServiceModel, tmp_path: Path) -> None:
    folder = tmp_path / "m"
    folder.mkdir()
    outside = tmp_path / "outside.json"
    outside.write_text(json.dumps({"metadata": {"hermetic": True}}))
    (folder / "link.json").symlink_to(outside)
    write_stubs(folder, {})
    assert outside.exists()
    assert (folder / "link.json").is_symlink()


def test_a_foreign_file_with_a_stub_name_is_not_overwritten(
    model: ServiceModel, tmp_path: Path
) -> None:
    folder = tmp_path / "m"
    folder.mkdir()
    stubs, _ = build_stubs(model)
    name = next(iter(stubs))
    (folder / name).write_text("{}")
    with pytest.raises(ProvisionError, match="not a stub Hermetic made"):
        write_stubs(folder, stubs)
    assert (folder / name).read_text() == "{}"


def test_a_symlinked_folder_is_refused(model: ServiceModel, tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    (tmp_path / "link").symlink_to(real)
    with pytest.raises(ProvisionError, match="symlink"):
        write_stubs(tmp_path / "link", build_stubs(model)[0])
    assert list(real.iterdir()) == []


def test_a_failed_write_leaves_no_temporary_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(src: str, dst: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr("hermetic.provision.stubs.os.replace", boom)
    with pytest.raises(OSError, match="disk full"):
        write_stubs(tmp_path / "m", {"a-00000000.json": {"metadata": {"hermetic": True}}})
    assert list((tmp_path / "m").iterdir()) == []


# --- the command ---


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "payments.yaml").write_text(SPEC)
    deps = DependencySet(
        service="shop",
        prompt_version="1",
        docs_hash="h",
        dependencies=[
            Dependency(name="payments", kind="http", origin="manual"),
            Dependency(name="db", kind="postgres", origin="manual"),
        ],
    )
    write_dependencies(tmp_path / "tests" / "shop", deps)
    return tmp_path


def test_stubs_command_writes_the_mock_folder(project: Path) -> None:
    result = cli_runner.invoke(
        app, ["stubs", "--service", "shop", "--name", "payments", "--spec", "payments.yaml"]
    )
    assert result.exit_code == 0, result.output
    assert "wrote 4 stubs" in result.output
    assert "skipped: GET /broken: no 2xx response" in result.output
    folder = project / ".hermetic" / "provision" / "shop" / "mocks" / "payments"
    assert len(list(folder.glob("*.json"))) == 4


def test_stubs_command_honours_output(project: Path) -> None:
    out = project / "elsewhere"
    result = cli_runner.invoke(
        app,
        ["stubs", "--service", "shop", "--name", "payments", "--spec", "payments.yaml",
         "--output", str(out)],
    )  # fmt: skip
    assert result.exit_code == 0
    assert len(list(out.glob("*.json"))) == 4


@pytest.mark.parametrize(
    ("args", "message"),
    [
        (["--service", "shop", "--name", "db"], "not an HTTP dependency"),
        (["--service", "shop", "--name", "nope"], "not an HTTP dependency"),
        (["--service", "other", "--name", "payments"], "no dependencies for 'other'"),
        (["--service", "Bad Name", "--name", "payments"], "error:"),
        (["--service", "shop", "--name", "payments", "--spec", "missing.yaml"], "error:"),
    ],
)
def test_stubs_command_refuses(project: Path, args: list[str], message: str) -> None:
    full = [*args] if "--spec" in args else [*args, "--spec", "payments.yaml"]
    result = cli_runner.invoke(app, ["stubs", *full])
    assert result.exit_code == 2
    assert message in result.output
