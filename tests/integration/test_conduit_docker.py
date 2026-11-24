"""Conduit end to end through the commands: discover, write the stack, start it, use it, stop it.

Opt-in: `uv run pytest -m integration` (needs a running Docker).
"""

from __future__ import annotations

import json
import re
import shutil
import socket
import urllib.request
from pathlib import Path

import pytest
from typer.testing import CliRunner

from hermetic.cli import app
from tests.integration.test_stack_docker import require_docker
from tests.unit.test_conduit import FIXTURE, MailInferrer

pytestmark = pytest.mark.integration
cli_runner = CliRunner()


def _run(*args: str) -> str:
    result = cli_runner.invoke(app, list(args))
    assert result.exit_code == 0, result.output
    return result.output


def test_conduit_stack_comes_up_from_its_documents(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    require_docker()
    for name in ("docs", "openapi.yml", "mail-openapi.yml"):
        source = FIXTURE / name
        if source.is_dir():
            shutil.copytree(source, tmp_path / name)
        else:
            shutil.copy(source, tmp_path / name)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("hermetic.cli.make_inferrer", lambda: MailInferrer())
    _run("discover", "openapi.yml", "--docs", "docs", "--suite", "suite", "--service", "conduit")
    _run("compose", "--service", "conduit", "--suite", "suite")
    _run(
        "stubs", "--service", "conduit", "--name", "mail", "--spec", "mail-openapi.yml",
        "--suite", "suite",
    )  # fmt: skip
    try:
        lines = _run("up", "--service", "conduit").splitlines()
        env = dict(line.split("=", 1) for line in lines)
        assert sorted(env) == [
            "HERMETIC_DEP_MAIL_URL",
            "HERMETIC_DEP_POSTGRES_URL",
            "HERMETIC_DEP_REDIS_URL",
        ]
        for name in ("HERMETIC_DEP_POSTGRES_URL", "HERMETIC_DEP_REDIS_URL"):
            port = int(re.search(r":(\d+)(?:/|$)", env[name]).group(1))  # type: ignore[union-attr]
            socket.create_connection(("127.0.0.1", port), timeout=5).close()
        url = env["HERMETIC_DEP_MAIL_URL"] + "/v2/messages/0f8fad5b"
        with urllib.request.urlopen(url, timeout=10) as response:
            assert response.status == 200
            body = json.load(response)
        assert body["status"] == "sent"
    finally:
        # not `_run`: a failing `down` must not hide the failure that brought us here
        cli_runner.invoke(app, ["down", "--service", "conduit"])
