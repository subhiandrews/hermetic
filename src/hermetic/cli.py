"""Command-line entry point."""

from __future__ import annotations

import ipaddress
import os
import socket
from collections.abc import Sequence
from pathlib import Path
from typing import Annotated

import typer

from hermetic import __version__
from hermetic.agent import ClaudeAuthor, ClaudeInferrer
from hermetic.discover import (
    DependenciesError,
    DependencyInferrer,
    DiscoverError,
    load_dependencies,
    write_dependencies,
)
from hermetic.discover import discover as run_discovery
from hermetic.execute import ExecuteError, check_target
from hermetic.execute.mocks import MockError, check_mock
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
from hermetic.provision import (
    COMPOSE_NAME,
    MOCKS_DIR,
    ComposeError,
    ProvisionError,
    StackError,
    build_stubs,
    check_compose,
    generate_compose,
    project_name,
    read_compose_file,
    refuse_links,
    write_compose,
    write_stubs,
)
from hermetic.provision import down as stop_stack
from hermetic.provision import up as start_stack
from hermetic.provision.validate import printable
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
    spec: Annotated[Path, typer.Argument(help="OpenAPI 3.x or Swagger 2.0 file (JSON or YAML).")],
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


def make_inferrer() -> DependencyInferrer:
    """The inferrer `discover` uses. Tests replace this with a fake."""
    return ClaudeInferrer()  # pragma: no cover  (needs the real API)


@app.command()
def discover(
    spec: Annotated[Path, typer.Argument(help="OpenAPI 3.x or Swagger 2.0 file (JSON or YAML).")],
    docs: Annotated[Path, typer.Option(help="Directory of design/architecture documents to read.")],
    suite: Annotated[
        Path | None,
        typer.Option(help="Where dependencies.json is kept; default tests/<service>."),
    ] = None,
    service: Annotated[
        str | None,
        typer.Option(help="The microservice to find dependencies for; default: the spec's title."),
    ] = None,
) -> None:
    """Find the databases, caches and HTTP services a service depends on, and save them.

    The result is tests/<service>/.hermetic/dependencies.json: commit it and edit it. An entry
    whose origin you change to "manual" is kept as it is whenever this runs again.
    """
    try:
        model = run_ingest(spec, docs)
        service_id = resolve_service(model.title, service)
        suite_dir = suite or _default_suite(service_id)
        found = run_discovery(model, service_id, make_inferrer(), load_dependencies(suite_dir))
        path = write_dependencies(suite_dir, found.dependencies)
    except (
        IngestError,
        InvalidServiceError,
        DiscoverError,
        DependenciesError,
        OSError,
    ) as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=2) from exc
    for kind, name in found.changes:
        typer.echo(f"{kind}: {name}")
    for note in found.notes:
        typer.echo(f"note: {note}")
    count = len(found.dependencies.dependencies)
    typer.echo(f"wrote {path} ({count} dependencies)")


@app.command()
def compose(
    service: Annotated[str, typer.Option(help="The microservice to write the mock stack for.")],
    suite: Annotated[
        Path | None,
        typer.Option(help="Where dependencies.json is kept; default tests/<service>."),
    ] = None,
    output: Annotated[
        Path | None,
        typer.Option(
            help=f"Where to write the file; default .hermetic/provision/<service>/{COMPOSE_NAME}."
        ),
    ] = None,
) -> None:
    """Write the compose file that serves a service's dependencies, from dependencies.json.

    The file is made by code from templates, then checked against the allowlist. Nothing is
    started: this only writes the file.
    """
    try:
        service_id = resolve_service(None, service)
        suite_dir = suite or _default_suite(service_id)
        found = load_dependencies(suite_dir)
        if found is None:
            raise ProvisionError(f"no dependencies in {suite_dir}; run `hermetic discover` first")
        if found.service != service_id:
            raise ProvisionError(
                f"{suite_dir} holds the dependencies of {found.service!r}, not {service_id!r}"
            )
        destination = output or Path(".hermetic") / "provision" / service_id / COMPOSE_NAME
        text = generate_compose(found, destination.parent)
        write_compose(destination, text)
    except (
        InvalidServiceError,
        DependenciesError,
        ProvisionError,
        ComposeError,
        OSError,
    ) as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=2) from exc
    typer.echo(f"wrote {destination} ({len(found.dependencies)} services)")


@app.command("check-compose")
def check_compose_command(
    file: Annotated[Path, typer.Argument(help="The compose file to check.")],
) -> None:
    """Check a compose file against the allowlist Hermetic applies before it runs one.

    Mounts are checked against the file's own directory, because that is what Compose resolves
    a relative path against when it runs the file.
    """
    try:
        text = read_compose_file(file)
        document = check_compose(text, file.parent)
    except ComposeError as exc:
        for problem in exc.problems:
            typer.echo(f"refused: {problem}", err=True)
        raise typer.Exit(code=1) from exc
    except OSError as exc:
        typer.echo(f"error: cannot read {file}: {exc.strerror}", err=True)
        raise typer.Exit(code=2) from exc
    typer.echo(f"ok: {file} ({len(document['services'])} services)")


