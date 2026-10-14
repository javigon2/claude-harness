# Backend stage: live worker progress, rate-limit pause, cost cap

You are working in `~/dev/projects/agent-harness/`. It is a plan-and-execute runner on the Claude Agent SDK (Python 3.12, uv, typer, pydantic v2, pytest + pytest-asyncio with `asyncio_mode = "auto"`, ruff with line-length 100 and rules E, F, I, B, UP). Main is at `cb4ba98` with 319 tests passing. Three backend features need adding. The design below is decided, so implement it as written. If something in the code contradicts this prompt, stop and tell me instead of improvising.

## Read first (only these)
- `src/harness/models.py`, `worker.py`, `review.py`, `router.py`, `scheduler.py`, `report.py`, `cli.py`
- `tests/test_router.py`, `tests/test_scheduler.py`, `tests/test_sdk_worker.py`, `tests/test_review.py`, `tests/test_report.py`, `tests/test_cli.py`. Copy their helpers and style.
- `~/dev/work-log/log.md`, the agent-harness entries. Read it fresh before every edit; I also edit it by hand.

## Hard rules
- **Tests:** no test may call the real SDK, the `claude` CLI or the network. Use the existing fakes: `scripted(...)` query functions, `FakeWorker`, `FakeReviewer`.
- **Model ids:** they live only in `src/harness/config.py`. Make no paid model calls at all.
- **Dependencies:** none new. Don't edit `pyproject.toml` or `uv.lock`.
- **Git:**
  - Don't push.
  - Don't commit on main directly. Each step goes on its own task branch through `harness.worktree.WorktreeManager` with run id `be1`: `create(task_id)`, work in the returned path, `commit(task_id, msg)`, `merge(task_id)`, `discard(task_id)`. All of them run from the main checkout with `uv run python -c ...`.
  - End every commit message with `Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>`.
- **Leave alone:**
  - `PROJECT_PLAN.md` at the repo root; it belongs to another session.
  - Any uncommitted `docs/` and `plans/` changes in the main checkout.
  - Never edit `docs/` on a task branch.
- **Acceptance for every step:** run these in the worktree, and all must exit 0:
  `uv run pytest -q`, `uv run ruff check src tests`, `uv run ruff format --check src tests`.
- **Behaviour today:** keep it unchanged except where a step says otherwise. Existing tests may only change where a step says so.

## SDK facts (verified on the installed claude-agent-sdk 0.2.167)
- **Tool calls:** `from claude_agent_sdk import ToolUseBlock, RateLimitEvent, RateLimitInfo`. `ToolUseBlock(id: str, name: str, input: dict)` appears in `AssistantMessage.content` when the model calls a tool.
- **Assistant errors:** `AssistantMessage` has `error: Literal["authentication_failed", "billing_error", "rate_limit", "invalid_request", "server_error", "unknown"] | None`.
- **Rate-limit events:** `RateLimitEvent(rate_limit_info: RateLimitInfo, uuid: str, session_id: str)`. `RateLimitInfo` has `status` (`"allowed"`, `"allowed_warning"` or `"rejected"`), `resets_at: int | None` (a unix timestamp), `rate_limit_type` and more; every other field has a default. The CLI emits one whenever the limit status changes.
- **Failed results:** `ResultMessage` has `api_error_status: int | None`, the HTTP status (for example 429) of the failing call when `is_error` is true.

## Step 1 — task `worker-progress`: live progress events

**Goal:** `events.jsonl` gets `worker_progress` events while a worker runs, so a UI can show "editing src/x.py", "running tests" and so on.

**Files:** `src/harness/worker.py`, `src/harness/router.py`, `tests/test_sdk_worker.py`, `tests/test_worker.py`, `tests/test_router.py`.

