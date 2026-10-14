# Plan: T1.10 `harness plan`

This stage adds `harness plan "<description>" [--repo PATH]`. `planner.py` runs one
interactive Opus session (`ClaudeSDKClient`) with this repo's `plan-project` and
`plan-stage` skills: replies are printed, the user types answers, and when the planner
rewrites `PROJECT_PLAN.md` with `Status: approved YYYY-MM-DD` the same session runs
`plan-stage` on stage 1 into `plans/stage-1/plan.json`. A plan that `load_plan` rejects
goes back to the planner with the error (at most 2 fixes). `FakePlannerSession` covers the
flow in tests; nothing calls a real model. Two orders in two waves, both on Opus.

## Waves

| Wave | Order | Title | Tier | Depends on |
|---|---|---|---|---|
| 1 | `planner` | Interactive plan-project then plan-stage session | opus | — |
| 2 | `cli-plan` | The `harness plan "<description>"` command | opus | planner |

## Orders

- **`planner`** owns `src/harness/planner.py`, `tests/test_planner.py`. SDK options, session
  algorithm and flow pinned; tests use a fake client and `FakePlannerSession`.
- **`cli-plan`** owns `src/harness/cli.py`, `tests/test_cli_plan.py`; the factory
  `_make_planner_session` is monkeypatched in tests.

## Decisions you may want to overrule

1. Skills load via `add_dirs=[harness repo]` + `setting_sources=["project"]` +
   `skills=["plan-project", "plan-stage"]` (never `"user"`; the SDK would default to
   `["user", "project"]` if setting_sources were left unset).
2. `CLAUDE_CODE_DISABLE_CLAUDE_MDS=1` and `CLAUDE_CODE_DISABLE_AUTO_MEMORY=1`: with
   `"project"`, Claude Code loads CLAUDE.md files from every parent directory (e.g.
   `~/dev/CLAUDE.md`); the env var keeps all of them out.
3. Claude Code's own system prompt (preset) plus a short append, because the skills assume
   Claude Code's tooling; tools Read/Write/Edit/Glob/Grep/Bash + Skill, `acceptEdits`.
4. One session for both phases; approval = `Status: approved YYYY-MM-DD` in a
   PROJECT_PLAN.md the planner rewrote during this run. Stage 1 always goes to
   `plans/stage-1/`.
5. Cost shown = the session's running total from the last ResultMessage.
6. 20 rounds max, `quit`/`exit` or end of input aborts.

## Prerequisites and risks

- Untested against a real model (paid): the T1.11 run is the first real call. Check its
  init message lists both skills. Whether `CLAUDE_CODE_DISABLE_CLAUDE_MDS` also hides skills
  is not documented; if the skills are missing, drop that env var and use the
  `claudeMdExcludes` setting instead.
- `HARNESS_ROOT` is found from the package path, so `harness plan` must run from this
  checkout (`uv run harness ...`), not from an installed wheel.
