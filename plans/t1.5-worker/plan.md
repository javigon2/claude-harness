# Plan: T1.5 Worker

This stage delivers `src/harness/worker.py`. It defines the `Worker` protocol (`async run(order, tier, cwd, retry_feedback) -> WorkerResult`) and a `WorkerResult` with four status values. It adds a parser for the final JSON block, which returns `unknown` when the block is missing or invalid, and a stable system prompt in `src/harness/prompts/worker_system.md` that carries over the implementer rules. A prompt builder orders the user message as root preamble, then order, then retry feedback. `FakeWorker` is scriptable per order and per call, for the T1.7/T1.8 tests. `SdkWorker` runs one Claude Agent SDK `query()` session per attempt, with pinned tools, `acceptEdits`, and user settings, CLAUDE.md and memory turned off. Two orders in two waves, both on Opus for this build. A manual SdkWorker call after the merge closes the stage.

## Waves

| Wave | Order | Title | Tier | Depends on |
|---|---|---|---|---|
| 1 | `worker-core` | Add the Worker protocol, WorkerResult, report parser, prompts and FakeWorker | opus | — |
| 2 | `sdk-worker` | Add SdkWorker on the Claude Agent SDK | opus | worker-core |

## Orders

### `worker-core`
Everything except the SDK: types, `parse_report`, `apply_report`, `load_system_prompt`, `build_prompt`, `FakeWorker`, `WorkerCall`, `FakeWorkerExhausted` and `fake_result`. The interface skeleton and the system-prompt text are pasted in full and are binding.
- **Owns:** `src/harness/worker.py`, `src/harness/prompts/worker_system.md`, `tests/test_worker.py`
- **Acceptance:** `pytest tests/test_worker.py`; `pytest -q`; `ruff check .`; `ruff format --check .`; a sha256 check that the prompt file matches the pasted text byte for byte
- **Budget:** 40 turns, 3 attempts

### `sdk-worker`
Adds `claude-agent-sdk>=0.2.166` with `uv add`, then `WORKER_TOOLS`, `WORKER_ENV`, `usage_from_result` and `SdkWorker` (`options()`, `run()`). Tests inject a fake `query_fn` and never call the real CLI. They cover 13 cases: options, prompt passing, usage mapping, fallbacks, `unknown`, `error` from `is_error`, an exception after the result, no result, and cancellation.
- **Owns:** `src/harness/worker.py`, `tests/test_sdk_worker.py`, `pyproject.toml`, `uv.lock`
- **Acceptance:** `import claude_agent_sdk`; `pytest tests/test_sdk_worker.py`; `pytest tests/test_worker.py`; `pytest -q`; `ruff check .`; `ruff format --check .`
- **Budget:** 40 turns, 3 attempts

## Pinned interface (summary)

- `WorkerStatus = Literal["done", "failed", "unknown", "error"]`. `done` and `failed` are the worker's own claim, `unknown` means no valid JSON block, and `error` means the session failed (SDK exception, `is_error` result, or no result).
- `WorkerResult`: `status`, `summary`, `files_changed`, `notes`, `self_acceptance`, `usage: Usage`, `num_turns`, `duration_ms`, `session_id`, `final_text`, `error`, plus the read-only property `cost_usd` (equal to `usage.cost_usd`).
- `Worker` is a `runtime_checkable` Protocol. `run()` returns failures as `status="error"` and never raises for them; cancellation still propagates.
- `FakeWorker(script: {order_id: [step, ...]}, default=step)`, where a step is a `WorkerResult`, an exception (raised), or a callable `(order, tier, cwd, feedback) -> result` (sync or async). It also has `.add(order_id, *steps)`, `.remaining(order_id)` and `.calls: list[WorkerCall]`. An empty queue with no default raises `FakeWorkerExhausted`.
- `SdkWorker(shared_context="", query_fn=None)`, with `.options(order, tier, cwd)` and `.run(...)`.

## Decisions you may want to overrule

1. **`cost_usd` is a property, not a stored field.** Cost is stored once, in `Usage`, which `TaskState` already aggregates.
2. **The root preamble goes in the user message, before the order.** The system prompt stays byte-identical across tasks so it caches, and the root plus order prefix is also stable across retries of the same task.
3. **Shared context is a run-level `SdkWorker(shared_context=...)` string appended to the system prompt.** The plan schema has no shared-context field, so this is the slot for it.
4. **Tokens come from `model_usage` (summed across models), falling back to `usage`.** Cost comes from `total_cost_usd`. The docs say `usage` covers only the main loop.
5. **Isolation:** `setting_sources=[]`, `strict_mcp_config=True`, `verbatim_prompts=True`, and the env vars `CLAUDE_CODE_DISABLE_AUTO_MEMORY=1` and `CLAUDE_CODE_DISABLE_CLAUDE_MDS=1`. The docs say auto memory and claude.ai connectors are not covered by `setting_sources`.
6. **The parser is lenient.** It takes the last ```` ```json ```` block, ignores text after it, and ignores extra keys. Missing keys or a status other than done/failed give `unknown`.
7. **SDK tests go in a separate `tests/test_sdk_worker.py`.** This keeps `test_worker.py` owned by one order; you named only `test_worker.py`.
8. **The system prompt adds rule 7.** Running out of turns counts as a failure, so the worker is told to keep turns for the final run and the reply. The prompt also says `null` is the exit code for a command that wasn't run.

## Prerequisites and risks

- `uv add` needs network access inside the worktree.
- **`acceptEdits` plus a bare `"Bash"` in `allowed_tools` gives full shell access.** The worktree path in the prompt and `cwd` are the only fences, since the sandbox is out of scope. The verifier's diff check (T1.6) is the backstop.
- **Two things can't be tested offline:** that claude.ai MCP connectors stay out under `strict_mcp_config`, and that `verbatim_prompts` doesn't break a run. The manual call checks both, using the init message's tool list and whether the log is left untouched.
- **Skills for T1.10 (note only):** the docs say `add_dirs=[harness_repo]` plus `setting_sources` including `"project"` loads `<dir>/.claude/skills/`, and `plugins=[{"type": "local", ...}]` is the other route. Either one conflicts with `setting_sources=[]`, so T1.10 needs its own options and must not reuse the worker's.
