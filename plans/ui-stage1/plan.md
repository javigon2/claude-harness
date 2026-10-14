# Run viewer UI, Stage 1: view model

Stage 1 delivers the read-only view model behind `harness ui`. `tests/ui_fixtures.py`
writes three real fixture runs (blocked, happy, running) into a temp repo with the
harness's own models, `save_state` and `EventLog`. `src/harness/ui/view.py` defines
`RunView`/`TaskView`/`TierView`/`EventView`, plus `describe_event` (one plain-words line per
event, never raises), `list_runs` (newest first) and `build_view`. `build_view` turns
`state.json`, `events.jsonl` and the plan into what the page shows: task rows, wave n/m,
cost, tier totals and the last 50 events. It never writes. Stage 2 (server, page, command)
only has to serialise it.

## Waves

| Wave | Order | Title | Tier | Depends on |
|---|---|---|---|---|
| 1 | `ui-fixtures` | Build in-code fixture runs for the UI tests | sonnet | - |
| 1 | `ui-describe` | Add the view models and describe_event | haiku | - |
| 2 | `ui-view` | Build the run view model: list_runs and build_view | sonnet | ui-fixtures, ui-describe |

## Orders

**ui-fixtures** (sonnet, 40 turns). Implement the five pinned helpers and three constants.
The data is pinned exactly: plan a/b/c, per-attempt tokens and costs, all 38/29/14 events
with payloads in the keys router.py and scheduler.py emit, and the type sequences.
Owns `tests/ui_fixtures.py`, `tests/test_ui_fixtures.py`.
Acceptance: `uv run pytest tests/test_ui_fixtures.py -q`, then the common tail (below).

**ui-describe** (haiku, 30 turns). Create the `harness.ui` package and `view.py` with the four
models (pinned skeleton) and `describe_event`, specified by a text table and 26 exact
examples.
Owns `src/harness/ui/__init__.py`, `src/harness/ui/view.py`, `tests/test_ui_describe.py`.
Acceptance: `uv run pytest tests/test_ui_describe.py -q`, then the common tail.

**ui-view** (sonnet, 40 turns). Add `list_runs`, `_read_events` and `build_view` to `view.py`
(pinned imports and signatures, rules given as behaviour), with tests against the fixtures'
expected values. Those values come from a reference implementation run in scratch.
Owns `src/harness/ui/view.py`, `tests/test_ui_view.py`.
Acceptance: `uv run pytest tests/test_ui_view.py -q`, then the common tail.

Common tail: `uv run pytest -q`, `uv run ruff check src tests`,
`uv run ruff format --check src tests`.

## Decisions you may want to overrule

- **Three orders, not two.** The plan's T1.2 is split into `ui-describe` (models + wording,
  no dependencies, runs alongside the fixtures) and `ui-view`, which keeps each order near
  30 minutes. `describe_event` tests live in a third file, `tests/test_ui_describe.py`.
- **Lint scope is `src tests`, not `.`.** ruff 0.17 formats Python blocks in Markdown, and
  `ruff format --check .` fails today on PROJECT_PLAN.md. No order may touch that file.
- **Pending/blocked rows show the planned tier.** `TaskView.tier` is `tier_used`, else the
  order's tier from the plan, else None.
- **Tier totals come from `report.tier_totals`.** A tier that only has reviews appears with
  `attempts` 0, so the tier costs add up to the run cost. The plan said "only tiers with
  attempts".
- **Resumed runs.** `wave` counts `wave_started` events since the last `run_started`, and
  `waves_total` comes from that last `run_started`.
- **Invalid `state.json`.** The `StateError` propagates (Stage 2 maps it to 500). A missing
  run gives `FileNotFoundError("No run state at <path>")`.
- **`describe_event` edge cases.** A missing or None key shows `?`, a missing `task_id`
  shows `?`, and `verify_finished` without `passed: true` reads "failed". A non-dict payload
  counts as `{}`. "after 1 attempts" stays as pinned.
- **Fixture details.** All fixture attempts have `worker_status` "done". The fixtures have
  no reviews. `session_init` events are emitted in all three runs, not only happy.
  `append_event` writes the file directly; it does not go through `EventLog`.

## Prerequisites and risks

- **Commit first.** Work orders run from committed `main`: commit `plans/ui-stage1/`
  before running. To make the stage's own done-when (`ruff format --check .`) pass, run
  `uv run ruff format PROJECT_PLAN.md` once (it changes two code blocks).
- **The backend session is changing event types now.** It has added `worker_progress`
  (commit bef6ad9) and will add `task_paused`, `run_stopping`, `budget_exceeded` and a
  `paused` status. Stage 1 shows the new events by their type name (the fallback), and
  `TaskView.status` uses `harness.models.TaskStatus`, so `paused` is picked up
  automatically. Wording for these events is left for later. `worker_progress` events can
  crowd the 50-event feed during long attempts.
- **Fixture helpers call `asyncio.run`.** They only work from plain `def` tests. The
  orders say so.
