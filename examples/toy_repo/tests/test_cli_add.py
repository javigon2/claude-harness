from pathlib import Path

import pytest
from typer.testing import CliRunner

from todo.cli import app

runner = CliRunner()


def test_add_then_list(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TODO_FILE", str(tmp_path / "todos.json"))
    result = runner.invoke(app, ["add", "buy milk"])
    assert result.exit_code == 0
    assert result.output == "Added 1: buy milk\n"
    result = runner.invoke(app, ["list"])
    assert result.output == "[ ] 1: buy milk\n"
