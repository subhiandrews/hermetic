"""Starts real containers. Opt-in: `uv run pytest -m integration` (needs a running Docker)."""

from __future__ import annotations

import json
import socket
import subprocess
import urllib.error
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
from tests.integration.helpers import require_docker

pytestmark = pytest.mark.integration


def test_every_kind_comes_up_healthy_answers_and_goes_away(tmp_path: Path) -> None:
    require_docker()
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


def _admin(base: str, method: str, path: str, body: dict[str, str] | None = None) -> int:
    request = urllib.request.Request(
        base + path,
        method=method,
        data=None if body is None else json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return int(response.status)
    except urllib.error.HTTPError as error:
        return int(error.code)


def test_a_declared_error_is_answered_after_its_scenario_is_switched(tmp_path: Path) -> None:
    require_docker()
    deps = DependencySet(
        service="hermetic-scn",
        prompt_version="1",
        docs_hash="h",
        dependencies=[Dependency(name="payments", kind="http", origin="manual")],
    )
    spec = tmp_path / "payments.yaml"
    spec.write_text(
        "openapi: 3.0.3\ninfo: {title: p, version: '1'}\nservers: [{url: 'https://p.example/v1'}]\n"
        "paths:\n  /charges/{id}:\n    get:\n      operationId: getCharge\n"
        "      parameters: [{name: id, in: path, required: true, schema: {type: string}}]\n"
        "      responses:\n"
        "        '200': {description: ok}\n"
        "        '404':\n          description: missing\n          content:\n"
        "            application/json:\n"
        "              schema: {type: object, example: {error: gone}}\n"
    )
    stubs = build_stubs(ingest(spec))[0]
    scenario = next(m["metadata"]["scenario"] for m in stubs.values())
    write_stubs(tmp_path / MOCKS_DIR / "payments", stubs)
    path = tmp_path / COMPOSE_NAME
    text = generate_compose(deps, tmp_path)
    path.write_text(text)
    try:
        (endpoint,) = up("hermetic-scn", path, text)
        base = endpoint.url
        charge = base + "/v1/charges/ch_1"
        assert _admin(base, "GET", "/v1/charges/ch_1") == 200  # the scenario starts in `Started`
        state = f"/__admin/scenarios/{scenario}/state"
        assert _admin(base, "PUT", state, {"state": "404"}) == 200
        request = urllib.request.Request(charge)
        with pytest.raises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(request, timeout=10)
        assert caught.value.code == 404
        assert json.load(caught.value) == {"error": "gone"}
        assert _admin(base, "PUT", state, {"state": "nonsense"}) == 422  # only declared states
        assert _admin(base, "POST", "/__admin/scenarios/reset") == 200
        assert _admin(base, "GET", "/v1/charges/ch_1") == 200
    finally:
        down("hermetic-scn")
