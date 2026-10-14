"""Command-line entry point: `harness <command>`."""

from pathlib import Path

import typer

app = typer.Typer(help="Plan-and-execute harness for Claude agents.", no_args_is_help=True)


def _not_implemented(name: str) -> None:
    typer.echo(f"harness {name}: not implemented yet", err=True)
    raise typer.Exit(code=1)


@app.command()
def run(plan: Path) -> None:
    """Run a plan."""
    _not_implemented("run")


@app.command()
def status() -> None:
    """Show task states for the current run."""
    _not_implemented("status")


@app.command()
def report() -> None:
    """Show tokens and cost per task and per tier."""
    _not_implemented("report")


@app.command()
def validate(plan: Path) -> None:
    """Validate a plan file."""
    _not_implemented("validate")
