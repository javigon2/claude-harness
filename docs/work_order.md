# Work order schema

A work order is one self-contained task for one worker. The worker gets the order and
nothing else: no chat history, no plan, no other orders. Everything it needs to finish
without exploring or asking must be in the order.

A plan (`plan.json`) is a list of work orders. The runner reads the orders; the worker
reads one.

## Fields

| Field | Type | Required | Meaning |
|---|---|---|---|
| `id` | string | yes | Unique within the plan. Short, stable, e.g. `T3` or `cli-done`. Used for branch names, so `[a-z0-9-]` only (uppercase is lowered). |
| `title` | string | yes | One line, imperative. Shown in status tables. |
| `goal` | string | yes | 1–3 sentences: the observable outcome when the task is done, and why it matters. Not the steps. |
| `tier` | `"haiku"` \| `"sonnet"` \| `"opus"` | yes | The tier the first attempt runs on. Failures escalate upward from here. |
| `depends_on` | list of ids | yes (may be `[]`) | Orders that must be merged before this one starts. The runner computes waves from this. |
| `files_owned` | list of paths | yes, non-empty | The only files this task may create, edit or delete. Exact repo-relative paths, no globs. A file that doesn't exist yet is allowed and means "create it". No two orders in the same wave may share a path. |
| `files_readonly` | list of paths | yes (may be `[]`) | Files the worker may read but not change. Listing them tells the worker where the relevant code is; their relevant parts should still be pasted into `context`. A file pasted in `context` with a label saying "whole file" is current, and the worker doesn't need to open it. |
| `context` | list of `{label, content}` | yes | Pasted excerpts, interfaces, decisions and conventions the worker needs. `label` says where it came from (`src/todo/store.py, lines 1-40` or `decision`). `content` is the literal text. Paste it; never write "see X". |
| `steps` | list of strings | yes | Ordered, concrete actions. Each step names the file it touches. |
| `constraints` | list of strings | yes (may be `[]`) | Rules the result must follow that the acceptance commands may not catch: style, no new dependencies, error messages, compatibility. |
| `acceptance` | list of shell commands | yes, non-empty | Run in order from the repo root of the worktree. The task passes only if every command exits 0. This decides "done", not the worker's report. |
| `budget` | `{max_turns, max_attempts}` | yes | `max_turns`: agent turns per attempt. `max_attempts`: total attempts across all tiers, including the first. |
| `out_of_scope` | list of strings | yes (may be `[]`) | Things the worker might reasonably be tempted to do and must not. Each item says what is excluded, and where helpful, why or which order owns it. |

All fields are required so a missing one is a validation error rather than a silent
default. Lists that genuinely have nothing in them are written as `[]`.

## Rules for writing a good order

- **No exploring.** If the worker would need to open a file to learn a signature, a
  naming convention or a test pattern, paste that part into `context`.
- **Acceptance first.** Write the acceptance commands before the steps. If you can't
  write a command that fails before the work and passes after, the order isn't ready.
- **Name new tests in `files_owned`.** Acceptance usually runs tests the worker writes;
  those test files must be owned by the order.
- **Paste the checker's config.** If acceptance runs a linter, formatter or type checker,
  paste its config section into `context`, so the worker knows which rules apply.
- **Exact behaviour, not intent.** Give the exact output strings, exit codes and error
  cases. "Handle errors gracefully" is not a step.
- **Name what fills each placeholder.** In an exact message such as
  `"overlapping files_owned in wave <n>: <path>"`, say which value goes in each
  placeholder: for example, the path as the user wrote it, or after normalising.
- **Say what happens when a command fails.** For every external command the code runs,
  including cleanup and abort commands (`git rebase --abort`, `rm`), say whether a
  failure is ignored, raised, or reported.
- **Small.** About 30 minutes of work or less. If the steps list goes past ~8 items or
  `files_owned` past ~4 files, split the order.
- **Self-check before handing off:** could a fresh Sonnet session do this without asking
  a single question?

## Complete example

The repo is a small Python CLI todo app (`typer`, `pytest`, `uv`). The store already
supports marking a todo done; this order adds the CLI command.

