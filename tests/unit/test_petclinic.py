"""The offline pipeline on the PetClinic example: one service with a database and nothing else.

The inputs are in `examples/petclinic` and the expected outputs next to them. Run with
HERMETIC_UPDATE_GOLDEN=1 to rewrite the expected files, then read the diff before committing.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest
from typer.testing import CliRunner

from hermetic.cli import app
from hermetic.discover.infer import InferRequest, InferResult
from hermetic.ingest import ingest
from hermetic.provision import COMPOSE_NAME, build_stubs, check_compose, read_compose_file

PETCLINIC = Path(__file__).parents[2] / "examples" / "petclinic"
UPDATE = os.environ.get("HERMETIC_UPDATE_GOLDEN") == "1"
cli_runner = CliRunner()


class NothingElse:
    """What the agent says about the PetClinic note: the hints found the database, so no more."""

    prompt_version = "golden"

    def infer(self, request: InferRequest) -> InferResult:
        return InferResult([])


def golden(name: str, actual: str) -> None:
    path = PETCLINIC / "expected" / name
    if UPDATE:
        path.parent.mkdir(exist_ok=True)
        path.write_text(actual, encoding="utf-8")
    assert actual == path.read_text(encoding="utf-8"), f"{name} differs from the expected file"


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    shutil.copytree(PETCLINIC / "docs", tmp_path / "docs")
    shutil.copy(PETCLINIC / "openapi.yml", tmp_path / "openapi.yml")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("hermetic.cli.make_inferrer", lambda: NothingElse())
    return tmp_path


def run(*args: str) -> str:
    result = cli_runner.invoke(app, list(args))
    assert result.exit_code == 0, result.output
    return result.output


def test_the_real_spec_is_read_and_every_operation_can_be_mocked() -> None:
    model = ingest(PETCLINIC / "openapi.yml", PETCLINIC / "docs")
    assert model.title == "Spring PetClinic"
    assert len(model.operations) == 37
    stubs, skipped = build_stubs(model)
    answers = [m for m in stubs.values() if m["metadata"].get("state") in (None, "Started")]
    assert len(answers) == 37 and len(stubs) == 131  # 37 operations and 94 declared errors
    assert skipped == ["response 304 not mocked (31 operations)"]  # the only code left out
    assert {m["request"]["method"] for m in stubs.values()} == {"GET", "POST", "PUT", "DELETE"}


def test_discovery_finds_only_the_database(project: Path) -> None:
    output = run(
        "discover", "openapi.yml", "--docs", "docs", "--suite", "suite", "--service", "petclinic"
    )
    assert "added: postgres" in output
    assert "redis" not in output
    written = project / "suite" / ".hermetic" / "dependencies.json"
    golden("dependencies.json", written.read_text(encoding="utf-8"))


def test_the_compose_file_holds_the_database_alone(project: Path) -> None:
    run("discover", "openapi.yml", "--docs", "docs", "--suite", "suite", "--service", "petclinic")
    run("compose", "--service", "petclinic", "--suite", "suite")
    path = project / ".hermetic" / "provision" / "petclinic" / COMPOSE_NAME
    golden(COMPOSE_NAME, path.read_text(encoding="utf-8"))
    document = check_compose(read_compose_file(path), path.parent)
    assert list(document["services"]) == ["postgres"]
    assert "ok:" in run("check-compose", str(path))
