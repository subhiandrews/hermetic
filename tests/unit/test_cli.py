import json
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from hermetic import __version__
from hermetic.cli import app

runner = CliRunner()


def test_version_prints_package_version() -> None:
    result = runner.invoke(app, ["version"])
    assert result.exit_code == 0
    assert result.stdout.strip() == __version__


PETSTORE = Path(__file__).parent.parent / "fixtures" / "petstore" / "openapi.json"


def fake_uvicorn(monkeypatch: pytest.MonkeyPatch) -> list[tuple[Any, dict[str, Any]]]:
    """Replace the server with a recorder, so `serve` returns instead of listening."""
    calls: list[tuple[Any, dict[str, Any]]] = []
    monkeypatch.setattr("uvicorn.run", lambda served, **kwargs: calls.append((served, kwargs)))
    return calls


def status_with_host(served: Any, host: str) -> int:
    client = TestClient(served, base_url="http://localhost")
    return int(client.get("/api/runs", headers={"host": host}).status_code)


def test_serve_listens_on_loopback_only_by_default(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls = fake_uvicorn(monkeypatch)
    result = runner.invoke(app, ["serve", "--history-dir", str(tmp_path)])
    assert result.exit_code == 0 and "warning" not in result.output
    ((served, kwargs),) = calls
    assert kwargs == {"host": "127.0.0.1", "port": 8000}
    assert status_with_host(served, "localhost:8000") == 200
    assert status_with_host(served, "evil.example") == 421


def test_serve_warns_when_asked_to_listen_beyond_loopback(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls = fake_uvicorn(monkeypatch)
    result = runner.invoke(
        app, ["serve", "--history-dir", str(tmp_path), "--host", "192.168.1.5", "--port", "9000"]
    )
    assert result.exit_code == 0
    assert "warning: listening on 192.168.1.5" in result.output
    ((served, kwargs),) = calls
    assert kwargs == {"host": "192.168.1.5", "port": 9000}
    assert status_with_host(served, "192.168.1.5:9000") == 200
    assert status_with_host(served, "localhost") == 200
    assert status_with_host(served, "evil.example") == 421


def test_serve_on_every_interface_accepts_any_host_header(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls = fake_uvicorn(monkeypatch)
    result = runner.invoke(app, ["serve", "--history-dir", str(tmp_path), "--host", "0.0.0.0"])
    assert result.exit_code == 0 and "warning" in result.output
    assert status_with_host(calls[0][0], "hermetic.internal:8000") == 200


def test_serve_on_a_bare_ipv6_address_accepts_its_bracketed_host_header(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls = fake_uvicorn(monkeypatch)
    runner.invoke(app, ["serve", "--history-dir", str(tmp_path), "--host", "fe80::1"])
    assert status_with_host(calls[0][0], "[fe80::1]:8000") == 200


@pytest.mark.parametrize("port", ["0", "70000"])
def test_serve_rejects_a_port_that_cannot_exist(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, port: str
) -> None:
    calls = fake_uvicorn(monkeypatch)
    result = runner.invoke(app, ["serve", "--history-dir", str(tmp_path), "--port", port])
    assert result.exit_code == 2 and calls == []


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
