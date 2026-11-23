"""Command-line entry point."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from hermetic import __version__
from hermetic.execute import ExecuteError
from hermetic.history import RunStoreError, open_store
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
    timeout: Annotated[float, typer.Option(help="Per-request timeout in seconds.")] = 30.0,
) -> None:
    """Run a hand-written suite against a target, record the run and publish reports."""
    if docs is not None and spec is None:
        raise typer.BadParameter("--docs needs --spec: the docs are read together with the spec")
    try:
        model: ServiceModel | None = run_ingest(spec, docs) if spec is not None else None
        sinks = load_sinks(report or ["stdout"], SinkContext(output_dir=report_dir))
        record = run_suite(
            suite, target, open_store(history_dir), sinks, model=model, timeout=timeout
        )
    except (IngestError, ExecuteError, RunStoreError, SinkError) as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=2) from exc
    if any(result.status in ("failed", "error") for result in record.results):
        raise typer.Exit(code=1)
