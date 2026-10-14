# Run viewer UI: implementation plan

Status: approved 2026-10-14
Repo: /Users/javi.gonzalezpaterna/dev/projects/agent-harness

## Goal

A very small, read-only web page that shows what the harness's agents are doing. You start
runs in the terminal as always (`harness plan`, `harness run`). `harness ui` serves a local
page that follows the run's `events.jsonl` and `state.json` and lists each work order with
its tier, attempt, status and cost, plus a short feed of what just happened. Nothing else.

### MVP

- `harness ui` serves one page on `127.0.0.1` that works on a live run and a finished one.
- A run picker (default: the latest run) and a header with the stage, wave progress,
  running cost and per-tier totals.
- One row per work order: id, title, tier, attempt n/max, status, tokens, cost. A blocked
  or failed row shows its replan note or error.
- A feed of the last 50 events in plain words.
- The page updates by itself while the run progresses.
- It never writes a file, starts a run, calls a model or opens a non-local connection.

**MVP done when:** `uv run harness run <plan> --repo <copy> --fake` in one terminal and
`uv run harness ui --repo <copy>` in another show the tasks changing live in the browser,
and `uv run pytest -q` passes.

### Out of scope

- Starting runs or entering prompts in the page (the terminal does that).
- The pixel-art office, sprites, agents panel, toasts, replay, search, key-source chips.
- Live "editing file X" bubbles (needs a worker progress callback; later).
- A frontend build toolchain or any new Python dependency.

## Shared context

- **Repo:** existing Python package `harness` (runner, router, scheduler, worker, review,
  planner, report, cli; 304 tests); work merges into `main`
- **Stack:** Python 3.12, uv, typer, pydantic v2, stdlib `http.server`, pytest +
  pytest-asyncio (`asyncio_mode=auto`), ruff (line length 100, rules E,F,I,B,UP)
- **Commands:** install `uv sync`, test `uv run pytest -q`, lint `uv run ruff check .`,
  format `uv run ruff format --check .`, run `uv run harness ui --repo <repo>`
- **Layout:** code in `src/harness/ui/` (`view.py`, `server.py`, `static/index.html`),
  the command in `src/harness/cli.py`, tests in `tests/test_ui_*.py`, test helpers in
  `tests/ui_fixtures.py`
- **Conventions:** every parser and reducer is a pure function with unit tests; docstrings
  state exact behaviour as in the existing modules; no model ids outside `config.py`
- **Principles:** read-only (no writes under `.harness/`, only GET handlers); tests never
  call the SDK, the `claude` CLI or a non-loopback network; the view model is the single
  source of truth and the page only renders it

## Decisions

- **Stack:** stdlib `ThreadingHTTPServer` + Server-Sent Events + one static HTML file
  with vanilla JS — no dependency, no build step (default of the stack question)
- **Scope:** viewer only; prompts stay in the terminal (asked)
- **Update mechanism:** the SSE handler polls `events.jsonl` size and `state.json` mtime
  every 0.5 s and sends a full snapshot on change — simpler than tailing, enough for a
  run of tens of events
- **Fixtures:** built in code by `tests/ui_fixtures.py` with `EventLog` and `save_state`,
  not copied files, because `.harness/` is gitignored
- **Cost cap, replay, live-progress backend stage:** dropped from this MVP
- **Port:** 8765 by default; bound to `127.0.0.1` only
- **Tiers:** haiku for mechanical tasks, sonnet for the rest; the dry-run process may run
  them on opus as before

## Estimate

| Stage | Delivers | Tasks | Orders | Worker time |
|---|---|---|---|---|
| 1 | View model: run listing, snapshot reducer, event wording | 2 | ~3 | ~1.8 h |
| 2 | Server, SSE, page and `harness ui` | 3 | ~4 | ~2.4 h |
| **Total** | | 5 | ~7 | ~4.2 h |

## Stage 1: View model (~1.8 h, ~3 orders)

**Delivers:** `build_view(repo, run_id)` turns a run's files into a `RunView`;
`list_runs(repo)` lists runs; both are tested on in-code fixture runs.

