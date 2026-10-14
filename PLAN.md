# Agent harness — implementation plan (MVP)

## MVP definition

Opus writes a `plan.json` with about 5 work orders for a toy repo. Running `harness run plan.json` then:

- runs independent tasks in parallel, each in its own git worktree, on the model tier the plan assigned
- checks each task with its acceptance command
- escalates failures from Haiku to Sonnet to Opus
- merges successful work back
- prints a token and cost report per task and per tier

Every state change is written to a JSONL event log, which the future UI will read. The UI itself is out of scope.

**Estimated time to MVP: 4–6 focused days.** Phase 0 takes about 1 day and Phase 1 about 3–4 days. Part-time, that's roughly 1.5–2 calendar weeks.

## Shared context (paste into every work order)

- **Project:** `~/dev/projects/agent-harness/`. Python 3.12, `uv`, `typer` CLI, `pydantic` v2, `pytest`, `ruff`, async with `asyncio`.
- **Agent runtime:** the Python Claude Agent SDK (`claude-agent-sdk`): `query()` with `ClaudeAgentOptions` (model, system prompt, allowed tools, `cwd`, max turns, permission mode). The result message carries usage and cost. **Check these names against the current SDK docs before coding.** They were written from memory.
- **Model IDs:** `claude-haiku-5-5`, `claude-sonnet-5-5`, `claude-opus-5-5`. Keep them in one config file, never hardcoded elsewhere.
- **Principles:**
  - State lives on disk, never in a model's context.
  - The acceptance command decides whether a task is done. The worker's self-report doesn't.
  - Every state change emits an event.
  - All model calls go through one interface, so tests can use a fake worker.

## Phase 0: Claude Code-native prototype (~1 day, no runner code)

This phase checks whether the work-order format is good enough before anything is built on top of it.

### T0.1 Work-order schema
- **Tier:** Opus, written together with the user
- **Files:** `docs/work_order.md`
- **Do:** Define the fields:
  - `id`, `title`, `goal`
  - `tier`
  - `depends_on`
  - `files_owned`: only this task may edit them
  - `files_readonly`
  - `context`: pasted excerpts, interfaces and decisions, so the worker doesn't explore
  - `steps`
  - `constraints`
  - `acceptance`: shell commands
  - `budget`: max turns and max attempts
  - `out_of_scope`

  Include one complete example.
- **Done when:** a fresh Sonnet session, given only the example, can say what to do without asking anything.

### T0.2 Planner skill
- **Tier:** Opus
- **Depends:** T0.1
- **Files:** `.claude/skills/plan-stage/SKILL.md`
- **Do:** The skill tells Opus to break a project stage into work orders following the schema. Each order should take about 30 minutes of work or less. It must not overlap `files_owned` within a parallel wave, and every task needs an acceptance command. Before it outputs, it runs a self-check: "Could Sonnet do this without asking a question? Is the needed context pasted in, not just referenced?" Output is `plan.json` plus a readable `plan.md`.
- **Done when:** running the skill on a sample stage produces a valid plan that a person can read in under 5 minutes.

### T0.3 Subagent definitions
- **Tier:** Sonnet
- **Depends:** T0.1
- **Files:** `.claude/agents/scout.md`, `.claude/agents/implementer.md`, `.claude/agents/reviewer.md`
- **Do:**
  - `scout` (Haiku, read-only tools): returns file excerpts and summaries for a question.
  - `implementer` (Sonnet): executes one work order, edits only `files_owned`, ends with a JSON result block.
  - `reviewer` (Sonnet, read-only): checks a diff against its work order and returns pass/fail with reasons.
- **Done when:** each agent can be invoked in Claude Code and stays inside its tools.

### T0.4 Dry run
- **Tier:** the user, plus Opus
- **Depends:** T0.2, T0.3
- **Do:** Plan a real small stage and run its orders by hand through the subagents. Record each point where a worker had to explore, asked a question, or failed acceptance. Update the schema and the skill to fix those.
- **Done when:** at least 4 of 5 orders pass on the first attempt.

