"""Command-line entry point."""

from __future__ import annotations

import typer

from hermetic import __version__

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
