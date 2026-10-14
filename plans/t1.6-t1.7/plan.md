# Plan: T1.6 Verifier + T1.7 Router

This stage delivers the part of the runner that decides whether an attempt passed and what
happens next. `review.py` adds a read-only reviewer session (SDK, injectable query, fake for
tests). `verify.py` runs every acceptance command in the worktree with a timeout (last 200
lines of output kept), checks that the diff stays inside `files_owned` (git-ignored files
and caches excluded), calls the reviewer only when both pass, and turns a failure into retry
feedback. `router.py` runs attempts up the tier ladder within `budget.max_attempts`, sums
usage into `TaskState`, records an `AttemptRecord` per attempt, emits an event for every
transition, and ends the task `done`, `failed` or `blocked` (with a replan note). Three
orders in three waves, all on Opus for this build.

## Waves

| Wave | Order | Title | Tier | Depends on |
|---|---|---|---|---|
| 1 | `review` | Reviewer protocol, SdkReviewer, FakeReviewer, verdict parser | opus | — |
| 2 | `verify` | Acceptance commands, files_owned diff check, optional review | opus | review |
| 3 | `router` | Attempts, verification, escalation, blocked state (+ models additions) | opus | verify |

## Orders

### `review`
- **Owns:** `src/harness/review.py`, `src/harness/prompts/reviewer_system.md`, `tests/test_review.py`
- Interface skeleton and prompt text pasted (prompt sha256-checked). Read-only tools
  (Read/Glob/Grep), `permission_mode="dontAsk"`, same isolation as SdkWorker.
- **Acceptance:** own tests, full suite, ruff check, ruff format --check, prompt sha256.

### `verify`
- **Owns:** `src/harness/verify.py`, `tests/test_verify.py`
- Shell commands in a new process group, killed on timeout/cancel; `git diff --name-only
  <base>` + `git ls-files --others --exclude-standard`, minus built-in caches; exact
  feedback format.
- **Acceptance:** own tests, full suite, ruff check, ruff format --check.

### `router`
- **Owns:** `src/harness/router.py`, `tests/test_router.py`, `src/harness/models.py`, `tests/test_models.py`
- `models.py` gets `Usage.plus`, `AttemptRecord`, `TaskState.history/replan_note/error`
  (whole target file pasted). Event names and payloads pinned.
- **Acceptance:** own tests, full suite, ruff check, ruff format --check.

## Decisions you may want to overrule

1. Diff check ignores git-ignored untracked files plus `__pycache__/`, `.pytest_cache/`,
   `.ruff_cache/`, `.venv/`, `*.pyc`; acceptance timeout 600 s per command.
2. All acceptance commands run even after one fails, so the retry feedback shows every failure.
3. Only a review verdict of `fail` fails a task; `unknown`/`error` are recorded in the
   `verify_finished` event but pass (the commands are the truth).
4. Escalation: one tier up per failed attempt, staying on Opus once there. Budget used up
   below Opus -> `failed`; last failure on Opus -> `blocked` with a mechanical replan note
   (no model call).
5. The worker's own status never decides; a worker exception counts as an `error` attempt.
6. `VIRTUAL_ENV` is removed from the acceptance-command environment; leftover background
   processes in the command's process group are killed.

## Prerequisites and risks

- `SdkReviewer` is only tested with a fake query; its first real use is the T1.11 run.
- `permission_mode="dontAsk"` is new to this repo; if the CLI rejects it, switch to `"default"`.
