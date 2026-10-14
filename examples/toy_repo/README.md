# todo

A tiny command-line todo list, used as the toy project for the agent-harness end-to-end run.

    uv run todo add "buy milk"
    uv run todo list

Todos live in `todos.json` in the current directory (override with `TODO_FILE`).

Test: `uv run pytest -q`. Lint: `uv run ruff check . && uv run ruff format --check .`
