"""The offline pipeline on the Sock Shop example: a service with a database and three HTTP services.

The orders service and the three services it calls are Swagger 2.0 files, read by converting them.
The inputs are in `examples/sockshop` and the expected outputs next to them. Run with
HERMETIC_UPDATE_GOLDEN=1 to rewrite the expected files, then read the diff before committing.
"""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

import pytest
from typer.testing import CliRunner

from hermetic.cli import app
from hermetic.discover.infer import InferRequest, InferResult, Proposal, Quote
from hermetic.ingest import ingest
from hermetic.provision import COMPOSE_NAME, build_stubs, check_compose, read_compose_file

SOCKSHOP = Path(__file__).parents[2] / "examples" / "sockshop"
DOWNSTREAM = ["carts", "payment", "user"]
UPDATE = os.environ.get("HERMETIC_UPDATE_GOLDEN") == "1"
cli_runner = CliRunner()


class Downstream:
    """What the agent says about the orders note: the hints find the database, it finds the rest."""

    prompt_version = "golden"

    def infer(self, request: InferRequest) -> InferResult:
        return InferResult(
            [
                Proposal(
                    name=name,
                    kind="http",
                    quotes=[Quote(source="architecture.md", quote=f"asks the {name} service")],
                )
                for name in ("carts", "user", "payment")
            ]
        )


def golden(name: str, actual: str) -> None:
    path = SOCKSHOP / "expected" / name
    if UPDATE:
        path.parent.mkdir(exist_ok=True)
        path.write_text(actual, encoding="utf-8")
    assert actual == path.read_text(encoding="utf-8"), f"{name} differs from the expected file"


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    shutil.copytree(SOCKSHOP / "docs", tmp_path / "docs")
    shutil.copytree(SOCKSHOP / "specs", tmp_path / "specs")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("hermetic.cli.make_inferrer", lambda: Downstream())
    return tmp_path


def run(*args: str) -> str:
    result = cli_runner.invoke(app, list(args))
    assert result.exit_code == 0, result.output
    return result.output


def discover() -> str:
    return run(
        "discover", "specs/orders.json", "--docs", "docs", "--suite", "suite", "--service", "orders"
    )


def test_every_spec_is_converted_and_every_operation_can_be_mocked() -> None:
    counts = {}
    for name in ["orders", *DOWNSTREAM]:
        model = ingest(SOCKSHOP / "specs" / f"{name}.json", SOCKSHOP / "docs")
        assert model.openapi_version.startswith("3.0")  # converted from Swagger 2.0
        stubs, skipped = build_stubs(model)
        assert skipped == [] and len(stubs) == len(model.operations)
        counts[name] = len(model.operations)
    assert counts == {"orders": 2, "carts": 5, "payment": 2, "user": 15}


def test_discovery_finds_the_database_and_the_three_services(project: Path) -> None:
    output = discover()
    for name in ["mongo", *DOWNSTREAM]:
        assert f"added: {name}" in output
    written = project / "suite" / ".hermetic" / "dependencies.json"
    golden("dependencies.json", written.read_text(encoding="utf-8"))


def test_the_compose_file_serves_the_database_and_a_mock_per_service(project: Path) -> None:
    discover()
    run("compose", "--service", "orders", "--suite", "suite")
    path = project / ".hermetic" / "provision" / "orders" / COMPOSE_NAME
    golden(COMPOSE_NAME, path.read_text(encoding="utf-8"))
    document = check_compose(read_compose_file(path), path.parent)
    assert list(document["services"]) == ["mongo", "carts", "payment", "user"]
    assert "ok:" in run("check-compose", str(path))


@pytest.mark.parametrize("name", DOWNSTREAM)
def test_each_mock_answers_from_its_converted_spec(project: Path, name: str) -> None:
    discover()
    run("stubs", "--service", "orders", "--name", name, "--spec", f"specs/{name}.json",
        "--suite", "suite")  # fmt: skip
    folder = project / ".hermetic" / "provision" / "orders" / "mocks" / name
    files = sorted(folder.glob("*.json"))
    written = {p.name: json.loads(p.read_text(encoding="utf-8")) for p in files}
    golden(f"{name}-stubs.json", json.dumps(written, indent=2, sort_keys=True) + "\n")
    assert written


def test_running_the_pipeline_again_changes_nothing(project: Path) -> None:
    def everything() -> dict[str, str]:
        return {
            str(p.relative_to(project)): p.read_text(encoding="utf-8")
            for p in sorted(project.rglob("*"))
            if p.is_file() and ".hermetic" in p.parts
        }

    steps = [
        ["discover", "specs/orders.json", "--docs", "docs", "--suite", "suite",
         "--service", "orders"],
        ["compose", "--service", "orders", "--suite", "suite"],
        *[["stubs", "--service", "orders", "--name", n, "--spec", f"specs/{n}.json",
           "--suite", "suite"] for n in DOWNSTREAM],
    ]  # fmt: skip
    for step in steps:
        run(*step)
    first = everything()
    for step in steps:
        run(*step)
    assert everything() == first