**Done when:** `uv run pytest tests/test_ui_fixtures.py tests/test_ui_view.py -q`,
`uv run ruff check .`, `uv run ruff format --check .`, all exit 0

**Interfaces:**

```python
# tests/ui_fixtures.py
def write_plan(repo: Path) -> Path: ...
def write_blocked_run(repo: Path, run_id: str = "blocked1") -> str: ...
def write_happy_run(repo: Path, run_id: str = "happy1") -> str: ...
def write_running_run(repo: Path, run_id: str = "running1") -> str: ...
def append_event(
    repo: Path, run_id: str, type_: str, task_id: str | None = None, payload: dict | None = None
) -> None: ...

# src/harness/ui/view.py
class TaskView(BaseModel):
    task_id: str
    title: str
    status: TaskStatus
    tier: Tier | None
    attempt: int
    max_attempts: int | None
    escalated: bool
    cost_usd: float
    input_tokens: int
    output_tokens: int
    detail: str | None


class TierView(BaseModel):
    tier: Tier
    attempts: int
    cost_usd: float


class EventView(BaseModel):
    ts: str
    task_id: str | None
    type: str
    text: str


class RunView(BaseModel):
    run_id: str
    stage: str
    finished: bool
    wave: int
    waves_total: int
    cost_usd: float
    tiers: list[TierView]
    tasks: list[TaskView]
    events: list[EventView]


def list_runs(repo: Path) -> list[str]: ...
def build_view(repo: Path, run_id: str, max_events: int = 50) -> RunView: ...
def describe_event(type_: str, task_id: str | None, payload: dict) -> str: ...
```

- All models are `BaseModel` subclasses with `ConfigDict(extra="forbid")`.
- Fixture plan: written to `<repo>/plans/plan.json` via `Plan.model_dump_json`; orders
  `a` "Store" (haiku), `b` "CLI" (haiku, depends on `a`), `c` "Docs" (sonnet, depends on
  `a` and `b`); every order `max_attempts` 3, `max_turns` 10. `plan_path` in the fixture
  states is the relative string `plans/plan.json`.
- Fixture runs (state via `save_state` into `<repo>/.harness/runs/<run_id>/`, events via
  `EventLog.emit` run with `asyncio.run`; payloads use the keys the router and scheduler
  emit today):
  - `blocked`: waves `[[a],[b],[c]]`; `a` fails 3 times (haiku, sonnet, opus), ends
    `blocked` with a replan note and a `task_blocked` event with `attempts`, `tier`,
    `replan_note`; `b` and `c` blocked with `reason` and `dependency`; finished.
  - `happy`: `a` done on attempt 1 and merged; `b` fails attempt 1 on haiku, passes
    attempt 2 on sonnet (one `escalated` event); `c` done; finished; the worker
    `session_init` events carry `apiKeySource: "none"`.
  - `running`: `a` done, `b` status `running` on attempt 1, `c` pending; not finished; no
    `run_finished` event.
- `list_runs`: run ids with a `state.json`, newest first (reverse string order); `[]` if
  `.harness/runs` is missing.
- `build_view`: raises `FileNotFoundError(f"No run state at {path}")` if the run has no
  `state.json`. Title and `max_attempts` come from the plan at `state.plan_path`
  (relative paths resolve against `repo`); if the plan is missing or invalid, title =
  task id and `max_attempts` = None. `attempt` = `TaskState.attempts`. `escalated` = more
  than one distinct tier in `history`. `detail` = `replan_note`, else `error`, else the
  last history `feedback` when status is `failed`, else None. `wave` = number of
  `wave_started` events, `waves_total` = `len(run_started.payload["waves"])` (0 without a
  `run_started`). `cost_usd` = sum of task costs. `tiers` = attempts and cost per tier in
  ladder order, only tiers with attempts, from `history`. `events` = last `max_events`
  events, oldest first; unreadable or partial lines are skipped; no `events.jsonl` gives
  `[]`.