1. **`worker.py`**:
   - Add `ProgressFn = Callable[[dict[str, Any]], Awaitable[None]]`.
   - Add `PROGRESS_DETAIL_MAX = 80`.
   - Add `def describe_tool(name: str, tool_input: Mapping[str, Any], cwd: Path) -> dict[str, str]`, which returns `{"tool": name, "detail": <detail>}`. The detail depends on the tool:
     - For `Read`, `Edit` and `Write`, it is `tool_input["file_path"]` made relative to `cwd.resolve()` when it lies inside it, otherwise unchanged.
     - For `Glob` and `Grep`, it is `tool_input["pattern"]`.
     - For `Bash`, it is the first line of `tool_input["command"]`, with a leading `cd <anything> && ` removed.
     - For any other tool, or when a key is missing or not a string, it is `""`.
     - The detail is cut to `PROGRESS_DETAIL_MAX` characters, ending with `…` when cut.
     - It never raises.
2. **`Worker` protocol and both workers.** `run` gains a keyword-only `progress: ProgressFn | None = None` after `retry_feedback`. Change the protocol docstring to say the callback may be awaited zero or more times during the attempt.
   - `FakeWorker.run` accepts `progress` and ignores it. Don't change `WorkerCall`.
3. **`SdkWorker`**:
   - `__init__` gains `progress_interval_s: float = 2.0`, stored on `self`.
   - In `run`, for each top-level `AssistantMessage` (`parent_tool_use_id is None`), go through each `ToolUseBlock` in `content`. When `progress` is set, call `await progress(describe_tool(block.name, block.input, cwd))` if either of these is true:
     - the block's tool name differs from the last one reported in this attempt, or
     - at least `progress_interval_s` seconds have passed, by `time.monotonic()`, since the last report.
   - Any `Exception` raised by `progress` is caught and ignored, and the session keeps going. Cancellation still propagates.
4. **`router.py`**, in `Router.route`: pass `progress=report` to `self.worker.run(...)`, where `report` is a local `async def report(p)` that does `await emit("worker_progress", {"attempt": n, "tier": tier, **p})`.
   - Define it inside the loop so it captures this attempt's `n` and `tier`.
5. **Tests**:
   - `describe_tool`, one test per rule: a relative path inside cwd, an absolute path outside cwd kept as is, the `cd … && ` prefix stripped, truncation with `…`, an unknown tool, and a missing key.
   - SdkWorker:
     - With `progress_interval_s=0`, a script with two `ToolUseBlock` messages and a result calls `progress` twice, in order.
     - With `progress_interval_s=3600`, two `Edit` blocks report once, and an `Edit` followed by a `Bash` reports twice.
     - A `progress` that raises still ends with status `"done"`.
   - FakeWorker: accepts `progress=`.
   - Router: a small test worker class whose `run` awaits `progress({"tool": "Edit", "detail": "a.py"})` and then writes `done.txt` produces a `worker_progress` event with payload `{"attempt": 1, "tier": "haiku", "tool": "Edit", "detail": "a.py"}`, between `attempt_started` and `worker_finished`.
   - The existing exact event-list tests must stay unchanged, because FakeWorker never reports.

## Step 2 — task `rate-limit-pause`: stop cleanly on a rate limit

**Goal:** a rate limit stops the run instead of burning attempts. Today an SDK error counts as a failed attempt, so a rate limit escalates haiku, then sonnet, then opus, and ends `blocked`. After this step the task becomes `paused`, no new attempts start, and the user resumes with `--resume` after the reset.

**Files:** `src/harness/models.py`, `src/harness/worker.py`, `src/harness/review.py`, `src/harness/router.py`, `src/harness/scheduler.py`, `src/harness/report.py`, `src/harness/cli.py` and their tests. Split this step into two task branches if it grows past about 8 files: first models, worker and review; then router, scheduler, report and cli.

1. **`models.py`:** add `"paused"` to `TaskStatus`.
2. **`worker.py`:** `WorkerResult` gains `rate_limited: bool = False` and `rate_limit_resets_at: int | None = None`. In `SdkWorker.run`, the session counts as rate-limited if any of these is seen:
   - a `RateLimitEvent` whose `rate_limit_info.status == "rejected"`. Keep its `resets_at`; the last one seen wins.
   - an `AssistantMessage` whose `error == "rate_limit"`.
   - a `ResultMessage` with `is_error` true and `api_error_status == 429`.

   When it is rate-limited, the status stays `"error"` (unchanged), `rate_limited=True`, and `error` starts with `"rate limited"`.

   A non-rejected `RateLimitEvent` (`allowed`, `allowed_warning`) changes nothing.
