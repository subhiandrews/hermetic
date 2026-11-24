"""Command-line entry point."""

from __future__ import annotations

import ipaddress
import os
import socket
from collections.abc import Sequence
from enum import StrEnum
from pathlib import Path
from typing import Annotated

import typer

from hermetic import __version__
from hermetic.agent import ClaudeAuthor
from hermetic.execute import ExecuteError, check_target
from hermetic.generate import GenerateError, ManifestError, SuiteAuthor, generate_suite
from hermetic.history import (
    API_TOKEN_ENV,
    RunRecord,
    RunStoreError,
    SuiteChange,
    open_store,
)
from hermetic.ingest import IngestError, ServiceModel
from hermetic.ingest import ingest as run_ingest
from hermetic.net import LOOPBACK_NAMES, is_header_token
from hermetic.publish import (
    ExportError,
    PublisherError,
    load_publisher,
    require_tests,
    write_suite_zip,
)
from hermetic.report import SinkContext, SinkError, load_sinks
from hermetic.runner import run_suite
from hermetic.service import InvalidServiceError, resolve_service

app = typer.Typer(
    name="hermetic",
    help="Agentic tester for REST microservices.",
    no_args_is_help=True,
    add_completion=False,
)


@app.callback()
def main() -> None:
    """Hermetic command group."""


@app.command()
def version() -> None:
    """Print the Hermetic version."""
    typer.echo(__version__)


@app.command()
def ingest(
    spec: Annotated[Path, typer.Argument(help="OpenAPI 3.x file (JSON or YAML).")],
    docs: Annotated[
        Path | None, typer.Option(help="Directory of design/architecture documents.")
    ] = None,
) -> None:
    """Parse the spec and docs and print the resulting service model as JSON."""
    try:
        model = run_ingest(spec, docs)
    except IngestError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    typer.echo(model.model_dump_json(indent=2))


def make_author() -> SuiteAuthor:
    """The author `generate` and `run --generate` use. Tests replace this with a fake."""
    return ClaudeAuthor()  # pragma: no cover  (needs the real API)


@app.command()
def generate(
    spec: Annotated[Path, typer.Argument(help="OpenAPI 3.x file (JSON or YAML).")],
    suite: Annotated[
        Path | None,
        typer.Option(help="Directory the test suite is written to; default tests/<service>."),
    ] = None,
    service: Annotated[
        str | None,
        typer.Option(help="The microservice these tests are for; default: the spec's title."),
    ] = None,
    docs: Annotated[
        Path | None, typer.Option(help="Directory of design/architecture documents.")
    ] = None,
    force: Annotated[bool, typer.Option(help="Rewrite every test, not only changed ones.")] = False,
) -> None:
    """Write or update the test suite for the spec; only changed operations are rewritten."""
    try:
        model = run_ingest(spec, docs)
        service_id = resolve_service(model.title, service)
        suite_dir = suite or _default_suite(service_id)
        changes = generate_suite(model, make_author(), suite_dir, force=force)
    except (IngestError, InvalidServiceError, GenerateError, ManifestError, OSError) as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=2) from exc
    _echo_changes(changes)


def _default_suite(service: str) -> Path:
    """Generated tests are kept per microservice."""
    return Path("tests") / service


def _echo_changes(changes: list[SuiteChange]) -> None:
    if not changes:
        typer.echo("suite is up to date")
    for change in changes:
        typer.echo(f"{change.kind}: {change.test_id} ({change.reason})")


class ExportFormat(StrEnum):
    ZIP = "zip"


@app.command()
def export(
    service: Annotated[str, typer.Option(help="The microservice whose tests to export.")],
    suite: Annotated[
        Path | None, typer.Option(help="The suite directory; default tests/<service>.")
    ] = None,
    archive_format: Annotated[
        ExportFormat, typer.Option("--format", help="Archive format (zip is the only one).")
    ] = ExportFormat.ZIP,
    output: Annotated[
        Path | None,
        typer.Option(help="Where to write the archive; default hermetic-tests-<service>.zip."),
    ] = None,
) -> None:
    """Pack a service's generated tests and manifest into one archive."""
    try:
        service_id = resolve_service(None, service)
        suite_dir = suite or _default_suite(service_id)
        destination = output or Path(f"hermetic-tests-{service_id}.zip")
        packed = write_suite_zip(service_id, suite_dir, destination)
    except (InvalidServiceError, ExportError, OSError) as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=2) from exc
    typer.echo(f"wrote {destination} ({len(packed)} files)")


