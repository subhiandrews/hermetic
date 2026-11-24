"""The web UI and API: the run history as JSON under /api, a static page over it, and, when a
token is configured, an endpoint that accepts runs from `hermetic run`."""

from __future__ import annotations

import logging
import secrets
import threading
import traceback
from collections.abc import Awaitable, Callable, Sequence
from pathlib import Path
from typing import Annotated

from fastapi import FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import ValidationError
from starlette.concurrency import run_in_threadpool

from hermetic.history import (
    RunNotFoundError,
    RunRecord,
    RunStore,
    RunStoreError,
    RunSummary,
    ServiceSummary,
    is_valid_run_id,
    summarize,
)

STATIC_DIR = Path(__file__).parent / "static"
LOOPBACK_HOSTS = ("localhost", "127.0.0.1", "[::1]")
WILDCARD = "*"
DEFAULT_RUN_LIMIT = 200
MAX_RUN_LIMIT = 10_000
MAX_INGEST_BYTES = 10 * 1024 * 1024  # a run with every exchange recorded is far below this

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


def _where(exc: BaseException) -> str:
    """Where an exception was raised, without its message, which can quote stored data."""
    innermost = traceback.extract_tb(exc.__traceback__)[-1:]
    return ", ".join(f"{Path(f.filename).name}:{f.lineno} in {f.name}" for f in innermost) or "?"


def hostname(host_header: str) -> str:
    """The host in a `Host` header without its port; an IPv6 address keeps its brackets."""
    host = host_header.strip().lower()
    if host.startswith("["):
        return host[: host.find("]") + 1]
    if host.count(":") > 1:  # a bare IPv6 address, not host:port
        return f"[{host}]"
    return host.partition(":")[0]


async def _read_limited(request: Request) -> bytes:
    """The request body, refused as soon as it is known to exceed the ingest limit."""
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > MAX_INGEST_BYTES:
        raise HTTPException(413, "the run is too large")
    chunks: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > MAX_INGEST_BYTES:
            raise HTTPException(413, "the run is too large")
        chunks.append(chunk)
    return b"".join(chunks)


def create_app(
    store: RunStore,
    allowed_hosts: Sequence[str] = LOOPBACK_HOSTS,
    ingest_token: str | None = None,
) -> FastAPI:
    """The app over `store`. `allowed_hosts` are the host names it answers to; "*" allows any.

    Runs are accepted only with `ingest_token` as a bearer token; without one, ingest is off.
    """
    # Entries are compared like a `Host` header, by name: "example.test:8080" is "example.test".
    allowed = frozenset(hostname(host) for host in allowed_hosts)
    if "" in allowed:  # it would also match a request that sends no Host header
        raise ValueError("an allowed host cannot be empty")
    # The interactive docs load script from a CDN, which the policy above does not allow.
    app = FastAPI(title="Hermetic", docs_url=None, redoc_url=None, openapi_url="/api/openapi.json")

    @app.middleware("http")
    async def guard(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        if WILDCARD not in allowed and hostname(request.headers.get("host", "")) not in allowed:
            response: Response = PlainTextResponse("unexpected Host header", status_code=421)
        else:
            try:
                response = await call_next(request)
            except Exception as exc:
                # Starlette would answer outside this middleware, without the headers, and the
                # server would log the traceback with the exception's message, which can quote
                # stored data. Answer here and log only the kind of failure, as for the store.
                _logger.error(
                    "unexpected error serving the request (%s at %s)",
                    type(exc).__name__,
                    _where(exc),
                )
                response = JSONResponse({"detail": "internal server error"}, status_code=500)
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

    save_lock = threading.Lock()

    def save_run(record: RunRecord) -> None:
        # A file store reads its index, then writes it: two saves at once would drop an entry.
        with save_lock:
            store.save(record)

    def authorize(request: Request) -> None:
        if not ingest_token:
            raise HTTPException(403, "ingest is off: start the server with HERMETIC_API_TOKEN set")
        scheme, _, credential = request.headers.get("authorization", "").partition(" ")
        if scheme.lower() != "bearer" or not secrets.compare_digest(
            credential.encode(), ingest_token.encode()
        ):
            raise HTTPException(
                401, "missing or wrong token", headers={"WWW-Authenticate": "Bearer"}
            )

    @app.get("/api/runs")
    def list_runs(
        limit: Annotated[int, Query(ge=1, le=MAX_RUN_LIMIT)] = DEFAULT_RUN_LIMIT,
    ) -> list[RunSummary]:
        return store.list()[:limit]  # newest first; history only grows, so the payload is capped

    @app.get("/api/services")
    def list_services() -> list[ServiceSummary]:
        return store.services()

    @app.get("/api/services/{service}/runs")
    def list_service_runs(
        service: str, limit: Annotated[int, Query(ge=1, le=MAX_RUN_LIMIT)] = DEFAULT_RUN_LIMIT
    ) -> list[RunSummary]:
        return store.list(service)[:limit]

    @app.post("/api/services/{service}/runs", status_code=201)
    async def ingest_run(service: str, request: Request) -> RunSummary:
        authorize(request)  # before the body is read, so an unauthenticated client costs nothing
        body = await _read_limited(request)
        try:
            record = RunRecord.model_validate_json(body)
        except ValidationError as exc:
            # Only where and what, never the input: it is untrusted and may hold secrets.
            problems = [
                {"loc": e["loc"], "msg": e["msg"]}
                for e in exc.errors(include_input=False, include_url=False, include_context=False)
            ]
            raise HTTPException(422, problems) from exc
        if record.service != service:
            raise HTTPException(422, "the run's service does not match the address")
        if not is_valid_run_id(record.run_id):
            raise HTTPException(422, "the run id is not valid")
        try:
            await run_in_threadpool(save_run, record)  # file I/O must not block the event loop
        except RunStoreError as exc:  # the message can quote stored data: log only the kind
            _logger.warning("run could not be saved (%s)", type(exc).__name__)
            raise HTTPException(500, "the run could not be saved") from exc
        return summarize(record)

    @app.get("/api/services/{service}/runs/{run_id}")
    def get_run(service: str, run_id: str) -> RunRecord:
        # A run id is only unique within its service, so the service is part of the address.
        return store.get(service, run_id)

    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    return app