3. **`review.py`:** `ReviewResult` gains the same two fields, set by the same detection in `SdkReviewer.review`. A rate-limited review keeps verdict `"error"`.
4. **`router.py`**:
   - **Worker rate limit:** right after the worker call, if `result.rate_limited`:
     - add `result.usage` to `state.usage`
     - append no `AttemptRecord`
     - set `state.attempts = n - 1`, so the paused attempt doesn't count
     - set `state.status = "paused"` and `state.error = "rate limited; resets at <iso>"`, where `<iso>` is `datetime.fromtimestamp(resets_at, UTC).isoformat()`, or the word `unknown` when there is no timestamp
     - call `await self._changed(state)`
     - emit `task_paused` with `{"attempt": n, "tier": tier, "reason": state.error, "resets_at": resets_at}`
     - return
     - Do this before verification and before any escalation.
   - **Reviewer rate limit:** if `vr.review is not None and vr.review.rate_limited`, do the same: add the worker and review usage, append no record, attempts n−1, `paused`, the same event, return. Workers and reviewers share one subscription, so the next session would be limited too.
   - **Stop check:** `Router.__init__` gains `should_stop: Callable[[], str | None] | None = None`. At the top of each attempt, before `attempt_started`, if `should_stop` is set and returns a reason:
     - set `state.status = "paused"`, `state.error = reason` and `state.attempts = n - 1`
     - call `await self._changed(state)`
     - emit `task_paused` with `{"attempt": n, "tier": tier, "reason": reason, "resets_at": None}`
     - return
5. **`scheduler.py`**:
   - Add `self._stop_reason: str | None = None` and pass `should_stop=lambda: self._stop_reason` to every `Router`.
   - In `_run_task`, right after acquiring the semaphore and before creating the worktree, if `self._stop_reason` is set, return and leave the task `pending`.
   - After `router.route`, if `ts.status == "paused"` and `self._stop_reason is None`, set `self._stop_reason = ts.error`. The paused task's worktree is kept; `--resume` already discards it.
   - At the end of each wave, if `self._stop_reason` is set, emit `run_stopping` with `{"reason": ...}` and break out of the wave loop. Later waves don't start, and their tasks stay `pending`.
   - `run_finished` gains `"paused": <count>` and `"stopped_reason": self._stop_reason`.
   - The `--resume` path already resets every non-done task; check that a paused task restarts from attempt 1.
6. **`report.py`:**
   - The Totals line becomes `Totals: {n} tasks: {done} done, {failed} failed, {blocked} blocked, {paused} paused, {other} not run; {attempts} attempts; cost {money}`, where `other` now excludes paused tasks.
   - "Needs attention" also lists paused tasks with their `error`.
   - Update the exact-text tests to match.
7. **`cli.py`, `run`:** after the report prints, if any task is `paused`:
   - print `Run stopped: <first paused task's error>. Resume with: harness run <plan> --repo <repo> --resume` (plus `--review` if it was given)
   - exit with `typer.Exit(code=3)`
   - Exit code 1 stays the code for "finished but not all done".
8. **Tests**, all with fakes:
   - The three detection signals for SdkWorker and SdkReviewer. A `RateLimitEvent` is built as `RateLimitEvent(rate_limit_info=RateLimitInfo(status="rejected", resets_at=1760000000), uuid="u", session_id="s")`.
   - A non-rejected event is ignored.
   - Router:
     - A worker rate limit on attempt 1 gives `paused` with attempts 0, no history, no `escalated`, no verification, and one `task_paused` event.
     - A reviewer rate limit gives `paused`.
     - A `should_stop` returning a reason before attempt 2 gives `paused` with attempts 1.
   - Scheduler, using the 6-task DAG style from `tests/test_scheduler.py`: a rate limit in wave 1 means later waves never start (their tasks stay `pending`), `run_finished` has `paused: 1`, and `--resume` then runs the paused and pending tasks.
   - Report: the paused count and the attention line.
   - CLI: exit code 3 and the message, using a FakeWorker made by monkeypatching `harness.cli._make_worker`.

