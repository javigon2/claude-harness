# Plan: T1.8 Scheduler + T1.9 CLI and report

This stage turns the router into a runner. `state.py` keeps `RunState` in
`<repo>/.harness/runs/<run_id>/state.json` (atomic rewrite after every transition; run id =
UTC `YYYYmmdd-HHMMSS`). `WorktreeManager.discard` removes one task's worktree and branch.
`scheduler.py` runs the waves in order, tasks in a wave concurrently under
`asyncio.Semaphore(max_parallel)`, each in its own worktree through the router; passing
tasks are committed and fast-forward merged (all git calls on the main repo under one lock),
dependents of failed/blocked tasks become `blocked`, and `--resume` skips done tasks.
`report.py` prints the status table and the per-task/per-tier token and cost report. The
CLI wires `validate`, `run [--max-parallel] [--review] [--resume] [--fake]`, `status` and
`report`. Five orders in three waves, all on Opus.

## Waves

| Wave | Order | Title | Tier | Depends on |
|---|---|---|---|---|
| 1 | `state` | Run state on disk: RunState, save/load, run ids | opus | — |
| 1 | `worktree-discard` | WorktreeManager.discard(task_id) | opus | — |
| 2 | `scheduler` | Waves, parallel tasks, merge, blocked propagation, resume | opus | state, worktree-discard |
| 2 | `report` | Status table, per-task and per-tier tokens and cost | opus | state |
| 3 | `cli` | Wire up validate, run, status and report | opus | scheduler, report |

## Orders

- **`state`** owns `src/harness/state.py`, `tests/test_state.py`. Acceptance: own tests, suite, ruff.
- **`worktree-discard`** owns `src/harness/worktree.py`, `tests/test_worktree.py` (whole target file pasted).
- **`scheduler`** owns `src/harness/scheduler.py`, `tests/test_scheduler.py`; 6-task DAG test,
  parallelism, resume, merge conflict, prechecks. 50 turns.
- **`report`** owns `src/harness/report.py`, `tests/test_report.py`; exact text formats.
- **`cli`** owns `src/harness/cli.py`, `tests/test_cli.py`; every test uses `--fake`.

## Decisions you may want to overrule

1. Run state in `.harness/runs/<run_id>/` (state.json + events.jsonl); run id = UTC timestamp
   unless `--run-id`; `status`/`report`/`--resume` default to the latest run.
2. Whole waves run in order (no streaming of ready tasks across waves), per PLAN.md.
3. The run refuses to start unless the repo is on the integration branch (`main`), and refuses
   to reuse an existing run id without `--resume`.
4. Merged tasks' worktrees and branches are removed right after the merge; failed/blocked
   ones are kept for inspection. A merge conflict fails the task (no automatic retry).
5. `--fake` = a worker that reports done with no changes (1000 in / 200 out tokens, $0), so
   the plan's own acceptance commands still decide; with `--review`, a reviewer that passes.
6. `harness run` exits 1 unless every task is done, and always prints the report.
7. Reviewer tier default `sonnet` (`--review-tier`); review only with `--review`.

## Prerequisites and risks

- A task marked done by the router is saved as done before its merge; a crash between the
  two would make `--resume` skip an unmerged task (small window, noted).