@app.command()
def publish(
    service: Annotated[str, typer.Option(help="The microservice whose tests to publish.")],
    suite: Annotated[
        Path | None, typer.Option(help="The suite directory; default tests/<service>.")
    ] = None,
    publisher: Annotated[
        str,
        typer.Option(
            help="Where to deliver the suite: 'local' leaves it where it is; other names come "
            "from plugins in the hermetic.publishers entry-point group."
        ),
    ] = "local",
) -> None:
    """Deliver a service's generated tests with a publisher."""
    try:
        service_id = resolve_service(None, service)
        suite_dir = suite or _default_suite(service_id)
        require_tests(suite_dir)
        chosen = load_publisher(publisher)
        try:
            where = chosen.publish(service_id, suite_dir)
        except Exception as exc:  # a third-party publisher can fail in any way
            raise PublisherError(f"publisher {publisher!r} failed: {exc}") from exc
    except (InvalidServiceError, ExportError, PublisherError) as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=2) from exc
    typer.echo(f"published {service_id} to {where}")


@app.command()
def run(
    target: Annotated[str, typer.Option(help="Base URL of the service under test.")],
    suite: Annotated[
        Path | None,
        typer.Argument(
            help="A pytest suite: a directory or a test file. Default: tests/<service>."
        ),
    ] = None,
    service: Annotated[
        str | None,
        typer.Option(help="The microservice under test; default: the --spec title."),
    ] = None,
    spec: Annotated[Path | None, typer.Option(help="OpenAPI file, to record its hashes.")] = None,
    docs: Annotated[
        Path | None, typer.Option(help="Design documents, to record their hashes.")
    ] = None,
    history_dir: Annotated[
        str,
        typer.Option(
            help="Where run history is stored: a directory, or the address of a Hermetic "
            "server (http:// or https://; the token is read from HERMETIC_API_TOKEN)."
        ),
    ] = ".hermetic/runs",
    report_dir: Annotated[Path, typer.Option(help="Where file reports are written.")] = Path(
        "hermetic-reports"
    ),
    report: Annotated[
        list[str] | None,
        typer.Option(help="Sink to publish to (repeatable): markdown, html, json, stdout."),
    ] = None,
    generate_first: Annotated[
        bool, typer.Option("--generate", help="Update the suite from --spec before running it.")
    ] = False,
    timeout: Annotated[float, typer.Option(help="Per-request timeout in seconds.")] = 30.0,
) -> None:
    """Run a suite against a target, record the run and publish reports."""
    if docs is not None and spec is None:
        typer.echo("error: --docs needs --spec: the docs are read together with it", err=True)
        raise typer.Exit(code=2)
    if generate_first and spec is None:
        typer.echo("error: --generate needs --spec: the tests are written from it", err=True)
        raise typer.Exit(code=2)
    record: RunRecord | None = None
    try:
        model: ServiceModel | None = run_ingest(spec, docs) if spec is not None else None
        service_id = resolve_service(model.title if model else None, service)
        suite_dir = suite or _default_suite(service_id)
        changes: list[SuiteChange] = []
        author = make_author() if generate_first else None
        # What a typo can break is checked before the step that pays for model calls and
        # rewrites the suite, so a bad `--report` does not cost a generation.
        check_target(target)
        sinks = load_sinks(report or ["stdout"], SinkContext(output_dir=report_dir))
        store = open_store(history_dir)
        if model is not None and author is not None:
            changes = generate_suite(model, author, suite_dir)
            _echo_changes(changes)
        record = run_suite(
            suite_dir,
            target,
            store,
            sinks,
            service=service_id,
            model=model,
            changes=changes,
            prompt_version=author.prompt_version if author else None,
            timeout=timeout,
        )
    except (
        IngestError,
        InvalidServiceError,
        GenerateError,
        ManifestError,
        ExecuteError,
        RunStoreError,
        SinkError,
        OSError,
    ) as exc:
        typer.echo(f"error: {exc}", err=True)
        # A sink or store failure comes after the run happened: tests that failed still exit 1.
        record = exc.record if isinstance(exc, SinkError | RunStoreError) else None
        if record is None:
            raise typer.Exit(code=2) from exc
        raise typer.Exit(code=1 if _any_failed(record) else 2) from exc
    if _any_failed(record):
        raise typer.Exit(code=1)


