"""The read-only web UI: the run history as JSON under /api, and a static page over it."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Sequence
from pathlib import Path
from typing import Annotated

from fastapi import FastAPI, Query, Request, Response
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles

from hermetic.history import RunNotFoundError, RunRecord, RunStore, RunStoreError, RunSummary

STATIC_DIR = Path(__file__).parent / "static"
LOOPBACK_HOSTS = ("localhost", "127.0.0.1", "[::1]")
WILDCARD = "*"
DEFAULT_RUN_LIMIT = 200
MAX_RUN_LIMIT = 10_000

_logger = logging.getLogger("hermetic.web")
_SECURITY_HEADERS = {
    # No inline script or style and nothing from another origin; the page cannot be framed.
    "Content-Security-Policy": (
        "default-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
    ),
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "X-Frame-Options": "DENY",
}


def _harden(response: Response, path: str) -> None:
    response.headers.update(_SECURITY_HEADERS)
    if path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store"


def hostname(host_header: str) -> str:
    """The host in a `Host` header without its port; an IPv6 address keeps its brackets."""
    host = host_header.strip().lower()
    if host.startswith("["):
        return host[: host.find("]") + 1]
    return host.partition(":")[0]


def create_app(store: RunStore, allowed_hosts: Sequence[str] = LOOPBACK_HOSTS) -> FastAPI:
    """The app over `store`. `allowed_hosts` are the host names it answers to; "*" allows any."""
    # Entries are compared like a `Host` header, by name: "example.test:8080" is "example.test".
    allowed = frozenset(hostname(host) for host in allowed_hosts)
    # The interactive docs load script from a CDN, which the policy above does not allow.
    app = FastAPI(title="Hermetic", docs_url=None, redoc_url=None, openapi_url="/api/openapi.json")

    @app.middleware("http")
    async def guard(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        if WILDCARD not in allowed and hostname(request.headers.get("host", "")) not in allowed:
            response: Response = PlainTextResponse("unexpected Host header", status_code=421)
        else:
            response = await call_next(request)
        _harden(response, request.url.path)
        return response

    @app.exception_handler(RunNotFoundError)
    async def not_found(request: Request, exc: RunNotFoundError) -> JSONResponse:
        return JSONResponse({"detail": "run not found"}, status_code=404)

    @app.exception_handler(RunStoreError)
    async def unreadable(request: Request, exc: RunStoreError) -> JSONResponse:
        # The message can quote the stored record (a corrupt file's validation error does), so
        # neither the client nor the log gets it: only what kind of failure it was.
        _logger.warning("run history could not be read (%s)", type(exc).__name__)
        return JSONResponse({"detail": "the run history could not be read"}, status_code=500)

    @app.exception_handler(Exception)
    async def crashed(request: Request, exc: Exception) -> JSONResponse:
        # Starlette answers an unhandled error outside the middleware above, so the headers are
        # added here too; the traceback is still logged by the server.
        response = JSONResponse({"detail": "internal server error"}, status_code=500)
        _harden(response, request.url.path)
        return response

    @app.get("/api/runs")
    def list_runs(
        limit: Annotated[int, Query(ge=1, le=MAX_RUN_LIMIT)] = DEFAULT_RUN_LIMIT,
    ) -> list[RunSummary]:
        return store.list()[:limit]  # newest first; history only grows, so the payload is capped

    @app.get("/api/runs/{run_id}")
    def get_run(run_id: str) -> RunRecord:
        # Until the API is service-scoped, look in each service for the run. Run ids carry a
        # timestamp and a random tail, so they do not repeat across services in practice.
        for entry in store.services():
            try:
                return store.get(entry.service, run_id)
            except RunNotFoundError:
                continue
        raise RunNotFoundError(f"no run {run_id!r}")

    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    return app