### T0.5 Project planner skill
- **Tier:** Opus
- **Depends:** T0.2
- **Files:** `.claude/skills/plan-project/SKILL.md`
- **Do:** The skill takes a project description from the user and turns it into a high-level implementation plan split into stages, in the style of this file. Opus first asks the user the open questions that change what gets built, then writes `PROJECT_PLAN.md` with the goal, the stages in order, and what each stage delivers. It waits for the user's approval before anything else happens. Each stage must be small and concrete enough for `plan-stage` to turn into work orders.
- **Done when:** given a short project description, the skill asks only questions that matter, produces a plan a person can approve in a few minutes, and `plan-stage` can turn its first stage into work orders without asking anything more.

## Phase 1: Runner MVP (~3–4 days)

### T1.1 Scaffold
- **Tier:** Haiku
- **Files:** `pyproject.toml`, `src/harness/__init__.py`, `src/harness/cli.py`, `src/harness/config.py`, `tests/`
- **Do:** Create the uv project and a typer app with an empty `run`/`status`/`report`/`validate` command group. Put model IDs per tier in `config.py`. Set up ruff and pytest.
- **Done when:** `uv run harness --help` works and `uv run pytest` passes with one smoke test.

### T1.2 Models and plan validation
- **Tier:** Sonnet
- **Depends:** T1.1, T0.1
- **Files:** `src/harness/models.py`, `src/harness/plan.py`, `tests/test_plan.py`
- **Do:** Write pydantic models `WorkOrder`, `Plan`, `TaskState` (pending/running/verifying/done/failed/blocked, attempts, tier_used, usage) and `Event`. `load_plan(path)` should catch:
  - unknown dependencies
  - cycles
  - two tasks in the same wave with overlapping `files_owned`
  - empty `acceptance`

  It should also compute the topological waves.
- **Done when:** tests cover each validation error and the wave computation.

### T1.3 Event log
- **Tier:** Haiku
- **Depends:** T1.2
- **Files:** `src/harness/events.py`, `tests/test_events.py`
- **Do:** Write an append-only JSONL writer (`run_dir/events.jsonl`) with timestamp, run_id, task_id, type and payload. Add `harness events --follow` to tail the log.
- **Done when:** events round-trip in tests, and concurrent writes from async tasks don't interleave lines.

### T1.4 Worktree manager
- **Tier:** Sonnet
- **Depends:** T1.1
- **Files:** `src/harness/worktree.py`, `tests/test_worktree.py`
- **Do:** Write `create(task_id, base)`, which makes branch `harness/<run>/<task>` in `.harness/worktrees/<task>`. Write `merge(task_id)`, which rebases onto the integration branch and fast-forwards; on conflict it aborts, returns `conflict`, and leaves the worktree for inspection. Write `cleanup()`. Use subprocess `git` only, with no extra library.
- **Done when:** tests on a temp repo cover create, commit, merge, the conflict path and cleanup.

### T1.5 Worker
- **Tier:** Sonnet
- **Depends:** T1.2
- **Files:** `src/harness/worker.py`, `src/harness/prompts/worker_system.md`, `tests/test_worker.py`
- **Do:** Define a `Worker` protocol with `async run(order, tier, cwd, retry_feedback) -> WorkerResult` (status, summary, files_changed, usage, cost). Implement `SdkWorker` on top of the Agent SDK and a `FakeWorker` for tests.
  - Build the prompt with the stable system prompt and shared context first, so prompt caching applies. The work order goes next, then any retry feedback.
  - Allowed tools are Read/Edit/Write/Glob/Grep plus Bash, set to edit-accept mode, with `cwd` set to the worktree.
  - Parse the final JSON block. If it's missing, set `status=unknown`.
- **Done when:** `FakeWorker` tests pass, and one manual `SdkWorker` call on a trivial order completes and reports usage.

