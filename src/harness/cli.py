"""Command-line entry point: `harness <command>`."""

from pathlib import Path
from typing import Annotated

import typer

from harness.events import EVENTS_FILE
from harness.events import follow as follow_lines

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


@app.command()
def events(
    run_dir: Path,
    follow: Annotated[bool, typer.Option("--follow", "-f", help="Keep printing.")] = False,
) -> None:
    """Print a run's event log (RUN_DIR/events.jsonl)."""
    path = run_dir / EVENTS_FILE
    if follow:
        for line in follow_lines(path):
            typer.echo(line)
        return
    if not path.exists():
        typer.echo(f"No event log at {path}", err=True)
        raise typer.Exit(code=1)
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            typer.echo(line)
