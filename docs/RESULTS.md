# T1.11: end-to-end run (MVP)

Status: **not run yet.** The run calls real models (paid), so it waits for the user. This
file holds the setup, the exact commands, the cost estimate and empty tables to fill in.

## Setup

- Toy project: `examples/toy_repo/` (a `todo` CLI: typer, JSON store, `add` and `list`, 4
  tests, ruff clean). It is plain files in this repo; the run uses a fresh copy with its own
  git history, because worktrees need a repo whose `main` has a commit.
- Description: `examples/toy_description.md` (adds `done`, `remove`, priorities,
  `list --pending`, `search`; about 5 tasks).
- Planner: `harness plan` (Opus, plan-project then plan-stage skills from this repo).
- Runner: `harness run` with SdkWorker; workers start on the tier each order names and
  escalate haiku -> sonnet -> opus.

## Commands

Run from the harness repo root (`~/dev/projects/agent-harness`):

```bash
# 1. Fresh copy of the toy repo with one commit on main
rm -rf /tmp/toy_repo && cp -R examples/toy_repo /tmp/toy_repo
cd /tmp/toy_repo && git init -b main && git add -A && git commit -m "toy repo" && cd -

# 2. Plan (interactive: answer the questions, then reply "approve")
uv run harness plan "$(cat examples/toy_description.md)" --repo /tmp/toy_repo

# 3. Check the plan, then copy it here for the record
uv run harness validate /tmp/toy_repo/plans/stage-1/plan.json
cp /tmp/toy_repo/plans/stage-1/plan.json examples/toy_plan.json

# 4. Run it (in a second terminal: uv run harness events /tmp/toy_repo/.harness/runs/<run_id> -f)
uv run harness run /tmp/toy_repo/plans/stage-1/plan.json --repo /tmp/toy_repo --max-parallel 3 --review

# 5. Afterwards (also useful after a Ctrl-C)
uv run harness status --repo /tmp/toy_repo
uv run harness report --repo /tmp/toy_repo
# resume an interrupted run:
uv run harness run /tmp/toy_repo/plans/stage-1/plan.json --repo /tmp/toy_repo --resume --review
```

Optional dry rehearsal first (no model, free): step 4 with `--fake` shows the waves, the
state files and the report; with a real plan its acceptance commands fail, since the fake
worker changes nothing, so expect escalation and `blocked` tasks.

## Cost estimate

Based on T1.5's manual SdkWorker call ($0.08 for a 3-turn opus order) and the Phase-0
worker token counts (15–47k tokens per order):

| Part | Estimate |
|---|---|
| `harness plan` (Opus, ~5–8 turns incl. skills and validation) | $1–3 |
| 5 orders, first attempts (mix of haiku/sonnet) | $0.5–2 |
| Retries and escalations (assume 1–2, one reaching opus) | $0.5–2 |
| `--review` (sonnet, one per passing order) | $0.3–1 |
| **Total** | **about $2–8** |

**Suggested budget cap: $10.** The MVP has no automatic cost gate (that is after the
MVP), so watch `harness events -f` or `harness status` and stop with Ctrl-C if the running
cost in the report passes the cap; `--resume` continues later.

## Results (fill in)

| Order | Tier planned | Attempts | Tiers used | Final | Merged | Tokens in/out | Cost |
|---|---|---|---|---|---|---|---|
| | | | | | | | |

- Planner: rounds __, questions asked __, cost $__
- Run: wall time __, total cost $__, escalations __ of __ tasks
- Done-when: all tasks merged, or blocked ones have clear notes, and the report prints

## What went wrong in the work orders (fill in)

| # | Problem | Order | Kind (order gap / worker / harness) |
|---|---|---|---|
| | | | |

## Things to check during the run

- The planner's init message lists `plan-project` and `plan-stage` as skills (they load
  through `add_dirs` + `setting_sources=["project"]`; `CLAUDE_CODE_DISABLE_CLAUDE_MDS=1`
  keeps `~/dev/CLAUDE.md` out). If the skills are missing, stop and report it.
- `~/dev/work-log/log.md` is unchanged after the run (workers and planner must not load
  user settings).
- The reviewer's `permission_mode="dontAsk"` works (first real reviewer call).