@app.command()
def up(
    service: Annotated[str, typer.Option(help="The microservice whose stack to start.")],
    file: Annotated[
        Path | None,
        typer.Option(
            help=f"The compose file; default .hermetic/provision/<service>/{COMPOSE_NAME}."
        ),
    ] = None,
) -> None:
    """Start a service's mock stack and print where each dependency listens, as NAME=URL lines.

    The file is checked against the allowlist first, and the text that passed is what Docker
    gets. The engines hold nothing between runs: `hermetic down` removes them.
    """
    try:
        service_id = resolve_service(None, service)
        path = file or Path(".hermetic") / "provision" / service_id / COMPOSE_NAME
        refuse_links(path)  # before reading anything through it
        endpoints = start_stack(service_id, path, read_compose_file(path))
    except (InvalidServiceError, StackError, ProvisionError, ComposeError, OSError) as exc:
        typer.echo(f"error: {printable(str(exc))}", err=True)
        raise typer.Exit(code=2) from exc
    except KeyboardInterrupt as interrupt:  # Typer would print only "Aborted!" and drop the notes
        for note in getattr(interrupt, "__notes__", []):
            typer.echo(f"error: {printable(note)}", err=True)
        raise
    for endpoint in endpoints:
        typer.echo(f"{endpoint.variable}={endpoint.url}")


@app.command()
def down(
    service: Annotated[str, typer.Option(help="The microservice whose stack to stop.")],
) -> None:
    """Stop and remove a service's mock stack, data included."""
    try:
        service_id = resolve_service(None, service)
        stop_stack(service_id)
    except (InvalidServiceError, StackError) as exc:
        typer.echo(f"error: {printable(str(exc))}", err=True)
        raise typer.Exit(code=2) from exc
    typer.echo(f"stopped {project_name(service_id)}")


@app.command()
def stubs(
    service: Annotated[str, typer.Option(help="The microservice the mock is for.")],
    name: Annotated[str, typer.Option(help="The HTTP dependency to write stubs for.")],
    spec: Annotated[Path, typer.Option(help="The OpenAPI file of that dependency.")],
    suite: Annotated[
        Path | None,
        typer.Option(help="Where dependencies.json is kept; default tests/<service>."),
    ] = None,
    output: Annotated[
        Path | None,
        typer.Option(
            help="Where to write the stubs; default .hermetic/provision/<service>/mocks/<name>."
        ),
    ] = None,
) -> None:
    """Write WireMock stubs for an HTTP dependency from its OpenAPI spec.

    Each operation with a 2xx response gets one stub answering with an example from the spec.
    A declared 4xx or 5xx response also gets a stub, in a WireMock scenario state named after
    its status; the mock answers the 2xx until something switches the state.
    Stubs a former run wrote are replaced; files you wrote yourself are left alone.
    """
    try:
        service_id = resolve_service(None, service)
        found = load_dependencies(suite or _default_suite(service_id))
        if found is None or found.service != service_id:
            raise ProvisionError(f"no dependencies for {service_id!r}; run `hermetic discover`")
        match = next((d for d in found.dependencies if d.name == name), None)
        if match is None or match.kind != "http":
            raise ProvisionError(f"{name!r} is not an HTTP dependency of {service_id!r}")
        built, skipped = build_stubs(run_ingest(spec, None))
        folder = output or Path(".hermetic") / "provision" / service_id / MOCKS_DIR / name
        write_stubs(folder, built)
    except (InvalidServiceError, DependenciesError, ProvisionError, IngestError, OSError) as exc:
        typer.echo(f"error: {printable(str(exc))}", err=True)
        raise typer.Exit(code=2) from exc
    for line in skipped:
        typer.echo(f"skipped: {printable(line)}", err=True)
    typer.echo(f"wrote {len(built)} stubs to {folder}")


@app.command()
def generate(
    spec: Annotated[Path, typer.Argument(help="OpenAPI 3.x or Swagger 2.0 file (JSON or YAML).")],
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


def _parse_mocks(values: list[str]) -> dict[str, str]:
    """`--mock NAME=URL` values as a dict, refusing a typo before any test is run."""
    mocks: dict[str, str] = {}
    for value in values:
        name, separator, url = value.partition("=")
        if not separator or not name or not url:
            # not echoed: the value may hold a URL with a password
            raise ExecuteError("--mock expects NAME=URL, for example carts=http://127.0.0.1:8080")
        try:
            check_mock(
                name, url
            )  # first, so a name that is really the front of a URL is not echoed
        except MockError as exc:
            raise ExecuteError(str(exc)) from exc
        if name in mocks:
            raise ExecuteError(f"--mock {name!r} was given twice")
        mocks[name] = url
    return mocks


def _echo_changes(changes: list[SuiteChange]) -> None:
    if not changes:
        typer.echo("suite is up to date")
    for change in changes:
        typer.echo(f"{change.kind}: {change.test_id} ({change.reason})")


@app.command()
def export(
    service: Annotated[str, typer.Option(help="The microservice whose tests to export.")],
    suite: Annotated[
        Path | None, typer.Option(help="The suite directory; default tests/<service>.")
    ] = None,
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
            help="Where to deliver the suite: 'local' leaves it where it is, 'github-pr' opens "
            "a pull request (see docs/architecture.md); other names come from plugins in the "
            "hermetic.publishers entry-point group."
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
    except (InvalidServiceError, ExportError, PublisherError, OSError) as exc:
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
    mock: Annotated[
        list[str] | None,
        typer.Option(
            help="A mock whose declared errors tests may switch on, as NAME=URL (repeatable): "
            "the URL `hermetic up` printed for that dependency."
        ),
    ] = None,
) -> None:
    """Run a suite against a target, record the run and publish reports.

    Tests reach the target only. With --mock, a test can also make that mock answer with an
    error its spec declares, through the `hermetic_mocks` fixture; whatever it switched is
    reset when the test ends.
    """
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
        mocks = _parse_mocks(mock or [])
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
            mocks=mocks,
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
