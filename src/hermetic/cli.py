"""Command-line entry point."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from hermetic import __version__
from hermetic.ingest import IngestError
from hermetic.ingest import ingest as run_ingest

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
