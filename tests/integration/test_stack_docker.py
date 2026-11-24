"""Starts real containers. Opt-in: `uv run pytest -m integration` (needs a running Docker)."""

from __future__ import annotations

import shutil
import socket
import subprocess
import urllib.request
from pathlib import Path

import pytest

from hermetic.discover import Dependency, DependencySet
from hermetic.provision import COMPOSE_NAME, MOCKS_DIR, down, generate_compose, up

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
    (tmp_path / MOCKS_DIR / "payments").mkdir(parents=True)
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
        url = endpoints["payments"].url + "/__admin/mappings"
        with urllib.request.urlopen(url, timeout=10) as response:
            assert response.status == 200
    finally:
        down("hermetic-it")
