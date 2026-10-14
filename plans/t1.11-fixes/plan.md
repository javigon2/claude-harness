# t1.11-fixes: open issues before the MVP run

Fixes the open findings before T1.11: `WorktreeManager.cleanup()` only touches its own run
(O1), and each run's worktrees live in their own directory (O3). Worker and reviewer
sessions pass `skills=[]` plus the two env vars that drop bundled skills and built-in
subagents (G8). `_fail` is typed `NoReturn` (G13). Worker, reviewer and
planner sessions record the SDK init facts (model, tools, skills, mcp_servers,
permissionMode, apiKeySource): as `session_init` events in `events.jsonl` for runs, and on
the terminal plus `.harness/planner/session_init.json` for `harness plan`.

| Wave | Order | Title | Tier | Depends on |
|---|---|---|---|---|
| 1 | worktree-cleanup | cleanup() removes only this run's worktrees | opus | - |
| 1 | sdk-session-init | Record SDK init facts in worker and reviewer results; hide built-ins | opus | - |
| 1 | cli-noreturn | Type _fail as NoReturn and drop the unreachable raises | opus | - |
| 2 | router-session-init | Emit session_init events for worker and reviewer sessions | opus | sdk-session-init |
| 2 | planner-session-init | Show and save the planner session's init facts | opus | sdk-session-init |
| 2 | worktree-per-run | Put each run's worktrees under .harness/worktrees/<run_id>/ | opus | worktree-cleanup |

## Orders

- **worktree-cleanup**: parse `git worktree list --porcelain` records and remove only
  worktrees whose branch is under `refs/heads/harness/<run_id>/`. Owns `worktree.py`,
  `tests/test_worktree.py` (two-run test with r1/r2/r10, detached worktree kept).
  Acceptance: worktree tests, full suite, ruff check + format.
- **sdk-session-init**: `session_init_facts()` + `SESSION_INIT_KEYS` in `worker.py`, a
  `session_init` field on `WorkerResult`/`ReviewResult`, `skills=[]`, two new `WORKER_ENV`
  vars. Owns `worker.py`, `review.py` and their SDK tests.
- **cli-noreturn**: `_fail -> NoReturn`, 6 dead raises deleted (the order said 7). Owns `cli.py`.
- **router-session-init**: `session_init` event before `worker_finished` (role worker) and
  before `verify_finished` (role reviewer). Owns `router.py`, `tests/test_router.py`.
- **planner-session-init**: `format_session_init()`, first init said once and written to
  `<repo>/.harness/planner/session_init.json`. Owns `planner.py`, `tests/test_planner.py`.
- **worktree-per-run** (added after the post-merge rehearsal found O3): `path()` becomes
  `.harness/worktrees/<run_id>/<task_id>`, so a new run can reuse a task id whose old
  worktree an earlier run kept. Owns `worktree.py`, `tests/test_worktree.py`.

## Decisions

- Init facts are events (`session_init`), not state.json fields; fake runs emit none.
- `agents={}` is not set: this SDK never sends an empty dict, so it would do nothing. The
  env var `CLAUDE_AGENT_SDK_DISABLE_BUILTIN_AGENTS=1` does the job instead.
- The new env vars live in `WORKER_ENV`, so the planner gets them too; a no-cost CLI probe
  showed the planner still loads plan-project and plan-stage with them.
- Measured with a no-cost CLI probe: worker/reviewer context had no skill or agent listing
  even before this fix (no Skill/Agent tool), so G8 saved 0 tokens. The fix only tidies
  the init listing and guards the future.

## Prerequisites and risks

- Wave 2 pastes the `session_init` interface from wave 1; run it only after
  sdk-session-init merges.