@app.command()
def serve(
    history_dir: Annotated[
        str, typer.Option(help="Where run history is kept, and accepted runs are saved to.")
    ] = ".hermetic/runs",
    host: Annotated[
        str, typer.Option(help="Address to listen on; loopback keeps it on this machine.")
    ] = "127.0.0.1",
    port: Annotated[int, typer.Option(min=1, max=65535, help="Port to listen on.")] = 8000,
    allowed_host: Annotated[
        list[str] | None,
        typer.Option(
            "--allowed-host",
            help=(
                "A host name the UI answers to, besides loopback and this machine's own names "
                "(repeatable; a port is ignored). '*' answers to any, which leaves it open to "
                "DNS rebinding."
            ),
        ),
    ] = None,
) -> None:
    """Serve the web UI over the run history; with HERMETIC_API_TOKEN set, also accept runs."""
    import uvicorn  # only `serve` needs the server, so the other commands skip its import cost

    from hermetic.web import WILDCARD, create_app, hostname

    if any(not hostname(entry) for entry in allowed_host or []):
        typer.echo("error: --allowed-host needs a host name, not an empty value", err=True)
        raise typer.Exit(code=2)
    hosts = _allowed_hosts(host, allowed_host or [])
    if host not in LOOPBACK_NAMES:
        typer.echo(
            f"warning: listening on {host}: anyone who can reach this port can read the run "
            "history, including request and response bodies (secrets are redacted)",
            err=True,
        )
    # Compared as the app compares them: a port is dropped, so '*:8000' is still the wildcard.
    if any(hostname(entry) == WILDCARD for entry in hosts):
        typer.echo(
            "warning: --allowed-host '*' turns off the Host check, so a web page you open can "
            "reach this server by rebinding its own domain to it, even if the port is firewalled",
            err=True,
        )
    token = os.environ.get(API_TOKEN_ENV) or None
    if token and not is_header_token(token):  # no client could send it
        typer.echo(
            f"error: {API_TOKEN_ENV} must be printable ASCII without spaces, so clients can send "
            "it",
            err=True,
        )
        raise typer.Exit(code=2)
    if token and len(token) < MIN_TOKEN_LENGTH:  # the only guard on writes, and not rate limited
        typer.echo(
            f"error: {API_TOKEN_ENV} must be at least {MIN_TOKEN_LENGTH} characters; make one with "
            "python -c 'import secrets; print(secrets.token_urlsafe(32))'",
            err=True,
        )
        raise typer.Exit(code=2)
    if token:
        typer.echo(
            "ingest is on: runs are accepted at /api/services/<service>/runs with the "
            f"{API_TOKEN_ENV} bearer token",
            err=True,
        )
        if host not in LOOPBACK_NAMES:
            typer.echo(
                "warning: the token and the runs travel unencrypted unless a TLS proxy fronts "
                "this server",
                err=True,
            )
    try:
        store = open_store(history_dir)
    except RunStoreError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=2) from exc
    uvicorn.run(create_app(store, hosts, token), host=host, port=port)


MIN_TOKEN_LENGTH = 32


def _binds_every_interface(listen: str) -> bool:
    if not listen.strip():  # asyncio treats an empty host as every interface
        return True
    try:
        return ipaddress.ip_address(listen.strip("[]")).is_unspecified
    except ValueError:  # a host name, not an address
        return False


def _allowed_hosts(listen: str, extra: Sequence[str]) -> list[str]:
    """The `Host` values the UI answers to; anything else is refused.

    That is loopback, the names given with --allowed-host and the address it is asked to bind.
    On every interface no client sends the bound address, so this machine's own names stand in
    for it. A web page that rebinds its own domain to this server is not on the list.
    """
    from hermetic.web import LOOPBACK_HOSTS

    hosts = [*LOOPBACK_HOSTS, *extra]
    if _binds_every_interface(listen):
        hosts.extend(_machine_hosts())
    else:
        hosts.append(f"[{listen}]" if ":" in listen else listen)
    return hosts


def _machine_hosts() -> list[str]:
    """What a client on this machine's network may call it: its host name and its addresses."""
    name = socket.gethostname()
    try:
        found = socket.getaddrinfo(name, None)
    except (OSError, UnicodeError):  # unresolvable or unencodable name; the name still counts
        return [name]
    # IPv4 and IPv6 alike; an IPv6 address is bracketed and loses its zone, as in a `Host` header.
    addresses = {str(info[4][0]).partition("%")[0] for info in found}
    return [name, *sorted(f"[{a}]" if ":" in a else a for a in addresses)]


def _any_failed(record: RunRecord) -> bool:
    return any(result.status in ("failed", "error") for result in record.results)
