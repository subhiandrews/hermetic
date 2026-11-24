"""Starts real containers. Opt-in: `uv run pytest -m integration` (needs a running Docker)."""

from __future__ import annotations

import json
import shutil
import socket
import subprocess
import urllib.request
from pathlib import Path

import pytest

from hermetic.discover import Dependency, DependencySet
from hermetic.ingest import ingest
from hermetic.provision import (
    COMPOSE_NAME,
    MOCKS_DIR,
    build_stubs,
    down,
    generate_compose,
    up,
    write_stubs,
)

pytestmark = pytest.mark.integration


def _docker_ready() -> bool:
    if shutil.which("docker") is None:
        return False
    done = subprocess.run(["docker", "info"], capture_output=True, check=False, timeout=30)
    return done.returncode == 0


def test_every_kind_comes_up_healthy_answers_and_goes_away(tmp_path: Path) -> None:
    if not _docker_ready():
        pytest.skip("Docker is not running")
    deps = DependencySet(
        service="hermetic-it",
        prompt_version="1",
        docs_hash="h",
        dependencies=[
            Dependency(name="cache", kind="redis", origin="manual"),
            Dependency(name="pg", kind="postgres", origin="manual"),
            Dependency(name="my", kind="mysql", origin="manual"),
            Dependency(name="mongo", kind="mongo", origin="manual"),
            Dependency(name="payments", kind="http", origin="manual"),
        ],
    )
    spec = tmp_path / "payments.yaml"
    spec.write_text(
        "openapi: 3.0.3\ninfo: {title: p, version: '1'}\nservers: [{url: 'https://p.example/v1'}]\n"
        "paths:\n  /charges/{id}:\n    get:\n      operationId: getCharge\n"
        "      parameters: [{name: id, in: path, required: true, schema: {type: string}}]\n"
        "      responses:\n"
        "        '200':\n          description: ok\n          content:\n"
        "            application/json:\n              schema:\n                type: object\n"
        "                properties: {status: {type: string, enum: [succeeded]}}\n"
    )
    write_stubs(tmp_path / MOCKS_DIR / "payments", build_stubs(ingest(spec))[0])
    path = tmp_path / COMPOSE_NAME
    text = generate_compose(deps, tmp_path)
    path.write_text(text)
    try:
        endpoints = {e.name: e for e in up("hermetic-it", path, text)}
        with socket.create_connection(("127.0.0.1", endpoints["cache"].port), timeout=5) as conn:
            conn.sendall(b"PING\r\n")
            assert conn.recv(16).startswith(b"+PONG")
        for name in ("pg", "my", "mongo"):  # healthy by Compose's wait; also reachable
            socket.create_connection(("127.0.0.1", endpoints[name].port), timeout=5).close()
        url = endpoints["payments"].url + "/v1/charges/ch_1"
        with urllib.request.urlopen(url, timeout=10) as response:
            assert response.status == 200
            assert json.load(response) == {"status": "succeeded"}
    finally:
        down("hermetic-it")
    for kind in ("ps -aq", "network ls -q"):  # the stub file must still remove everything
        listed = subprocess.run(
            [
                "docker",
                *kind.split(),
                "--filter",
                "label=com.docker.compose.project=hermetic-hermetic-it",
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        assert listed.stdout.strip() == "", kind