## Step 3 — task `cost-cap`: `harness run --max-cost USD`

**Goal:** stop starting new attempts once the run's estimated cost passes a cap, reusing step 2's pause path.

**Files:** `src/harness/scheduler.py`, `src/harness/cli.py`, `tests/test_scheduler.py`, `tests/test_cli.py`.

1. **Scheduler:** `Scheduler.__init__` gains `max_cost_usd: float | None = None`; it raises `ValueError` if the value is not `None` and `<= 0`.
2. **Stop check:** `should_stop` becomes a method `_should_stop()`, which returns `self._stop_reason` if it is set. Otherwise, if `max_cost_usd` is set and `total = sum(t.usage.cost_usd for t in self.state.tasks.values())` is greater than the cap, it:
   - sets `self._stop_reason = f"estimated cost ${total:.2f} passed the ${cap:.2f} cap"`
   - emits `budget_exceeded` with `{"cost_usd": total, "max_cost_usd": cap}` exactly once
   - returns the reason

   It is checked before every attempt (through the router) and before each task starts (in `_run_task`).

   Attempts already running finish, so the final cost can exceed the cap by what was in flight. Say so in the docstring.
   - The emit is async, but `should_stop` is a sync callable, so make the check sync. Emit `budget_exceeded` from `_run_task` or the router path the first time a reason is set; the simplest way is to queue it with a flag and emit it from the async caller. Keep it deterministic and tested.
3. **Resume:** on `--resume`, done tasks keep their usage, so the cap covers the whole run so far. Keep it that way.
4. **CLI, `run`:** add `--max-cost` (`float | None`, help "Stop starting new attempts once the estimated cost passes this many USD.") and pass it to the Scheduler. A value `<= 0` fails with exit 1 and the message `--max-cost must be greater than 0`. The rest is step 2's paused handling: exit 3 and the resume hint.
5. **Tests:**
   - With FakeWorker results costing $0.40 each and `max_cost_usd=1.0` on a sequential plan (`max_parallel=1`), exactly 3 attempts run. The cost passes the cap after the third attempt, so the fourth is not started.
   - `budget_exceeded` is emitted once.
   - The remaining tasks are `paused` or `pending`, and `run_finished.stopped_reason` holds the cap message.
   - `max_cost_usd=None` never stops a run.
   - The CLI flag works end to end with `--fake`.

## Finish
1. **Main:** after each merge, run the full acceptance on main.
2. **Rehearsal:** at the end, do a free `--fake` rehearsal.
   - Copy `examples/toy_repo` to a temp dir and `git init` it with one commit on main.
   - Write a 3-order plan, and run it with `uv run harness run <plan> --repo <copy> --fake --max-cost 0.001`.
   - Make one order cost something, for example with a monkeypatched fake. If that isn't possible through the CLI, use a short Python script that drives `Scheduler` with a `FakeWorker` that has costs.
   - Check exit code 3, the resume hint, `harness report`, the `task_paused`/`budget_exceeded`/`run_finished` events, and that `--resume` without the cap finishes the run.
3. **Docs:** add a section "Backend stage: progress, rate limits, cost cap" to `docs/DRY_RUN.md` in the main checkout. Leave it uncommitted. It should cover:
   - each step, its merge sha and test count
   - anything in this prompt that turned out wrong
   - the new event types and payloads (`worker_progress`, `task_paused`, `run_stopping`, `budget_exceeded`) and the new `paused` status, because the UI reads them
4. **Work log:** update `~/dev/work-log/log.md` per `~/.claude/CLAUDE.md`. Read it fresh first, write one or two lines per item prefixed with `**agent-harness** —`, and add an entry under today's date in Fixed.
5. **Report back:**
   - what merged (shas), the test count, and the decisions you had to take
   - the exact new event payloads, which I'll pass to the UI chat
   - anything you couldn't finish