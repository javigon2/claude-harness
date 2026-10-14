# T1.11: end-to-end run (MVP)

Status: **done 2026-10-14. The MVP done-when is met:** both orders merged on the first try
and the report printed. Run id `t111` in `/tmp/toy_repo`; the plan is copied to
`examples/toy_plan.json`. Planning cost $1.06 and the run $0.08, $1.14 in all.

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

Run from the harness repo root (`~/dev/projects/agent-harness`). The T1.11 run used
`plans/stage-1/` at the toy root; since H2/H3 were fixed the planner writes to
`.harness/plans/` and any stage can be planned with `--stage N`:

```bash
# 1. Fresh copy of the toy repo with one commit on main
rm -rf /tmp/toy_repo && cp -R examples/toy_repo /tmp/toy_repo
cd /tmp/toy_repo && git init -b main && git add -A && git commit -m "toy repo" && cd -

# 2. Plan (interactive: answer the questions, then reply "approve")
uv run harness plan "$(cat examples/toy_description.md)" --repo /tmp/toy_repo

# 3. Check the plan, then copy it here for the record
uv run harness validate /tmp/toy_repo/.harness/plans/stage-1/plan.json
cp /tmp/toy_repo/.harness/plans/stage-1/plan.json examples/toy_plan.json

# 4. Run it (in a second terminal: uv run harness events /tmp/toy_repo/.harness/runs/<run_id> -f)
uv run harness run /tmp/toy_repo/.harness/plans/stage-1/plan.json --repo /tmp/toy_repo --max-parallel 2 --review

# 5. Afterwards (also useful after a Ctrl-C)
uv run harness status --repo /tmp/toy_repo
uv run harness report --repo /tmp/toy_repo
# resume an interrupted run:
uv run harness run /tmp/toy_repo/.harness/plans/stage-1/plan.json --repo /tmp/toy_repo --resume --review

# 6. Next stage of the approved project plan (no description)
uv run harness plan --stage 2 --repo /tmp/toy_repo
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

## Results

| Order | Tier planned | Attempts | Tiers used | Final | Merged | Worker in/out | Review in/out | Worker cost | Review cost | Total |
|---|---|---|---|---|---|---|---|---|---|---|
| store-done-remove | haiku | 1 | haiku (+ sonnet review) | done | yes, `2c6dbc4` | 3,855 / 3,408 | 5,543 / 491 | $0.0058 | $0.0422 | $0.0480 |
| cli-done-remove | haiku | 1 | haiku (+ sonnet review) | done | yes, `42c7edb` | 4,118 / 3,653 | 5,984 / 362 | $0.0048 | $0.0278 | $0.0326 |

Token counts exclude cached input. Totals per task, cache included: 9,398 / 3,899
in/out, 64,024 cache read, 24,524 cache write (store-done-remove); 10,102 / 4,015,
63,799, 15,695 (cli-done-remove). Worker numbers come from the `worker_finished`
events; review = state total minus worker.

| Tier | Attempts | Cost |
|---|---|---|
| haiku (workers) | 2 | $0.0106 |
| sonnet (reviews) | 2 | $0.0700 |

`harness report` prints a single "haiku 2 attempts $0.0806" row instead, because review
usage is added to the attempt's tier (finding H1).

- Planner: 3 rounds, 0 questions asked (the description was specific enough), cost
  $1.0620. 2 stages in PROJECT_PLAN.md (5 tasks); stage 1 became 2 orders in 2 waves.
- Run: wall time 57 s (21:56:53 to 21:57:50 UTC), total cost $0.0806, escalations 0 of
  2 tasks, failures 0. Each worker took 11 turns in about 20 s.
- After the run, toy main has 15 tests passing and `ruff check` clean. `ruff format
  --check .` fails only on the planner's untracked PROJECT_PLAN.md (H3).
- Isolation and login: every session's `session_init` event (2 workers on
  claude-haiku-5-5, 2 reviews on claude-sonnet-5-5) and the planner's show
  `apiKeySource: none`, which means the subscription login and no API key. The
  planner's skills include plan-project and plan-stage, `mcp_servers` is empty, and
  `~/dev/work-log/log.md` was unchanged after planning and after the run (same md5).
- Done-when: met.

Compared with the estimate ($2–8), the real cost was $1.14: almost all of it was
planning on Opus, and the haiku workers cost about a cent each.

## What went wrong in the work orders

Nothing: both orders passed on the first try, and both reviews passed. Order 1 gave
behaviour and signatures only; order 2 pasted the `done` command and asked for
`remove` in the same shape. So this run exercised neither escalation nor `blocked`,
which only the `--fake` rehearsal and the tests cover (`docs/DRY_RUN.md`).

## Harness findings from the run

| # | Finding | Kind | Blocks done-when? |
|---|---|---|---|
| H1 | Review usage is booked under the worker's tier. `report` shows haiku $0.0806, but $0.0700 of it was the sonnet reviewer, so per-tier cost is wrong whenever `--review` is on. | harness bug (report/router) | no |
| H2 | `harness plan` only plans stage 1 (hard-coded `plans/stage-1/`), so this project's 5 tasks became a 2-order run. Stage 2 (priority, `list --pending`, `search`) has no command. | harness gap | no |
| H3 | The planner leaves `PROJECT_PLAN.md` and `plans/` untracked in the target repo's root. `ruff format --check .` checks the code blocks in PROJECT_PLAN.md and fails, so committing them would break every later order whose acceptance runs it. | harness / planner gap | no |
| H4 | The planner made scratch dirs `/tmp/plancheck` and `/tmp/stage1check` and could not delete them (rm was denied). | planner hygiene | no |

## Things to check during the run

- The planner's init message lists `plan-project` and `plan-stage` as skills (they load
  through `add_dirs` + `setting_sources=["project"]`; `CLAUDE_CODE_DISABLE_CLAUDE_MDS=1`
  keeps `~/dev/CLAUDE.md` out). If the skills are missing, stop and report it.
- `~/dev/work-log/log.md` is unchanged after the run (workers and planner must not load
  user settings).
- The reviewer's `permission_mode="dontAsk"` works (first real reviewer call).
