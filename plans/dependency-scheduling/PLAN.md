# Dependency-based scheduling (replace the wave barrier)

Status: stages 1-3 implemented (uncommitted) by Sonnet 5.5 in one session, no work orders.
Decisions taken: critical-path-first ready order; `wave_*` events dropped (`waves` stays in
`run_started` and the UI as a depth grouping); no `task_ready` event, `queue_wait_ms` already
measures ready-to-start wait; `run_finished.duration_ms` added; stop/cap stops new starts only.
Stage 4 (real before/after run) not done.

## Problem

`Scheduler.run` (`src/harness/scheduler.py`, the `for i, wave in enumerate(waves)` loop)
runs one wave with `asyncio.gather` and starts the next wave only when every task in it has
ended. A task whose dependencies are all `done` still waits for unrelated slow tasks.
Evidence: run `20261026-190201`, one Sonnet→Opus escalation held wave 2 for about an hour.
`wave_finished.tail_wait_ms` already measures this.

## Goal

A task starts as soon as all its `depends_on` are `done` and a semaphore slot is free. No
change to what a task does (worktree, router, commit, merge under the lock).

## Design decisions to settle first (big model)

1. **File-overlap safety.** `validate_plan` only rejects overlapping `files_owned` inside a
   wave. Without waves, two orders in different "waves" that touch the same file and are
   not ordered by a dependency path would now run concurrently. New rule: any two orders
   whose `files_owned` overlap must be ordered by a transitive `depends_on` path; otherwise
   `PlanError`. Check against existing plans in `plans/` for plans this newly rejects
   (they were valid only because of wave ordering).
2. **Ready-queue policy.** When several tasks are ready, which gets the next slot? Proposal:
   longest dependency chain first (critical path), then plan order. Keep it deterministic
   for tests.
3. **Event and UI compatibility.** `wave_started`/`wave_finished`, `run_started.waves`,
   `view.py` (`wave`, `waves_total`, feed lines), `cli validate` output, `ui/runs.py`
   `waves` count and `index.html` all read waves. Proposal: keep `compute_waves` as a
   display/validation grouping ("depth"), stop emitting `wave_*` events, and add
   `task_ready` (with `ready_wait_ms`) so queue time stays measurable. Decide whether the
   run progress bar counts tasks instead of waves.
4. **Stop, pause and cost cap.** Today a rate limit or cost cap stops *later waves* after the
   current one drains. New behaviour: stop starting new tasks, let running ones finish,
   same `run_stopping` event, same exit code 3 / `--resume` semantics. Resume must skip
   `done` tasks and re-evaluate readiness from `state.json`.
5. **Failure propagation.** A failed/blocked/paused dependency blocks dependents
   immediately (no waiting for a wave to end), reusing the existing `task_blocked` event
   and reason text.

## Stages

1. **Plan validation** (`plan.py`, tests): transitive-ordering rule for overlapping
   `files_owned`; keep `compute_waves` for depth display. Check existing `plans/*/plan.json`.
2. **Scheduler core** (`scheduler.py`, `tests/test_scheduler.py`): replace the wave loop with
   a ready-set loop (tasks launched as dependencies finish, bounded by the semaphore).
   Keep `_run_task`, `request_stop`, `_should_stop`, merge lock and resume logic unchanged.
   Tests: a slow task in the first depth no longer delays an independent chain; dependents
   of a failed task are blocked at once; stop/budget/rate-limit stop new starts only;
   resume after a stop; deterministic ordering.
3. **Events, view and UI** (`view.py`, `cli.py`, `ui/runs.py`, `index.html`, their tests):
   remove or replace the wave wording and progress per decision 3.
4. **Measurement**: run the same plan before and after (use `--fake --fake-delay`, then one
   real small run). Compare run wall time and `queue_wait_ms` using `docs/TIMINGS.md`.

Stages 1 and 2 are sequential (2 relies on 1's rule). Stage 3 depends on 2's event choice.

## Out of scope

Flake retry/base-commit check, stalled-run timeout, dirty-tree check, planner speed,
mid-run messaging. Do not change worker, router or verifier behaviour.

## Risks

- Hidden coupling between orders that the plan did not declare was previously masked by
  wave order (e.g. one order reading a file another writes without owning it). Mitigation:
  the file-overlap rule above, plus a note in `plan-stage` that `depends_on` is the only
  ordering guarantee.
- The working tree has uncommitted edits to `scheduler.py`, `router.py` and tests;
  commit or stash them before starting so the new work has a clean base.
