# Plan: Phase 1 foundation (T1.1–T1.4)

Delivers the runner's base: an installable `harness` CLI with model IDs in one config module, pydantic models for work orders, plans, task state and events, plan loading with validation and wave computation, a concurrency-safe JSONL event log with `harness events [--follow]`, and a git worktree manager (create / commit / rebase-merge / cleanup). Five orders in three waves; nothing calls a model yet.

## Waves

| Wave | Order | Title | Tier | Depends on |
|---|---|---|---|---|
| 1 | `scaffold` | Create the uv project, CLI stub and model config | haiku | — |
| 2 | `models` | Add pydantic models for orders, plans, task state and events | haiku | scaffold |
| 2 | `worktree` | Add the git worktree manager | sonnet | scaffold |
| 3 | `events` | Add the JSONL event log and `harness events` | haiku | models |
| 3 | `plan-validate` | Load and validate plan files and compute waves | sonnet | models |

## Orders

### `scaffold` — Create the uv project, CLI stub and model config

The repo becomes an installable uv project: `uv run harness --help` lists the four commands, `uv run pytest` passes a smoke test, and ruff is configured. Model IDs live in one config module that later tasks import.

- **Owns:** `pyproject.toml`, `.python-version`, `.gitignore`, `uv.lock`, `src/harness/__init__.py`, `src/harness/config.py`, `src/harness/cli.py`, `tests/test_smoke.py`
- **Acceptance:** `uv sync`; `uv run harness --help`; `uv run pytest -q`; `uv run ruff check .`; `uv run ruff format --check .`
- **Budget:** 20 turns, 3 attempts

### `models` — Add pydantic models for orders, plans, task state and events

src/harness/models.py defines WorkOrder, Plan, TaskState, Usage and Event exactly as specified, so the plan loader, event log and runner share one set of types. Bad orders (missing or unknown fields, empty acceptance or files_owned, bad ids) are rejected at parse time.

- **Owns:** `src/harness/models.py`, `tests/test_models.py`
- **Acceptance:** `uv run pytest tests/test_models.py -q`; `uv run pytest -q`; `uv run ruff check .`; `uv run ruff format --check .`
- **Budget:** 30 turns, 3 attempts

### `worktree` — Add the git worktree manager

src/harness/worktree.py gives each task its own branch and worktree, merges finished work back by rebase + fast-forward, reports conflicts without losing the worktree, and cleans up a run. Only subprocess git is used.

- **Owns:** `src/harness/worktree.py`, `tests/test_worktree.py`
- **Acceptance:** `uv run pytest tests/test_worktree.py -q`; `uv run pytest -q`; `uv run ruff check .`; `uv run ruff format --check .`
- **Budget:** 40 turns, 3 attempts

### `events` — Add the JSONL event log and `harness events`

Every state change can be recorded with EventLog.emit() as one JSON line in <run_dir>/events.jsonl, safely from many concurrent async tasks, and `harness events RUN_DIR [--follow]` prints the log. This is what the future UI reads.

- **Owns:** `src/harness/events.py`, `tests/test_events.py`, `src/harness/cli.py`
- **Acceptance:** `uv run pytest tests/test_events.py -q`; `uv run pytest -q`; `uv run ruff check .`; `uv run ruff format --check .`
- **Budget:** 30 turns, 3 attempts

### `plan-validate` — Load and validate plan files and compute waves

load_plan(path) turns plan.json into a Plan or raises PlanError with a clear message for duplicate ids, unknown dependencies, cycles, blank acceptance commands and two orders in the same wave owning the same file. compute_waves gives the parallel waves the scheduler will run.

- **Owns:** `src/harness/plan.py`, `tests/test_plan.py`
- **Acceptance:** `uv run pytest tests/test_plan.py -q`; `uv run pytest -q`; `uv run ruff check .`; `uv run ruff format --check .`
- **Budget:** 30 turns, 3 attempts

## Decisions you may want to overrule

- **T1.2 split in two** (`models`, `plan-validate`): keeps each order under ~30 min and lets `events` start on the models without waiting for validation.
- **Scaffold and models are fully specified** (literal file contents / exact class code), so both are Haiku. The plan had T1.2 on Sonnet.
- **Run-time layout:** event logs in `<run_dir>/events.jsonl`; `harness events RUN_DIR` takes the run dir explicitly (no "current run" lookup yet).
- **Worktree API is a class** `WorktreeManager(repo, run_id, integration_branch="main")` with an extra `commit()`; `merge()` refuses a dirty worktree instead of committing for the worker.
- **Blank acceptance commands** are rejected in `plan.py`; an empty list is already rejected by the model.
- **pytest-asyncio** with `asyncio_mode = "auto"`, added in the scaffold for the event-log concurrency test.
- **No `claude-agent-sdk` yet**; the worker order (T1.5) adds it.

## Prerequisites and risks

- **The project is not a git repo yet.** Before any order runs in a worktree: `git init -b main`, commit the current files (PLAN.md, docs/, .claude/). The scaffold's `.gitignore` arrives with the first order.
- **uv needs network** the first time: it downloads Python 3.12 (only 3.11/3.13/3.14 are installed) and the dependencies.
- **Wave 3 orders run the full suite** in their own worktrees; a broken merge in wave 2 fails both.
- **`follow()` has no automated test**; check it by hand during the dry run.
