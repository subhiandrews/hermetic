"""A stand-in for WireMock's admin API: a stub listing, and a record of the calls it was sent."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any


class FakeAdmin:
    """Serves `GET /__admin/mappings` and answers the scenario calls, noting each one."""

    def __init__(self) -> None:
        self.listing: Any = {"mappings": [], "meta": {"total": 0}}
        self.listing_raw: bytes | None = None  # sent instead of `listing` when set
        self.listing_headers: dict[str, str] = {}  # extra headers on that reply
        self.statuses = {"GET": 200, "PUT": 200, "POST": 200}
        self.fail_on: dict[str, set[int]] = {}  # method -> numbers (from 1) of the calls to fail
        self.calls: list[tuple[str, str, Any]] = []  # method, path, JSON body
        admin = self

        class Handler(BaseHTTPRequestHandler):
            def _answer(self, method: str) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b""
                admin.calls.append((method, self.path, json.loads(raw) if raw else None))
                if method == "GET" and admin.listing_raw is not None:
                    body = admin.listing_raw
                else:
                    body = json.dumps(admin.listing if method == "GET" else {}).encode()
                count = sum(1 for verb, _, _ in admin.calls if verb == method)
                failing = count in admin.fail_on.get(method, set())
                self.send_response(500 if failing else admin.statuses[method])
                self.send_header("Content-Type", "application/json")
                if method == "GET":
                    for name, value in admin.listing_headers.items():
                        self.send_header(name, value)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self) -> None:
                self._answer("GET")

            def do_PUT(self) -> None:
                self._answer("PUT")

            def do_POST(self) -> None:
                self._answer("POST")

            def log_message(self, format: str, *args: object) -> None:
                pass  # keep test output quiet

        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self._httpd.server_address[1]}"

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()
        self._thread.join()

    def paths(self, method: str) -> list[str]:
        return [path for verb, path, _ in self.calls if verb == method]
