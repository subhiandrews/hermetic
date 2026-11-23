import json
from pathlib import Path

from typer.testing import CliRunner

from hermetic import __version__
from hermetic.cli import app

runner = CliRunner()


def test_version_prints_package_version() -> None:
    result = runner.invoke(app, ["version"])
    assert result.exit_code == 0
    assert result.stdout.strip() == __version__


PETSTORE = Path(__file__).parent.parent / "fixtures" / "petstore" / "openapi.json"


def test_ingest_prints_service_model_json(tmp_path: Path) -> None:
    (tmp_path / "design.md").write_text("# Design")
    result = runner.invoke(app, ["ingest", str(PETSTORE), "--docs", str(tmp_path)])
    assert result.exit_code == 0
    model = json.loads(result.stdout)
    assert model["title"].startswith("Swagger Petstore")
    assert [d["path"] for d in model["docs"]] == ["design.md"]


def test_ingest_reports_errors_and_exits_nonzero(tmp_path: Path) -> None:
    result = runner.invoke(app, ["ingest", str(tmp_path / "missing.yaml")])
    assert result.exit_code == 1
    assert "error: cannot read spec" in result.output


def test_no_args_shows_help() -> None:
    result = runner.invoke(app, [])
    assert "Usage" in result.output
    assert "version" in result.output
