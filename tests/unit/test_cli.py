import json
import socket
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


def fake_machine(monkeypatch: pytest.MonkeyPatch, *, fails_with: Exception | None = None) -> None:
    monkeypatch.setattr("socket.gethostname", lambda: "box.example")

    def resolve(name: str, port: None) -> list[tuple[Any, ...]]:
        if fails_with is not None:
            raise fails_with
        return [
            (socket.AF_INET, 0, 0, "", ("10.0.0.7", 0)),
            (socket.AF_INET6, 0, 0, "", ("fd00::7", 0, 0, 0)),
            (socket.AF_INET6, 0, 0, "", ("fe80::7%en0", 0, 0, 3)),
        ]

    monkeypatch.setattr("socket.getaddrinfo", resolve)


@pytest.mark.parametrize("every_interface", ["0.0.0.0", "::", "0:0:0:0:0:0:0:0", "[::]", ""])
def test_serve_on_every_interface_still_checks_the_host_header(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, every_interface: str
) -> None:
    # Opening the port to the network must not open it to DNS rebinding: the page the user
    # has open in a browser reaches the server as "evil.example", on the same port.
    calls = fake_uvicorn(monkeypatch)
    fake_machine(monkeypatch)
    result = runner.invoke(
        app, ["serve", "--history-dir", str(tmp_path), "--host", every_interface]
    )
    assert result.exit_code == 0 and "listening on" in result.output
    assert "rebinding" not in result.output
    served = calls[0][0]
    accepted_hosts = ("localhost:8000", "box.example:8000", "10.0.0.7:8000", "[fd00::7]:8000")
    for accepted in (*accepted_hosts, "[fe80::7]:8000"):
        assert status_with_host(served, accepted) == 200
    assert status_with_host(served, "evil.example") == 421
    assert status_with_host(served, "") == 421  # no Host header must not slip through


@pytest.mark.parametrize(
    "failure",
    [socket.gaierror("no such host"), UnicodeError("label empty or too long")],
    ids=["unresolvable", "unencodable"],
)
def test_serve_on_every_interface_keeps_this_machines_name_when_it_cannot_be_looked_up(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, failure: Exception
) -> None:
    calls = fake_uvicorn(monkeypatch)
    fake_machine(monkeypatch, fails_with=failure)
    result = runner.invoke(app, ["serve", "--history-dir", str(tmp_path), "--host", "0.0.0.0"])
    assert result.exit_code == 0 and len(calls) == 1  # no traceback at startup
    assert status_with_host(calls[0][0], "box.example") == 200
    assert status_with_host(calls[0][0], "10.0.0.7") == 421


def test_serve_answers_to_the_names_it_is_given(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls = fake_uvicorn(monkeypatch)
    fake_machine(monkeypatch)
    # A port in the value is ignored, as it is in the `Host` header it is compared with.
    names = ["--allowed-host", "hermetic.internal", "--allowed-host", "other.test:8080"]
    result = runner.invoke(
        app, ["serve", "--history-dir", str(tmp_path), "--host", "0.0.0.0", *names]
    )
    assert result.exit_code == 0 and "rebinding" not in result.output
    for accepted in ("hermetic.internal:8000", "other.test"):
        assert status_with_host(calls[0][0], accepted) == 200
    assert status_with_host(calls[0][0], "evil.example") == 421


@pytest.mark.parametrize("wildcard", ["*:8000", " * ", "*"])
def test_serve_warns_about_a_wildcard_however_it_is_written(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, wildcard: str
) -> None:
    calls = fake_uvicorn(monkeypatch)
    result = runner.invoke(
        app, ["serve", "--history-dir", str(tmp_path), "--allowed-host", wildcard]
    )
    assert "turns off the Host check" in result.output
    assert status_with_host(calls[0][0], "anything.example") == 200


@pytest.mark.parametrize("entry", ["", " ", ":8000"])
def test_serve_rejects_an_empty_allowed_host(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, entry: str
) -> None:
    calls = fake_uvicorn(monkeypatch)
    result = runner.invoke(app, ["serve", "--history-dir", str(tmp_path), "--allowed-host", entry])
    assert result.exit_code == 2 and "needs a host name" in result.output and calls == []


def test_serve_accepts_an_unbracketed_ipv6_allowed_host(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls = fake_uvicorn(monkeypatch)
    result = runner.invoke(
        app, ["serve", "--history-dir", str(tmp_path), "--allowed-host", "fd00::7"]
    )
    assert result.exit_code == 0
    assert status_with_host(calls[0][0], "[fd00::7]:8000") == 200


def test_serve_only_accepts_any_host_when_asked_to_and_says_what_that_risks(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls = fake_uvicorn(monkeypatch)
    result = runner.invoke(app, ["serve", "--history-dir", str(tmp_path), "--allowed-host", "*"])
    assert result.exit_code == 0
    assert "turns off the Host check" in result.output and "rebinding" in result.output
    assert status_with_host(calls[0][0], "anything.example") == 200


def test_serve_bound_to_a_host_name_answers_to_that_name(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls = fake_uvicorn(monkeypatch)
    quiet = runner.invoke(app, ["serve", "--history-dir", str(tmp_path), "--host", "localhost"])
    assert quiet.exit_code == 0 and "warning" not in quiet.output
    named = runner.invoke(app, ["serve", "--history-dir", str(tmp_path), "--host", "box.example"])
    assert named.exit_code == 0 and "listening on box.example" in named.output
    assert status_with_host(calls[1][0], "box.example:8000") == 200
    assert status_with_host(calls[1][0], "evil.example") == 421


def test_serve_on_a_bare_ipv6_address_accepts_its_bracketed_host_header(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls = fake_uvicorn(monkeypatch)
    result = runner.invoke(app, ["serve", "--history-dir", str(tmp_path), "--host", "fe80::1"])
    assert result.exit_code == 0 and len(calls) == 1
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