- `describe_event` texts (the `<task>` prefix is `task_id` and the payload key names are
  the ones in the event table of `router.py` and `scheduler.py`):
  `run_started` → `run started: <orders> orders in <n> waves`;
  `wave_started` → `wave <wave> started: <tasks joined by ", ">`;
  `wave_finished` → `wave <wave> finished`;
  `task_started` → `<task> started on <tier>`;
  `task_skipped` → `<task> skipped: <reason>`;
  `attempt_started` → `<task> attempt <attempt> on <tier>`;
  `escalated` → `<task> escalated <from_tier> -> <to_tier>`;
  `session_init` → `<task> <role> session: <model>`;
  `worker_finished` → `<task> worker finished: <status>`;
  `verify_started` → `<task> verifying attempt <attempt>`;
  `verify_finished` → `<task> verification passed` or `<task> verification failed`;
  `task_done` → `<task> done on <tier> after <attempts> attempts`;
  `task_failed` → `<task> failed: <reason>`;
  `task_blocked` → `<task> blocked: <reason>` when `reason` is set, else
  `<task> blocked after <attempts> attempts on <tier>`;
  `merge_conflict` → `<task> merge conflict`;
  `merged` → `<task> merged into main`;
  `run_finished` → `run finished: <done> done, <failed> failed, <blocked> blocked`;
  any other type → the type name. A missing payload key is shown as `?`; never raises.

### T1.1 Build in-code fixture runs
- **Tier:** sonnet
- **Depends:** none
- **Files:** `tests/ui_fixtures.py`, `tests/test_ui_fixtures.py`
- **Do:** implement the five helpers exactly as pinned under Interfaces, using
  `harness.events.EventLog`, `harness.state.save_state` and `RunState`, and the models in
  `harness.models`. Event types and payload keys must match `router.py` and
  `scheduler.py`. `append_event` writes one `Event` line to the run's `events.jsonl`
  synchronously.
- **Done when:** `tests/test_ui_fixtures.py` checks that each helper writes a loadable
  `state.json` (`load_state`), parseable events (`read_events`), the status mix described
  above, and that `append_event` adds exactly one line; `uv run pytest
  tests/test_ui_fixtures.py -q` passes.

### T1.2 Build the run view model
- **Tier:** sonnet
- **Depends:** T1.1
- **Files:** `src/harness/ui/__init__.py`, `src/harness/ui/view.py`, `tests/test_ui_view.py`
- **Do:** implement the models, `list_runs`, `build_view` and `describe_event` as pinned.
  Reuse `harness.report.tier_totals` and `harness.state` helpers where they fit. Never
  write to disk.
- **Done when:** tests on the blocked, happy and running fixtures cover: run order and the
  missing-directory case; task rows, `escalated`, `detail`, `attempt`/`max_attempts`;
  missing plan; wave counts; tier totals; cost; event truncation to `max_events`; a
  corrupt last line; the missing-run `FileNotFoundError`; every `describe_event` text and
  the unknown-type fallback; `uv run pytest tests/test_ui_view.py -q` passes.

## Stage 2: Server, page and command (~2.4 h, ~4 orders)

**Delivers:** `uv run harness ui --repo <repo>` serves the live page at
`http://127.0.0.1:8765/`.

**Done when:** `uv run pytest -q`, `uv run ruff check .`, `uv run ruff format --check .`,
all exit 0, and the manual check in MVP done when works against a `--fake` run.

**Interfaces:**

```python
# src/harness/ui/server.py
def make_handler(
    repo: Path, poll_interval: float = 0.5
) -> type[BaseHTTPRequestHandler]: ...
def make_server(repo: Path, port: int, poll_interval: float = 0.5) -> ThreadingHTTPServer: ...
```

- `make_server` binds `("127.0.0.1", port)`; port 0 picks a free port (used by tests).
- Routes, GET only; any other method returns 405 with a JSON body `{"error": "read-only"}`:
  - `/` → `static/index.html` (`text/html; charset=utf-8`).
  - `/api/runs` → `{"runs": list_runs(repo), "latest": <first or null>}`.
  - `/api/run/<run_id>` → `build_view(...)` as JSON.
  - `/api/stream/<run_id>` → `text/event-stream`; sends `data: <RunView json>\n\n`
    immediately and again whenever `events.jsonl` size or `state.json` mtime changes,
    checked every `poll_interval`; ends when the client disconnects.
  - `<run_id>` must be in `list_runs(repo)`, otherwise 404 with `{"error": "unknown run"}`
    (this also blocks path tricks). Other paths: 404 `{"error": "not found"}`.
