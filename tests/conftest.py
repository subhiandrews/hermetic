"""Shared fixtures."""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest


class _Handler(BaseHTTPRequestHandler):
    def _reply(self, status: int, payload: object) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        if self.path == "/boom":
            self._reply(500, {"error": "boom", "token": "server-secret"})
        elif self.path == "/chunked":
            self.send_response(200)
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            self.wfile.write(b"5\r\nhello\r\n0\r\n\r\n")
        elif self.path == "/empty":
            self.send_response(204)
            self.send_header("Content-Length", "0")
            self.end_headers()
        else:
            self._reply(200, {"ok": True})

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        self._reply(201, {"received": json.loads(self.rfile.read(length) or b"null")})

    def log_message(self, format: str, *args: object) -> None:  # silence request logging
        pass


@pytest.fixture
def server() -> Iterator[str]:
    """A tiny local HTTP service; yields its base URL."""
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}"
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join()