```json
{
  "id": "cli-done",
  "title": "Add `todo done <id>` command",
  "goal": "Users can mark a todo as done from the CLI with `todo done <id>`. Unknown ids fail with a clear message and exit code 1, so scripts can detect the error.",
  "tier": "haiku",
  "depends_on": [],
  "files_owned": [
    "src/todo/cli.py",
    "tests/test_cli_done.py"
  ],
  "files_readonly": [
    "src/todo/store.py",
    "tests/test_cli_add.py"
  ],
  "context": [
    {
      "label": "src/todo/store.py (public interface, unchanged by this order)",
      "content": "from dataclasses import dataclass\nfrom pathlib import Path\n\n@dataclass\nclass Todo:\n    id: int\n    text: str\n    done: bool = False\n\nclass TodoNotFound(KeyError):\n    \"\"\"Raised when no todo has the given id.\"\"\"\n\nclass Store:\n    def __init__(self, path: Path) -> None: ...\n    def add(self, text: str) -> Todo: ...\n    def list(self) -> list[Todo]: ...\n    def mark_done(self, todo_id: int) -> Todo:\n        \"\"\"Set done=True, save, and return the todo. Idempotent: marking an\n        already-done todo returns it unchanged. Raises TodoNotFound if no todo\n        has this id.\"\"\""
    },
    {
      "label": "src/todo/cli.py (whole file today)",
      "content": "import os\nfrom pathlib import Path\n\nimport typer\n\nfrom todo.store import Store\n\napp = typer.Typer()\n\n\ndef _store() -> Store:\n    return Store(Path(os.environ.get(\"TODO_FILE\", \"todos.json\")))\n\n\n@app.command()\ndef add(text: str) -> None:\n    todo = _store().add(text)\n    typer.echo(f\"Added {todo.id}: {todo.text}\")\n\n\n@app.command(\"list\")\ndef list_() -> None:\n    for todo in _store().list():\n        mark = \"x\" if todo.done else \" \"\n        typer.echo(f\"[{mark}] {todo.id}: {todo.text}\")"
    },
    {
      "label": "tests/test_cli_add.py (the test pattern to copy)",
      "content": "from typer.testing import CliRunner\n\nfrom todo.cli import app\n\nrunner = CliRunner()\n\n\ndef test_add_prints_id(tmp_path, monkeypatch):\n    monkeypatch.setenv(\"TODO_FILE\", str(tmp_path / \"todos.json\"))\n    result = runner.invoke(app, [\"add\", \"buy milk\"])\n    assert result.exit_code == 0\n    assert result.output == \"Added 1: buy milk\\n\""
    },
    {
      "label": "pyproject.toml [tool.ruff] section",
      "content": "[tool.ruff]\nline-length = 100\n\n[tool.ruff.lint]\nselect = [\"E\", \"F\", \"I\", \"B\", \"UP\"]"
    },
    {
      "label": "decision",
      "content": "Ids start at 1 and increase by 1 per `add`. Errors go to stderr with typer.echo(..., err=True). In tests, check error text with `in result.output`, which contains stderr on every Click version this repo supports."
    }
  ],
  "steps": [
    "In src/todo/cli.py, import TodoNotFound from todo.store alongside Store.",
    "At the end of src/todo/cli.py, add a `done(todo_id: int)` command registered as `done`. Call `_store().mark_done(todo_id)` and print `Done <id>: <text>` with typer.echo.",
    "In the same command, catch TodoNotFound, print `No todo with id <id>` to stderr with typer.echo(..., err=True), and exit with `raise typer.Exit(code=1) from None` (ruff B904 is enabled).",
    "Create tests/test_cli_done.py following the pattern in tests/test_cli_add.py, with three tests: (1) add one todo, run `done 1`, assert exit 0 and output `Done 1: buy milk\\n`, then run `list` and assert it prints `[x] 1: buy milk\\n`; (2) run `done 7` on an empty store, assert exit 1 and `No todo with id 7` in result.output; (3) run `done 1` twice, assert both exit 0 with the same output."
  ],
  "constraints": [
    "Do not change src/todo/store.py; it already does the work.",
    "No new dependencies.",
    "Keep the output format exactly `Done <id>: <text>`, matching the `Added <id>: <text>` style.",
    "Use typer.echo, not print."
  ],
  "acceptance": [
    "uv run pytest tests/test_cli_done.py -q",
    "uv run pytest -q",
    "uv run ruff check src tests"
  ],
  "budget": {
    "max_turns": 20,
    "max_attempts": 3
  },
  "out_of_scope": [
    "An `undone`/`undo` command.",
    "Marking several ids at once (`done 1 2 3`).",
    "Changing how `list` displays todos.",
    "Refactoring `_store()` or the existing commands."
  ]
}
```