- CLI: `harness ui [--repo PATH] [--run-id ID] [--port N]`; `--repo` defaults to `.`,
  `--port` to 8765. Prints `Serving http://127.0.0.1:<port>/ (read-only; Ctrl-C to stop)`
  (with `#<run_id>` appended when `--run-id` is given) and serves until Ctrl-C (exit 0).
  Port in use → `Cannot listen on 127.0.0.1:<port>: <error>` on stderr, exit 1.
- Page: shows the run picker (a `<select>` from `/api/runs`, preselected by the URL hash
  or the latest run), a header (stage, `wave n/m`, cost, tier totals, a `finished` or
  `running` label), the task table, the event feed, and `No runs yet` while the list is
  empty (it re-checks `/api/runs` every 2 s). It subscribes with `EventSource` to
  `/api/stream/<run_id>`, rebuilds the DOM from each snapshot, and sets text with
  `textContent` only (no `innerHTML`), so run data cannot inject markup. Status gets a
  CSS class `status-<status>` (colour per status); a row with `detail` shows it in a
  `<details>` element.

### T2.1 Serve the view model over HTTP and SSE
- **Tier:** sonnet
- **Depends:** none
- **Files:** `src/harness/ui/server.py`, `tests/test_ui_server.py`
- **Do:** implement `make_handler` and `make_server` as pinned, on `ThreadingHTTPServer`.
  `static/index.html` is read from the package directory at request time (404 JSON if
  missing). Silence the default request logging. An unreadable run directory gives 500
  `{"error": "<message>"}`.
- **Done when:** tests start `make_server(tmp_repo, 0, poll_interval=0.05)` in a thread and
  call it with `urllib` on `127.0.0.1` only: `/api/runs`, `/api/run/<id>` for the three
  fixtures, 404s, 405 on POST/PUT/DELETE, an SSE stream whose first event is the snapshot
  and whose second arrives after `append_event`, and no file under `.harness/` changed;
  `uv run pytest tests/test_ui_server.py -q` passes.

### T2.2 Write the page
- **Tier:** sonnet
- **Depends:** none
- **Files:** `src/harness/ui/static/index.html`, `tests/test_ui_page.py`
- **Do:** one self-contained HTML file with inline CSS and vanilla JS implementing the
  Page paragraph above. Plain, light layout; no external URLs, fonts or scripts.
- **Done when:** tests read the file and assert: no `http://` or `https://` string, no
  `innerHTML`, it references `/api/runs` and `/api/stream/`, and it defines a CSS class
  for each of the six statuses; `uv run pytest tests/test_ui_page.py -q` passes.

### T2.3 Add the `harness ui` command
- **Tier:** haiku
- **Depends:** T2.1, T2.2
- **Files:** `src/harness/cli.py`, `tests/test_cli_ui.py`
- **Do:** add the `ui` command as pinned, built on `make_server`. Serve with
  `serve_forever()`; on `KeyboardInterrupt` close the server and exit 0. Use `_fail` for
  the port error.
- **Done when:** tests with `CliRunner` monkeypatch `harness.cli.make_server` to a fake:
  they check the printed line (with and without `--run-id`), that Ctrl-C ends with exit 0,
  and the port-in-use message with exit 1; `uv run pytest -q` and `uv run ruff check .`
  pass.

## Prerequisites and risks

- Work orders run from committed `main`: commit this plan and the fixed skill files first
  (you say when).
- The real run `t111` is live in `/tmp/toy_repo`; the plan's tests never touch it. Use it
  (or a `--fake` run in a scratch copy) for the manual check and screenshots.
- Risk: the UI duplicates payload key names. Mitigation: `describe_event` degrades to `?`
  and the fixtures use the emitted keys, so a renamed key shows up in tests.
- Risk: loopback tests flake on slow machines. Mitigation: `poll_interval=0.05` and
  generous timeouts (5 s) in the SSE test.

## After the MVP

- Worker progress events (`SdkWorker` callback) for "editing X" lines
- Cost cap marker, replay with speed control, `apiKeySource` chip, search across runs
- The pixel-art office view
