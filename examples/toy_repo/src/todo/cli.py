"""Command-line interface: `todo <command>`."""

import os
from pathlib import Path

import typer

from todo.store import Store

app = typer.Typer(help="A tiny todo list.", no_args_is_help=True)


def _store() -> Store:
    return Store(Path(os.environ.get("TODO_FILE", "todos.json")))


@app.command()
def add(text: str) -> None:
    """Add a todo."""
    todo = _store().add(text)
    typer.echo(f"Added {todo.id}: {todo.text}")


@app.command("list")
def list_() -> None:
    """List todos."""
    for todo in _store().all():
        mark = "x" if todo.done else " "
        typer.echo(f"[{mark}] {todo.id}: {todo.text}")
