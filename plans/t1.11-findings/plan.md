# t1.11-findings: fix H1-H3 from the MVP run

Fixes the three harness findings from T1.11 (`docs/RESULTS.md`). H1: review cost is booked on
the reviewer's tier. H2: `harness plan --stage N` plans any stage of an approved project
plan. H3: all planner output goes to the git-excluded `<repo>/.harness/plans/`.

| Wave | Order | Title | Tier | Depends on |
|---|---|---|---|---|
| 1 | attempt-review-usage | Keep review usage apart from worker usage in each attempt | opus | - |
| 1 | worktree-ensure-excluded | Expose ensure_excluded(repo) in harness.worktree | opus | - |
| 2 | report-review-tier | Book review cost on the reviewer's tier in the report | opus | attempt-review-usage |
| 2 | plan-stage-n | harness plan --stage N, planner output under .harness/plans/ | opus | worktree-ensure-excluded |

## Orders

- **attempt-review-usage**: `AttemptRecord.review_tier` + `review_usage`; `usage` is worker
  only; `TaskState.usage` still sums both. Owns `models.py`, `router.py`, `tests/test_router.py`.
- **worktree-ensure-excluded**: public `ensure_excluded(repo)`; the manager's private method
  calls it. Owns `worktree.py`, `tests/test_worktree.py`.
- **report-review-tier**: `tier_totals` returns (attempts, reviews, usage); the per-tier
  table gets a `reviews` column. Owns `report.py`, `tests/test_report.py`.
- **plan-stage-n**: `--stage N` (description only for stage 1), `.harness/plans/` paths,
  `ensure_excluded` before planning. Owns `planner.py`, `cli.py` and their two test files.

## Decisions

- Planner files move to `.harness/plans/` (not kept at the root plus an exclude rule): they
  are working files of the harness, and `.harness/` is already excluded for runs.
- `--stage N > 1` takes no description and requires an approved PROJECT_PLAN.md.
- Old state.json files keep their reviews inside `usage` (no migration).

## Prerequisites and risks

- `docs/RESULTS.md` commands use `plans/stage-1/`; the orchestrator updates them after merge.
