"""The whole offline pipeline on a real-world service: Conduit and the Mail service it calls.

The inputs are in `tests/fixtures/conduit` and the expected outputs next to them. Run with
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
from hermetic.ingest import ingest
from hermetic.provision import COMPOSE_NAME, build_stubs, check_compose, read_compose_file
from tests.fakes import CONDUIT, MailInferrer

UPDATE = os.environ.get("HERMETIC_UPDATE_GOLDEN") == "1"
cli_runner = CliRunner()


def golden(name: str, actual: str) -> None:
    path = CONDUIT / "expected" / name
    if UPDATE:
        path.write_text(actual, encoding="utf-8")
    assert actual == path.read_text(encoding="utf-8"), f"{name} differs from the expected file"


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    shutil.copytree(CONDUIT / "docs", tmp_path / "docs")
    shutil.copy(CONDUIT / "openapi.yml", tmp_path / "openapi.yml")
    shutil.copy(CONDUIT / "mail-openapi.yml", tmp_path / "mail-openapi.yml")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("hermetic.cli.make_inferrer", lambda: MailInferrer())
    return tmp_path


def run(*args: str) -> str:
    result = cli_runner.invoke(app, list(args))
    assert result.exit_code == 0, result.output
    return result.output


def test_the_real_spec_is_read_and_every_operation_can_be_mocked() -> None:
    model = ingest(CONDUIT / "openapi.yml", CONDUIT / "docs")
    assert model.title == "RealWorld Conduit API"
    assert len(model.operations) == 19
    stubs, skipped = build_stubs(model)
    assert len(stubs) == 19 and skipped == []
    assert {m["request"]["method"] for m in stubs.values()} == {"GET", "POST", "PUT", "DELETE"}


def test_discovery_finds_the_database_the_cache_and_the_mail_service(project: Path) -> None:
    output = run(
        "discover", "openapi.yml", "--docs", "docs", "--suite", "suite", "--service", "conduit"
    )
    assert "added: postgres" in output and "added: redis" in output and "added: mail" in output
    golden("dependencies.json", (project / "suite" / ".hermetic" / "dependencies.json").read_text())


def test_the_compose_file_is_what_the_dependencies_call_for(project: Path) -> None:
    run("discover", "openapi.yml", "--docs", "docs", "--suite", "suite", "--service", "conduit")
    run("compose", "--service", "conduit", "--suite", "suite")
    path = project / ".hermetic" / "provision" / "conduit" / COMPOSE_NAME
    golden(COMPOSE_NAME, path.read_text())
    document = check_compose(read_compose_file(path), path.parent)
    assert list(document["services"]) == ["postgres", "redis", "mail"]
    assert "ok:" in run("check-compose", str(path))


def test_the_mail_stubs_answer_from_its_spec(project: Path) -> None:
    run("discover", "openapi.yml", "--docs", "docs", "--suite", "suite", "--service", "conduit")
    run(
        "stubs", "--service", "conduit", "--name", "mail", "--spec", "mail-openapi.yml",
        "--suite", "suite",
    )  # fmt: skip
    folder = project / ".hermetic" / "provision" / "conduit" / "mocks" / "mail"
    written = {p.name: json.loads(p.read_text()) for p in sorted(folder.glob("*.json"))}
    golden("mail-stubs.json", json.dumps(written, indent=2, sort_keys=True) + "\n")
    patterns = sorted(m["request"]["urlPathPattern"] for m in written.values())
    assert patterns == ["/v2/health", "/v2/messages", "/v2/messages/[^/]+"]


def test_running_the_pipeline_again_changes_nothing(project: Path) -> None:
    def everything() -> dict[str, str]:
        return {
            str(p.relative_to(project)): p.read_text()
            for p in sorted(project.rglob("*"))
            if p.is_file() and ".hermetic" in p.parts
        }

    steps = [
        ["discover", "openapi.yml", "--docs", "docs", "--suite", "suite", "--service", "conduit"],
        ["compose", "--service", "conduit", "--suite", "suite"],
        ["stubs", "--service", "conduit", "--name", "mail", "--spec", "mail-openapi.yml",
         "--suite", "suite"],
    ]  # fmt: skip
    for step in steps:
        run(*step)
    first = everything()
    for step in steps:
        run(*step)
    assert everything() == first
