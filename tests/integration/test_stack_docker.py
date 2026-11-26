"""Starts real containers. Opt-in: `uv run pytest -m integration` (needs a running Docker)."""

from __future__ import annotations

import json
import socket
import subprocess
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from hermetic.discover import Dependency, DependencySet
from hermetic.execute import execute_suite
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


def test_a_suite_switches_a_real_mock_and_it_is_reset_for_the_next_test(tmp_path: Path) -> None:
    require_docker()
    deps = DependencySet(
        service="hermetic-sw",
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
        "        '404': {description: missing}\n"
    )
    write_stubs(tmp_path / MOCKS_DIR / "payments", build_stubs(ingest(spec))[0])
    compose = tmp_path / COMPOSE_NAME
    text = generate_compose(deps, tmp_path)
    compose.write_text(text)
    suite = tmp_path / "suite"
    suite.mkdir()
    (suite / "test_flow.py").write_text(
        "def test_a_downstream_error(hermetic_client, hermetic_mocks):\n"
        "    assert hermetic_client.get('/v1/charges/c1').status_code == 200\n"
        "    hermetic_mocks['payments'].answer('getCharge', 404)\n"
        "    assert hermetic_client.get('/v1/charges/c1').status_code == 404\n"
        "\n"
        "def test_the_next_test_sees_the_normal_answer(hermetic_client):\n"
        "    assert hermetic_client.get('/v1/charges/c1').status_code == 200\n"
    )
    try:
        (endpoint,) = up("hermetic-sw", compose, text)

        class Relay(BaseHTTPRequestHandler):
            """Stands in for the service under test: it calls the mock and passes the status on."""

            def do_GET(self) -> None:
                try:
                    with urllib.request.urlopen(endpoint.url + self.path, timeout=10) as upstream:
                        status = upstream.status
                except urllib.error.HTTPError as error:
                    status = error.code
                self.send_response(status)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, format: str, *args: object) -> None:
                pass

        relay = ThreadingHTTPServer(("127.0.0.1", 0), Relay)
        thread = threading.Thread(target=relay.serve_forever, daemon=True)
        thread.start()
        try:
            target = f"http://127.0.0.1:{relay.server_address[1]}"
            results = execute_suite(suite, target, mocks={"payments": endpoint.url})
        finally:
            relay.shutdown()
            relay.server_close()
            thread.join()
        assert [(r.test_id.rpartition("::")[2], r.status) for r in results] == [
            ("test_a_downstream_error", "passed"),
            ("test_the_next_test_sees_the_normal_answer", "passed"),
        ]
    finally:
        down("hermetic-sw")
