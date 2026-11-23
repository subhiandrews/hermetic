"""Command-line entry point."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from hermetic import __version__
from hermetic.agent import ClaudeAuthor
from hermetic.execute import ExecuteError
from hermetic.generate import GenerateError, ManifestError, SuiteAuthor, generate_suite
from hermetic.history import RunRecord, RunStoreError, SuiteChange, open_store
from hermetic.ingest import IngestError, ServiceModel
from hermetic.ingest import ingest as run_ingest
from hermetic.report import SinkContext, SinkError, load_sinks
from hermetic.runner import run_suite

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
    suite: Annotated[Path, typer.Option(help="Directory the test suite is written to.")],
    docs: Annotated[
        Path | None, typer.Option(help="Directory of design/architecture documents.")
    ] = None,
    force: Annotated[bool, typer.Option(help="Rewrite every test, not only changed ones.")] = False,
) -> None:
    """Write or update the test suite for the spec; only changed operations are rewritten."""
    try:
        changes = generate_suite(run_ingest(spec, docs), make_author(), suite, force=force)
    except (IngestError, GenerateError, ManifestError, OSError) as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=2) from exc
    _echo_changes(changes)


def _echo_changes(changes: list[SuiteChange]) -> None:
    if not changes:
        typer.echo("suite is up to date")
    for change in changes:
        typer.echo(f"{change.kind}: {change.test_id} ({change.reason})")


@app.command()
def run(
    suite: Annotated[Path, typer.Argument(help="A pytest suite: a directory or a test file.")],
    target: Annotated[str, typer.Option(help="Base URL of the service under test.")],
    spec: Annotated[Path | None, typer.Option(help="OpenAPI file, to record its hashes.")] = None,
    docs: Annotated[
        Path | None, typer.Option(help="Design documents, to record their hashes.")
    ] = None,
    history_dir: Annotated[Path, typer.Option(help="Where run history is stored.")] = Path(
        ".hermetic/runs"
    ),
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
        changes: list[SuiteChange] = []
        author = make_author() if generate_first else None
        # What a typo can break is checked before the step that pays for model calls and
        # rewrites the suite, so a bad `--report` does not cost a generation.
        sinks = load_sinks(report or ["stdout"], SinkContext(output_dir=report_dir))
        store = open_store(history_dir)
        if model is not None and author is not None:
            changes = generate_suite(model, author, suite)
            _echo_changes(changes)
        record = run_suite(
            suite,
            target,
            store,
            sinks,
            model=model,
            changes=changes,
            prompt_version=author.prompt_version if author else None,
            timeout=timeout,
        )
    except (
        IngestError,
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


def _any_failed(record: RunRecord) -> bool:
    return any(result.status in ("failed", "error") for result in record.results)