### T1.6 Verifier
- **Tier:** Sonnet
- **Depends:** T1.2
- **Files:** `src/harness/verify.py`, `tests/test_verify.py`
- **Do:** Run each `acceptance` command in the worktree with a timeout. Capture the exit code and the last 200 lines of output. Then check that the diff only touches `files_owned`, and treat any other change as a failure. An optional `--review` flag calls the Sonnet reviewer, and it only runs if the commands pass.
- **Done when:** tests cover a pass, a failing command, a timeout and an edit outside `files_owned`.

### T1.7 Router and escalation
- **Tier:** Sonnet
- **Depends:** T1.5, T1.6
- **Files:** `src/harness/router.py`, `tests/test_router.py`
- **Do:** The ladder is Haiku, then Sonnet, then Opus, starting at the order's `tier`. Each attempt runs the worker and then the verifier. On failure, retry on the next tier up, passing the verifier output as feedback, within `budget.max_attempts`. If Opus fails, it writes a short "replan note" and the task is marked `blocked`. The MVP doesn't re-plan automatically. Emit an event for every transition.
- **Done when:** `FakeWorker` tests cover pass on the first try, escalation, budget exhaustion and the blocked state.

### T1.8 Scheduler
- **Tier:** Sonnet
- **Depends:** T1.3, T1.4, T1.7
- **Files:** `src/harness/scheduler.py`, `tests/test_scheduler.py`
- **Do:** Run the waves in order, with tasks inside a wave running concurrently through `asyncio.Semaphore(max_parallel)`. Each task gets a worktree, goes through the router, and is merged if it passes. Dependents of a failed or blocked task become `blocked`. Persist `state.json` after every transition. `--resume` skips tasks already marked done.
- **Done when:** a test with `FakeWorker` and a 6-task DAG gives the correct order, parallelism, blocked propagation and resume.

### T1.9 CLI and report
- **Tier:** Haiku
- **Depends:** T1.8
- **Files:** `src/harness/cli.py`, `src/harness/report.py`
- **Do:** Wire up four commands:
  - `validate`
  - `run [--max-parallel N] [--review] [--resume]`
  - `status`: a table of tasks
  - `report`: tokens and cost per task and per tier, attempts, and how often escalation happened
- **Done when:** all commands work against a `FakeWorker` run (add a `--fake` flag).

### T1.10 Plan command
- **Tier:** Sonnet
- **Depends:** T1.9, T0.5
- **Files:** `src/harness/planner.py`, `src/harness/cli.py`, `tests/test_planner.py`
- **Do:** Add `harness plan "<description>"`. It calls Opus with the `plan-project` skill, in an interactive session where Opus's questions appear in the terminal and the user answers them. Once the user approves `PROJECT_PLAN.md`, it calls Opus with `plan-stage` on the first stage. The result is a `plan.json` that `harness validate` accepts and `harness run` can execute. Check how the Agent SDK loads project skills against the current SDK docs.
- **Done when:** one `harness plan` command on a short description ends with a valid `plan.json`, and a `FakeWorker`-style fake planner covers the flow in tests.

### T1.11 End-to-end run
- **Tier:** Sonnet, plus the user
- **Depends:** T1.10, T0.2
- **Files:** `examples/toy_repo/`, `examples/toy_plan.json`, `docs/RESULTS.md`
- **Do:** Build a small toy project, such as a CLI todo app with tests. Use `harness plan` to make a 5-task plan from a short description, run it for real, and record the cost, failures, and what went wrong in the work orders.
- **Done when:** all tasks merge, or the blocked ones have clear notes, and the report prints. **This is the MVP.**

## After the MVP (not planned in detail)

- Haiku "context packer" that fills in work-order `context` automatically
- Automatic re-planning by Opus for blocked tasks
- Budget gates, such as stopping when the run is over $X
- Prompt-caching hit-rate metrics
- The agents-in-a-room UI, fed from `events.jsonl`
