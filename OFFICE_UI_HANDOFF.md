# Handoff: interactive office UI, stages 2–4

Status: ready for implementation (written 2026-10-16 by Opus 5.5 for a Sonnet 5.5 session)
Repo: /Users/javi.gonzalezpaterna/dev/projects/agent-harness (branch `main`, clean at `0941c1b`)

You are implementing the rest of the interactive "office" page of the harness UI. It runs
on the local web server (`harness ui`, stdlib `ThreadingHTTPServer`, bound to 127.0.0.1
only). Stage 1 (read-only desks and bubbles) is done. You build stages 2, 3 and 4 of
`UI_INTERACTIVE_PLAN.md`, end to end, in the order of the steps below. Each step is small,
ends green and gets its own commit. Don't skip ahead, and don't redesign anything this file
settles. If something here turns out to be wrong when you meet the code, take the smallest
change that keeps the stated behaviour, and note it in the step's commit message and in
the report at the end.

---

## 0. Before you write code

1. Read, in this order:
   - `UI_INTERACTIVE_PLAN.md`: the approved design (goal, the six safety rules, stages,
     risks, decisions 1–10). This handoff turns it into concrete steps. Where they differ,
     this file wins; the plan explains *why*.
   - `src/harness/ui/server.py` (209 lines): today's read-only handler. You extend it and
     don't rewrite it.
   - `src/harness/ui/view.py`: `build_view`, `list_runs`, `IDLE_MINUTES = 10`.
   - `src/harness/ui/static/index.html` (281 lines): the page. Look at `el()`, `desk()`,
     `renderOffice()`, `poll()`, and the CSS for `.desk-*` and `.bubble*`.
   - `src/harness/planner.py`: `run_planner`, `SdkPlannerSession`, `FakePlannerSession`,
     `is_approved`, `_fingerprint`, `PROJECT_PLAN_FILE`, `stage_plan_dir`.
   - `src/harness/cli.py`: `ui_cmd` (line ~205), `plan_cmd` (~256), `run` (~113).
   - `src/harness/worker.py`: `describe_tool` (line 70) and the throttled progress loop in
     `SdkWorker.run` (lines ~356–372). You copy that loop into the planner.
   - `tests/test_ui_server.py`, `tests/test_ui_page.py`, `tests/ui_fixtures.py`: the test
     style you follow (real server on port 0 in a daemon thread, `urllib`).
   - `.claude/skills/plan-project/SKILL.md` lines 130–265: the plan writes `Status: draft`,
     approval is `Status: approved YYYY-MM-DD`, and stages are `## Stage N: <name> (~…)`.
   - `docs/DRY_RUN.md` § "Manual check" (line ~584): how earlier browser checks were done,
     plus the headless Chrome gotcha.
2. Read `~/dev/work-log/log.md` (the **agent-harness** lines). Your global instructions
   require you to keep it current (see §6).
3. Commit this file first: `git add OFFICE_UI_HANDOFF.md && git commit`. Worktrees and any
   later harness run branch from committed `main`.
4. Check the baseline: `uv run pytest -q` (≈489 pass), `uv run ruff check src tests`,
   `uv run ruff format --check src tests`. Lint **`src tests` only**: ruff 0.17 fails
   `format --check .` on Markdown code blocks (known, not yours).

### Ground rules (all steps)

- **Stdlib only** for anything new. No new dependencies: no Markdown library, no web
  framework, no JS build. The page stays one self-contained `index.html` with no external
  URLs (`test_no_external_urls`).
- **No `innerHTML`, `outerHTML`, `insertAdjacentHTML`, `document.write` or `eval(`** in the
  page (`test_no_html_parsing_apis`). All text goes through `textContent` and
  `createElement`, Markdown included.
- **Tests never call a model.** Use the demo planner (step 2.3), `FakePlannerSession` or
  `FakeWorker` only. Never construct an `SdkPlannerSession` that connects.
- **Terminal commands keep working unchanged**: `harness plan`, `harness run`,
  `harness status`, `harness report`. `harness ui --read-only` must behave exactly like
  today's `harness ui`.
- Match the existing style: docstrings that state the contract, pydantic models with
  `extra="forbid"`, short functions, small modules.
- After each step: tests plus both ruff commands green, then commit with an `office:`
  prefix (e.g. `office: markdown block parser`). End every commit message with the
  `Co-Authored-By:` line your session's instructions give you.
- Never push. Never run a real (non-`--fake`) planner or run. The user does that.

---

## 1. Target behaviour (what "done" looks like)

`uv run harness ui --repo <toy copy> --fake` opens `http://127.0.0.1:8765/`. With no model
calls at all:

1. The boss desk ("planner") is **idle**. Clicking him opens a modal with a description
   textarea. Submit.
2. The boss is **busy**. His bubble streams progress (`Read: …`, `Glob: …`) and the modal,
   if open, shows the progress list.
3. He **asks a question**: the bubble says "has a question". Clicking shows his reply,
   rendered as Markdown, and an answer box. Answer it and he is busy again.
4. He writes `PROJECT_PLAN.md` and **flashes** (ready). Clicking shows the plan rendered
   with headings, lists, a table and code, plus two actions: **Continue conversation**
   (a textarea) and **Start implementing**.
5. Continue: busy, then the plan is revised and he flashes again.
6. Start implementing: a confirm dialog (planner cost so far, what will happen). Then the
   boss shows **staging**: the server sets the Status line, the session writes stage 1's
   `plan.json`, and the server validates it and launches `harness run … --fake
   --fake-delay 2` as a detached subprocess. The page switches to the new run by itself.
   The boss shows **supervising**, and the desks fill and update live.
7. A second "Start implementing", or any planner action during the run, gets **409**,
   shown as a visible message in the modal.
8. Run finished: the boss is **idle** again and offers **Plan next stage**. That runs
   stage 2's planner and then a stage-2 run.
9. Ctrl-C on the UI mid-plan, then restart: the boss shows **interrupted**, the draft is
   still viewable, and there are two choices: "Continue from draft" (a fresh session told
   to pick up the draft) or "Start fresh". Ctrl-C mid-run: the run keeps going (it's a
   separate process group). The restarted page picks it up live, and the terminal printed
   the run id plus a `harness status` hint.
10. `curl` POSTs with a foreign `Origin`, no `Origin` or no token get 403.
    `harness ui --read-only` answers 405 to every POST, and the boss can't be clicked.

---

## 2. Architecture you build (settled, don't redesign)

### 2.1 New and changed files

| File | What |
|---|---|
| `src/harness/__main__.py` | **new**, 3 lines: `from harness.cli import app` / `app()`. Lets the server launch `[sys.executable, "-m", "harness", "run", …]` without guessing where the `harness` script lives. |
| `src/harness/ui/markdown.py` | **new**: `parse_markdown(text) -> list[dict]` (block tree, §2.5). |
| `src/harness/planner_log.py` | **new**: `PlannerLog` (sync, thread-safe JSONL appender) + `ObservedSession` (PlannerSession wrapper that emits events and detects "plan ready"). |
| `src/harness/planner.py` | `SdkPlannerSession.__init__` gains `progress: ProgressFn \| None = None` (and `progress_interval_s=2.0`); `send` reports tool uses through it, copied from `SdkWorker`. Nothing else changes; `run_planner` stays as it is. |
| `src/harness/ui/demo.py` | **new**: `DemoPlannerSession` (scripted, delays, progress, writes a real draft plan and a valid stage plan) + `demo_stage_plan(stage) -> dict`. |
| `src/harness/ui/office.py` | **new**: `Office` (state machine, planner thread, answer queue, run launcher, lock file) and `OfficeView` (pydantic), plus `office_view_from_log(repo)` for read-only/restart. |
| `src/harness/ui/server.py` | `make_handler/make_server` gain `office: Office \| None = None`, `token: str \| None = None`; new GET `/api/office`, `/api/plan`; POST routes (§2.4); token/mode injected into `index.html`. With `office=None` POST stays 405 exactly as today. |
| `src/harness/ui/static/index.html` | boss states, modal (`<dialog>`), Markdown renderer from block tree, POST helper, auto-switch to the launched run, office polling. |
| `src/harness/cli.py` | `ui_cmd`: `--read-only`, `--fake`, `--fake-delay` (default 2.0, only with `--fake`), `--max-cost`; builds the `Office`; clean shutdown. `plan_cmd`: wraps its session in `ObservedSession(source="terminal")` so the page can show "busy (terminal)". |
| tests | `test_markdown.py`, `test_planner_log.py`, `test_ui_demo.py`, `test_office.py`, `test_ui_office_server.py`, `test_office_e2e.py`, plus edits to `test_ui_page.py`, `test_planner.py`, `test_cli_ui.py`. |

### 2.2 Planner event log: `.harness/planner/events.jsonl`

Append-only and shared by all sessions (office and terminal). One line per event, using the
existing `harness.models.Event` (`run_id` holds the planner **session id**
`planner-<new_run_id()>`, `task_id` is None). `PlannerLog` is a **sync** appender guarded
by a `threading.Lock`: one `write()` plus `flush()` per line, the same as `EventLog.emit`,
but callable from any thread. Don't reuse the async `EventLog`.

| type | payload | written by |
|---|---|---|
| `planner_started` | `session_id, source ("office"\|"terminal"), pid, stage, description, resumed_from_draft (bool)` | Office / plan_cmd, before the first send |
| `planner_message` | `chars` (length of the user text, not the text) | ObservedSession.send, at start |
| `planner_progress` | `tool, detail` (from `describe_tool`) | the progress callback |
| `planner_reply` | `text, plan_changed (bool), cost_usd` | ObservedSession.send, at end |
| `plan_ready` | `path (repo-relative), cost_usd` | ObservedSession, when the plan changed in a project-phase turn and is still `draft` |
| `planner_done` | `plan_path, cost_usd, rounds` | Office / plan_cmd, after run_planner returns |
| `planner_failed` | `reason, cost_usd` (reason `"interrupted: server stopped"`, `"idle timeout"`, `"aborted by the user"`, or the PlannerError text) | same, on PlannerError or shutdown |
| `run_launched` | `run_id, plan_path, pid, fake (bool)` | Office, after Popen |

A session's events run from its `planner_started` to its `planner_done` or
`planner_failed`. A view reads only the **last** session.

### 2.3 "Plan ready" vs "question" (the heuristic in the plan)

`ObservedSession(inner, repo, log, phase_fn)` implements `PlannerSession`. In `send(text)`
it does: log `planner_message`, take `fp_before = _fingerprint(PROJECT_PLAN)`, await
`inner.send(text)`, take `fp_after`, set `plan_changed = fp_after not in (None, fp_before)`,
then log `planner_reply`. If `plan_changed` and `phase_fn() == "project"` and the plan isn't
approved, it also logs `plan_ready`. It returns the turn unchanged, and `close()`
delegates. `run_planner` stays untouched; it only ever sees a `PlannerSession`.

### 2.4 HTTP API (all under the existing Host check)

GET (allowed in both modes):
- `/api/office` → `OfficeView` JSON (below).
- `/api/plan` → `{"exists": bool, "status": "draft"|"approved"|null, "blocks": [...]}`
  for `.harness/plans/PROJECT_PLAN.md`, with blocks from `parse_markdown`. `status` comes
  from the `Status:` line.

POST (interactive only; JSON body; every safety rule applied **in this order**, each
failing request changing nothing):
1. Host check (existing) → 403 `bad host`
2. route unknown → 404
3. `Origin` must be exactly `http://127.0.0.1:<port>` or `http://localhost:<port>` → else 403 `bad origin` (missing counts as bad)
4. `X-Harness-Token` must match (`secrets.compare_digest`) → else 403 `bad token`
5. `Content-Type` must start with `application/json` → else 415
6. `Content-Length` present and ≤ 65536 → else 413 (missing → 411)
7. body must be a JSON object with exactly the allowed keys and types → else 400
8. Office decides → 200 `{ok: true, office: <OfficeView>}` or 409 `{error: "<human sentence>"}`

| Route | Body | Allowed when | Effect |
|---|---|---|---|
| `/api/planner/start` | `{"description": str}` (1–20,000 chars) **or** `{"from_draft": true}` | idle (or interrupted/failed/timed-out); no live run; no terminal planner busy | new office session (stage 1). With `description` and an existing PROJECT_PLAN.md: first move it to `PROJECT_PLAN.<YYYYmmdd-HHMMSS>.md` ("Start fresh"). With `from_draft`: needs an existing draft; the description becomes the resume text (§3.4.2). |
| `/api/planner/message` | `{"text": str}` (1–20,000) | asking or ready | puts text on the answer queue → busy |
| `/api/planner/implement` | `{}` | asking or ready, and PROJECT_PLAN.md has `Status: draft` | approval → staging → run (§3.3.1) |
| `/api/planner/next-stage` | `{}` | idle, no live run, plan approved, a `## Stage N+1:` heading exists, `stage-(N+1)/plan.json` not yet launched | new session `run_planner(stage=N+1)` → staging → run |

`next-stage` is a fourth endpoint the plan folded into "Plan next stage". It takes no
fields, so the rule "intent, never paths or flags" holds. `OPTIONS` stays 405 in both
modes, so CORS preflight always fails. Read-only mode (`office=None`): every POST answers
405 `{"error": "read-only"}` with `Allow: GET`, exactly as today.

**Token and mode in the page**: `index.html` gets
`<meta name="harness-token" content="">` and `<meta name="harness-mode" content="read-only">`.
The server serves the file with those two `content` values substituted: a
`secrets.token_urlsafe(32)` made once per server, and `interactive` or `read-only`. In
read-only mode the token stays empty. `test_served_at_root` compares the body with the file
byte for byte, so change it to compare after the same substitution.

**`OfficeView`** (pydantic, `extra="forbid"`):
```
mode: "interactive" | "read-only"
state: "idle" | "busy" | "asking" | "ready" | "staging" | "supervising"
       | "interrupted" | "failed" | "terminal"        # terminal = a `harness plan` is busy elsewhere
session_id: str | None
stage: int | None                 # stage the session is planning
progress: list[str]               # last 8 "Tool: detail" lines of the current turn
reply: str | None                 # last planner_reply text (Markdown source)
reply_blocks: list[dict]          # parse_markdown(reply); the page renders these, not `reply`
plan_exists: bool
plan_status: "draft" | "approved" | None
cost_usd: float                   # planner cost of the current/last session
error: str | None                 # failure reason / interruption note / idle-timeout note
run_id: str | None                # run launched by this office (or the live run being supervised)
next_stage: int | None            # set when "Plan next stage" is possible
can_click: bool                   # false in read-only and in "terminal"
```

### 2.5 Markdown block tree (`ui/markdown.py`)

`parse_markdown(text: str) -> list[dict]`. It covers the subset the plan-project skill
writes, with no HTML passthrough ever: raw `<tag>` text stays text.

Blocks:
- `{"type": "heading", "level": 1-6, "inline": [...]}`: ATX `#` headings
- `{"type": "paragraph", "inline": [...]}`: consecutive non-blank lines joined by a space
- `{"type": "list", "ordered": bool, "items": [[block, ...], ...]}`: `-`, `*`, `1.`;
  continuation lines indented ≥2 spaces belong to the item; one level of nesting (an
  indented list inside an item becomes a child list block)
- `{"type": "code", "lang": str, "text": str}`: fenced ```` ``` ```` blocks, text verbatim
- `{"type": "table", "header": [inline, ...], "rows": [[inline, ...], ...]}`: pipe
  tables; the `|---|` separator row is dropped; ragged rows are padded or cut to the
  header width
- `{"type": "quote", "blocks": [...]}`: `>` lines
- `{"type": "hr"}`: `---`, `***` on their own line (not inside a table)

Inline spans are flat (no nesting): `{"t": "text"|"strong"|"em"|"code"|"link", "text": str}`,
and links also carry `"href"`. `**x**` → strong, `*x*`/`_x_` → em, `` `x` `` → code (no
markup inside), `[label](url)` → link. An unclosed marker stays literal text. The page
shows a link as its label followed by ` (url)` in a muted span, and it is never an `<a>`
(no navigation away from a page holding the token).

### 2.6 Office state machine (`ui/office.py`)

One `Office(repo, *, fake, fake_delay, max_cost, session_factory, idle_timeout_s=7200,
launcher=None, clock=time.time)` per server. All state changes happen under one
`threading.Lock`. **Every transition is also logged** (§2.2), so a restart can rebuild the
state from the log.

```
idle ──start──▶ busy ──turn ends, plan changed──▶ ready ──message──▶ busy
                 │                                  │
                 └─turn ends, plan unchanged─▶ asking ─message─▶ busy
ready|asking ──implement──▶ staging (approval turn → stage turn(s) → plan.json valid)
staging ──launch ok──▶ supervising ──run finished / process exited──▶ idle (next_stage set)
any planner state ──PlannerError──▶ failed      (draft kept; click → start again)
any planner state ──shutdown / idle timeout──▶ interrupted (on restart, from the log)
```
In `staging`, a reply without `plan.json` (plan-stage asked something) shows as `asking`
with `phase` still `staging`. Answering goes on through `message`. `plan_ready` is never
logged in staging.

**Planner thread**: `threading.Thread(daemon=False, name="harness-planner")`, whose target
runs `asyncio.run(run_planner(description, repo, observed_session, ask, say, stage=N))`.
- `ask(prompt)`: blocks on `self._answers: queue.Queue[str | None]`, looping
  `get(timeout=1.0)` so it can check `self._stopping` and the idle timeout (the time since
  the last reply is ≥ `idle_timeout_s` → log `planner_failed{"idle timeout"}`, return
  None). `run_planner` then raises `PlannerError("aborted: no answer …")` and closes the
  session in its `finally`.
- `say(text)`: no-op. Replies are already logged by `ObservedSession`, and `say` also gets
  the session-init block, which the page doesn't need.
- On return: log `planner_done`, then launch the run (§3.3.1). On `PlannerError`: log
  `planner_failed{reason}` (unless shutdown or timeout already logged one), and the state
  becomes `failed`.
- `session_factory(repo, progress) -> PlannerSession`: in real mode
  `SdkPlannerSession(repo, progress=progress)`, with `--fake` `DemoPlannerSession(repo,
  progress=progress, delay_s=…)`. The `progress` callback logs `planner_progress`.

**Live-run guard (rule 5)**: `live_run(repo) -> str | None` returns the latest run id
whose `state.json` has `finished == False` and whose `events.jsonl` mtime is younger than
`IDLE_MINUTES` (reuse the constant from `view.py`). A run started in a terminal counts.
While one exists, `start`, `message` and `next-stage` get 409 "a run is active (<id>); wait
for it to finish". The boss shows `supervising` even if the office didn't launch the run.

**Terminal planner**: if the last session in the log has `source == "terminal"`, isn't
ended, and its `pid` is alive (`os.kill(pid, 0)`), the state is `terminal`. The page shows
"busy (terminal)" and nothing in the office can be started.

**Lock file** `.harness/office.lock` (JSON `{pid, port, started}`): written by
`Office.open()`, removed by `Office.close()`. If it exists with a live pid that isn't ours,
interactive `harness ui` exits 1 with: `Another harness ui (pid N, port P) owns this repo;
stop it or use --read-only`. A dead pid is overwritten. Read-only mode never touches it.

**Shutdown** (`Office.close()`, called from `ui_cmd`'s `finally` before
`server.server_close()`): set `_stopping`; if a session is live, log
`planner_failed{"interrupted: server stopped"}`, put `None` on the queue, and join the
thread with a 10 s timeout. Never touch a launched run. If a run is active, print `Run
<id> keeps running; check it with: harness status --repo <repo> --run-id <id>`. Remove the
lock file.

---

## 3. Steps

Each step lists what to build, the tests that prove it, and its commit. Sizes match the
plan (about 11 worker hours in total).

### Stage 2: talk to the boss

#### Step 2.1: Markdown block parser
- Build `src/harness/ui/markdown.py` as in §2.5. Pure functions, no I/O.
- Tests in `tests/test_markdown.py`: one test per block type; inline spans incl. unclosed
  markers; `<script>alert(1)</script>` comes back as plain text inside a paragraph; a fenced
  block containing `# not a heading` and `| not | a table |` stays code; a real
  `PROJECT_PLAN.md`-shaped fixture (copy the skeleton from plan-project SKILL.md lines
  ~155–230) parses with no exception and has ≥1 heading, list, table and code block.
- Commit: `office: stdlib markdown block parser`.

#### Step 2.2: planner progress and planner event log
- `SdkPlannerSession(repo, client_factory=None, harness_root=…, *, progress=None,
  progress_interval_s=2.0)`. In `send`'s `AssistantMessage` branch (top level only), call
  `progress(describe_tool(b.name, b.input, self.repo))` for each `ToolUseBlock`, throttled
  exactly like `SdkWorker.run` (new tool name **or** ≥ interval). Swallow callback
  exceptions. `progress` is an async callable: reuse the `ProgressFn` type from
  worker.py. The Office passes an `async def` that calls the sync `PlannerLog.emit`.
- `src/harness/planner_log.py`: `PLANNER_EVENTS = Path(".harness") / "planner" /
  "events.jsonl"`, `PlannerLog(repo, session_id)` with `emit(type, payload) -> Event`
  (sync, lock, mkdir parents), `read_planner_events(repo) -> list[Event]` (skips bad
  lines), `last_session(events) -> list[Event]`, and `ObservedSession` (§2.3).
- `plan_cmd`: wrap the session in `ObservedSession(..., source="terminal")`. Log
  `planner_started` before `run_planner` and `planner_done`/`planner_failed` after it.
  Terminal output stays byte-identical, and the existing `test_cli_plan.py` must pass
  unchanged.
- Tests: the progress throttle with a fake client factory (copy the shape from
  `test_planner.py`'s fake client; three Read blocks in a row → one report, Read then Edit
  → two); `ObservedSession` logs `plan_ready` only when the file changed during the turn
  (use `FakePlannerSession` steps that write or don't write PROJECT_PLAN.md); events from 4
  threads × 50 emits → 200 valid lines; `harness plan` with a fake session writes a
  `source: terminal` session to the log.
- Commit: `office: planner progress callback and planner event log`.

#### Step 2.3: demo planner and demo stage plan
- `src/harness/ui/demo.py`: `DemoPlannerSession(repo, progress=None, delay_s=1.0)`
  implements `PlannerSession`. It picks the reply from the message content, not from a
  fixed list, so Continue can be pressed any number of times:
  - Message starts like `PROJECT_PROMPT` (contains "Use the plan-project skill") **or**
    "earlier planning session was interrupted" → 3 progress steps (`Glob **/*.py`,
    `Read README.md`, `Read pyproject.toml`) spaced by `delay_s`, then the reply is a short
    Markdown question ("Two questions before I write the plan: …" with a 2-item list). No
    file is written.
  - Next non-approval message while the plan doesn't exist yet → progress
    (`Write .harness/plans/PROJECT_PLAN.md`), then write a draft PROJECT_PLAN.md that passes
    the plan-project validator shape: title, `Status: draft`, a Goal paragraph with
    **bold** and `code`, a bullet list, a pipe table (stage | orders | hours), a fenced code
    block, `## Stage 1: Demo core (~1 h, ~3 orders)` and `## Stage 2: Demo polish (~1 h,
    ~2 orders)`. Reply: "Draft written; review it and approve, or tell me what to change."
  - Any later non-approval message → append a `## Revision N` section echoing the first
    80 chars of the message as a quote, and bump nothing else (the fingerprint changes →
    flash again).
  - Message containing "approved the plan" → reply "Approved. Writing stage 1 next." and
    no file change.
  - Message containing "Now use the plan-stage skill on stage K" → progress, then write
    `stage-K/plan.json` = `json.dumps(demo_stage_plan(K), indent=2)` and a two-line
    `plan.md`. Reply "Stage K: N orders in 2 waves, validated."
  - `usage`: grow `cost_usd` by 0.01 per turn so the page shows a moving cost.
- `demo_stage_plan(stage) -> dict`: a plan `load_plan` accepts (check `models.py` and
  `examples/toy_plan.json` for the exact schema). Stage 1 has 3 orders: wave 1 `demo-a`
  (haiku), `demo-b` (haiku); wave 2 `demo-c` (sonnet, depends on both). Stage 2 has 2
  orders. Each order's `files_owned` is `["demo/<id>.txt"]`, and `acceptance` is
  `["python -c \"print('ok')\""]`. Keep every other field minimal but valid. The fake
  worker changes nothing, so the empty diff passes and the run finishes all `done`.
- Tests: `load_plan` accepts both stage plans; driving `run_planner` with
  `DemoPlannerSession(delay_s=0)` and a scripted `ask` (answer, then approve by writing the
  Status line yourself and sending "The user approved the plan") returns a `PlanResult`;
  `harness run <stage-1 plan> --repo <git-inited tmp repo> --fake` exits 0 with all tasks
  done (use `typer.testing.CliRunner` as `test_cli.py` does; the tmp repo needs `git init`
  plus one commit).
- Commit: `office: scripted demo planner for --fake`.

#### Step 2.4: the Office (planner part) and the read-only office view
- `src/harness/ui/office.py`: `OfficeView`, `Office` with `start(description=None,
  from_draft=False)`, `message(text)`, `view()`, `open()`, `close()`. The planner thread,
  queue, idle timeout, live-run guard, terminal-planner detection and lock file follow
  §2.6. `implement` and `next_stage` raise `OfficeConflict("not yet")` for now, and the
  stage 3 and 4 steps fill them in. Conflicts raise `OfficeConflict(message)`, which the
  server maps to 409.
- `office_view_from_log(repo) -> OfficeView`: the read-only derivation used by read-only
  mode and on restart. It reads the last session in the log: `planner_reply` without a
  later message → asking or ready (ready if that turn was `plan_changed`); ended with
  `planner_failed` whose reason starts with `interrupted` or `idle timeout` → interrupted;
  other `planner_failed` → failed; not ended with a dead pid → interrupted; not ended with
  a live pid of another process → terminal; a live run → supervising. In read-only mode
  `can_click` is false.
- Tests (`tests/test_office.py`, demo session with `delay_s=0`, and a helper
  `wait_for(lambda: office.view().state == "asking", 5)`): idle → start → asking → message
  → ready; `start` while busy → OfficeConflict; `message` while busy → OfficeConflict;
  progress lines appear in `view().progress`; idle timeout with `idle_timeout_s=0.2` → the
  session closes, the state is interrupted, and `error` mentions the timeout; `close()`
  while asking → the thread joins and the log ends with `interrupted: server stopped`; a
  second `Office.open()` on the same repo while the first is open → error; a stale lock
  with a dead pid (use a pid from a finished `subprocess`) → taken over; a fake live run
  (`write_running_run` from `ui_fixtures`) → start raises a conflict that mentions the run.
- Commit: `office: Office state machine and planner thread`.

#### Step 2.5: POST endpoints, safety rules, `harness ui` flags
- `server.py`: §2.4. Keep `_not_get` for read-only. In interactive mode route `do_POST`
  through a `_post()` that applies rules 3–7 in order, then calls the Office. Token and mode
  substitution when serving `/`. Add GET `/api/office` and `/api/plan` (read-only mode uses
  `office_view_from_log`).
- `cli.py` `ui_cmd`: `--read-only`, `--fake`, `--fake-delay` (default 2.0, error unless
  `--fake`), `--max-cost` (>0). Interactive mode: `ensure_excluded(repo)` (as plan_cmd
  does), `check_skills()` unless `--fake`, build `Office`, `office.open()` (lock error →
  `_fail`), make the server with `office` + token, and in `finally` run `office.close()`
  then `server_close()`. The banner text becomes `Serving <url> (interactive; Ctrl-C to
  stop)` or `(read-only; …)`. Keep `test_cli_ui.py` green; where it asserts the old banner,
  run that test with `--read-only`.
- Tests (`tests/test_ui_office_server.py`, real server on port 0 with a demo Office):
  every rule from §2.4 with one request each (bad Origin → 403, no Origin → 403, bad token
  → 403, `text/plain` → 415, 70 KB body → 413, unknown key → 400, non-object JSON → 400,
  `OPTIONS` → 405); a valid start → 200 and then a 409 on a second start; read-only server
  → every POST 405 with `Allow: GET` and **no file written under the repo** (reuse
  `_snapshot` from test_ui_server.py); GET `/` contains the token in interactive mode and
  an empty content attribute in read-only mode; `/api/plan` returns blocks once the demo
  has written the draft.
- Commit: `office: interactive POST endpoints with origin and token checks`.

#### Step 2.6: the page talks to the boss
`index.html`:
- Read the two meta tags. `api(path, body)` = `fetch(path, {method: "POST", headers:
  {"Content-Type": "application/json", "X-Harness-Token": token}, body:
  JSON.stringify(body)})`. The browser adds `Origin` itself on same-origin POSTs. On a
  non-2xx response, show the JSON `error` in the modal's message line (`#office-msg`).
- Poll `/api/office` every 1 s (alongside the existing `/api/runs` poll). Keep the run SSE
  as it is.
- Boss desk: class `desk-boss boss-<state>`. Bubble text per state: busy → last progress
  line or "thinking…", asking → "has a question", ready → "plan ready!", staging →
  "writing work orders", supervising → "supervising <run_id>", interrupted → "interrupted",
  failed → "failed", terminal → "busy (terminal)". Add CSS: `.boss-ready .desk-top {
  animation: flash 1s infinite; }` with `@keyframes flash` (a background toggle) and a
  cursor pointer when `can_click`.
- One `<dialog id="office-modal">` with sections that the code shows and hides:
  - **prompt** (idle): textarea `#desc`, button "Start planning" → `planner/start`.
  - **status** (busy, staging, supervising, terminal): state line, cost, progress list.
    No input.
  - **reply** (asking): reply blocks rendered, textarea `#answer`, button "Send" →
    `planner/message`.
  - **plan** (ready, and also reachable from asking when `plan_exists`): plan blocks from
    `/api/plan`, textarea `#continue` with button "Continue conversation" →
    `planner/message`, button "Start implementing" → `confirm()`, then
    `planner/implement`. Its confirm text gives the cost so far and: "This approves the
    plan, has the planner write stage 1 work orders (one more Opus session turn), then
    starts `harness run`."
  - **interrupted/failed**: the error line, button "Continue from draft" (`{from_draft:
    true}`, only if `plan_exists`), "Start fresh" (shows the prompt section).
  - **next stage** (idle with `next_stage`): button "Plan next stage (N)" →
    `planner/next-stage`, plus "New project" (prompt section).
- `renderBlocks(blocks) -> Node`: one function per block type, built with
  `createElement`/`textContent` only. Tables become `<table>`, code becomes `<pre><code>`.
- Clicking the boss when `can_click` is false does nothing (read-only).
- Update `tests/test_ui_page.py`: assert the new ids and state names are present, the
  meta tags exist, and that the forbidden-API and no-external-URL tests still pass;
  `test_script_parses` (node `--check`) must pass.
- **Stage 2 browser check** (§4). Then commit: `office: boss desk, modal and markdown
  rendering`.

### Stage 3: continue and implement

#### Step 3.1: approval, staging and the run launch
- `Office.implement()`: allowed in asking or ready with a `Status: draft` line, else
  OfficeConflict. Under the lock:
  1. Rewrite the line `Status: draft` → `Status: approved <date.today().isoformat()>`
     (one regex sub on `^Status: draft$`, multiline; anything else → OfficeConflict "plan
     has no `Status: draft` line").
  2. Set phase = staging.
  3. Put `"The user approved the plan in the office page; the Status line is already set.
     Reply with one short line."` on the queue.
  `run_planner` then breaks out of the project loop (the fingerprint changed and the plan
  is approved) and sends `STAGE_PROMPT`. On `PlanResult`, the thread calls
  `_launch(result.plan_path)`.
- `_launch(plan_path)`: `load_plan` again (defence in depth; on error the state is failed
  with the message). Then `rid = new_run_id()`; argv = `[sys.executable, "-m", "harness",
  "run", <plan path relative to repo>, "--repo", str(repo), "--run-id", rid]`, plus
  `["--fake", "--fake-delay", str(fake_delay)]` when fake, plus `["--max-cost",
  str(max_cost)]` when given. `subprocess.Popen(argv, cwd=repo, stdin=DEVNULL,
  stdout/stderr → open(".harness/office/run-<rid>.log", "w"), start_new_session=True)`.
  Log `run_launched`, the state becomes supervising with `run_id = rid`. The arguments are
  built **only** from server-side values. Inject `launcher` (default `subprocess.Popen`)
  so tests can capture argv without starting anything.
- Supervising → idle: in `view()`, when the run's `state.json` has `finished` **or** the
  Popen has exited (`poll() is not None`). If the process exited without `finished`, set
  `error = "run <id> exited with code N; see .harness/office/run-<id>.log"`. Also set
  `next_stage` there (step 4.3 fills in the rule; for now None).
- `message`, `start` and `next-stage` during supervising → 409 via the live-run guard.
  `implement` during supervising or staging → 409 "a run is already active" or "already
  staging".
- Tests: with an injected launcher, argv is exactly as above (fake and real-mode variants,
  `--max-cost` passthrough; no element comes from the request); implement while busy →
  409; implement with no draft → 409; the Status line is rewritten to today's date and
  `is_approved` is true; an end-to-end test with the real launcher in `--fake` mode on a
  git-inited tmp repo (`fake_delay=0`) ends with the run finished, all tasks done, the
  office idle, and `harness status --repo <tmp>` (CliRunner) shows the same run id.
- Commit: `office: start implementing launches harness run`.

#### Step 3.2: continue conversation and the page following the run
- Continue is already `planner/message` in ready. Check that the demo's revision path
  flashes again (`plan_ready` logged twice).
- Page: when `office.run_id` is set and appears in `/api/runs`, set `location.hash` to it
  once (remember the last auto-switched id, so a user who picks another run isn't
  overridden). Supervising shows the run id in the status section. A 409 from a second
  "Start implementing" shows in `#office-msg`.
- `tests/test_office_e2e.py`: the whole stage 3 loop over HTTP against a real interactive
  server with the demo Office (`delay_s=0`, `fake_delay=0`): start → wait asking → message
  → wait ready → message (continue) → wait ready again (two `plan_ready`) → implement →
  wait supervising → a second implement gets 409 → wait idle → `/api/runs` lists the run →
  `/api/run/<id>` has all tasks done. Scrape the token from GET `/` and send the right
  Origin.
- **Stage 3 browser check** (§4). Then commit: `office: continue conversation and follow
  the launched run`.

### Stage 4: restarts and leftovers

#### Step 4.1: restart behaviour
- On `Office.open()`, rebuild from the log via `office_view_from_log`: if the last
  session isn't ended and its pid is dead or ours-from-before, log `planner_failed{reason:
  "interrupted: server restarted"}` once, so the state is interrupted. If a launched run is
  still live (`live_run`), start in supervising with that run id (no Popen handle;
  `finished` decides).
- `start(from_draft=True)`: description = `"An earlier planning session was interrupted.
  The current draft is {project_plan}; read it first and continue planning from it with
  the user. Original description:\n\n{orig}"`, where `orig` is the `description` from the
  previous session's `planner_started`. Log `resumed_from_draft: true`.
- "Start fresh" with an existing PROJECT_PLAN.md → archive it as in §2.4 before the
  session starts. Without this, an old approved plan would make `run_planner` end the
  project phase at once.
- Tests: write a log with an unended office session and a dead pid → after `open()` the
  view is interrupted, `plan_exists`, `/api/plan` still renders; from_draft start →
  the description sent to the demo contains "interrupted" and the old description; fresh
  start archives the old plan; a running fixture run → open() gives supervising; a running
  run that then gets `finished` → idle.
- Commit: `office: interrupted and supervising state survive a restart`.

#### Step 4.2: Ctrl-C cleanup and the idle timeout surfaced
- `ui_cmd` `finally`: `office.close()` (§2.6). If a run is live, print the `harness
  status` hint. The idle timeout is already in the Office; the page shows `error` in the
  interrupted section ("Session closed after 2 h without a message; the draft is kept.").
- Test with `subprocess`: start `python -m harness ui --repo <tmp> --fake --port 0`… The
  CLI doesn't print a chosen port 0 today, so either print the real bound port in the
  banner (do this: the banner uses `server.server_address[1]`) or use a free port from a
  socket. Send SIGINT mid-question, then check the process exits within 15 s, the log ends
  with `interrupted: server stopped`, no `claude` child is left (demo has none; just assert
  the exit), and the lock file is gone.
- Commit: `office: clean shutdown on Ctrl-C`.

#### Step 4.3: Plan next stage
- `next_stage` in the view: once the last launched run is finished and PROJECT_PLAN.md is
  approved, find the stage numbers with `re.findall(r"^## Stage (\d+): ", text, re.M)`.
  N = the number in that run's stage plan path (`stage-N`), and `next_stage` = N+1 if that
  heading exists and no run was launched from `stage-(N+1)/plan.json`, else None.
- `Office.next_stage()`: guards as in §2.4, then a new session with `run_planner("",
  repo, …, stage=N+1)`, phase staging from the start. On `PlanResult` → `_launch`.
- Page: the "Plan next stage (N)" button (step 2.6) now works.
- Tests: e2e over HTTP after the stage-1 run: next-stage → supervising → idle with a
  stage-2 run whose `/api/run/<id>` shows stage 2's orders; after stage 2, `next_stage` is
  None (the demo plan has 2 stages); next-stage during a run → 409.
- **Stage 4 browser check** (§4). Then commit: `office: plan next stage from the boss`.

---

## 4. Browser checks (local web server, after steps 2.6, 3.2 and 4.3)

Set up a scratch toy repo once (use your session's scratchpad directory, not `/tmp`):
```
S=<scratchpad>/office-check && rm -rf "$S" && cp -R examples/toy_repo "$S" \
  && git -C "$S" init -q && git -C "$S" add -A && git -C "$S" commit -qm init
uv run harness ui --repo "$S" --fake --port 8791      # leave it running (run_in_background)
```
1. **API walk** (always possible headless): run a small Python script (in the scratchpad)
   that GETs `/`, scrapes the token, and drives start → message → implement exactly like
   `test_office_e2e.py`, but against the live `--fake --fake-delay 2` server. Print each
   `/api/office` state as it changes. All states from §1 must appear in order.
2. **Screenshots** at each boss state, with headless Chrome:
   `"/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" --headless=new
   --screenshot=<scratchpad>/shots/<state>.png --window-size=1280,900 --timeout=4000
   http://127.0.0.1:8791/`. **Don't** use `--virtual-time-budget` (it hangs on the open SSE
   stream, see DRY_RUN.md). The modal can't be opened headless without clicks, so the
   screenshots cover desks, bubbles and the flash; the modal is covered by the user's
   click-through.
3. **Safety from a terminal**:
   ```
   curl -s -o /dev/null -w '%{http_code}\n' -X POST -H 'Origin: http://evil.example' \
     -H 'Content-Type: application/json' -d '{"description":"x"}' http://127.0.0.1:8791/api/planner/start   # 403
   curl -s -o /dev/null -w '%{http_code}\n' -X POST -H 'Origin: http://127.0.0.1:8791' \
     -H 'Content-Type: application/json' -d '{"description":"x"}' http://127.0.0.1:8791/api/planner/start   # 403 (no token)
   ```
   Then restart with `--read-only`: the same POST with a valid token gets 405.
4. **Stage 4 only**: kill the server (Ctrl-C, then also `kill -9` on a second try)
   while the boss is asking, restart, and confirm `/api/office` says interrupted and
   `/api/plan` still renders. Kill it mid-run, restart, and confirm supervising, then idle
   once the run finishes; `uv run harness status --repo "$S"` shows the same run.
5. Stop the server when done (TaskStop or kill), and delete nothing outside the scratchpad.

Write what you saw (states reached, screenshot paths, curl codes, anything odd) into
`docs/DRY_RUN.md` as a new section `## Office UI stages 2–4`, in the style of the
existing sections: Worked / Found / Decisions. Commit it with the last step.

---

## 5. Pitfalls already known

- Python 3.12 asyncio: `Process.wait()` blocks until every pipe closes. Not your problem
  here (you use `subprocess.Popen` and poll), but don't pipe the run's stdout back into the
  server; send it to the log file.
- `run_planner` treats `quit`/`exit` answers as abort. The page sends user text as is, so
  an answer of "exit" ends the session. That's accepted behaviour; the page shows the
  failed state.
- `asyncio.run` inside the planner thread: create nothing asyncio-bound outside it.
  `PlannerLog` is sync for this reason.
- `ThreadingHTTPServer` request threads are daemons. The planner thread is **not** a
  daemon, so it gets joined in `close()`. Forgetting `close()` makes the CLI hang on exit;
  `test_cli_ui.py` will catch that.
- `FakePlannerSession` raises when its steps run out. That's why the demo is a separate
  class that answers by message content.
- `harness run` stores `plan_path` relative to the repo when the plan is inside it, so pass
  the stage plan path **relative** to the repo; the view then resolves titles and
  `max_attempts`.
- A cost stop or a rate-limit pause makes `harness run` exit 3 with `finished` false. Treat
  a non-zero exit as "run stopped" (`error` set, state idle) and show the resume hint the
  run's log printed; never auto-resume.

---

## 6. When you finish (or stop)

1. All of `uv run pytest -q`, `uv run ruff check src tests` and `uv run ruff format --check
   src tests` pass; report the test count.
2. Update `~/dev/work-log/log.md`. Re-read it first, since the user may have edited it.
   Move the **agent-harness** "Interactive office UI planned …" item out of **Working on**
   into **Fixed** under today's date: one or two lines naming what now works (interactive
   `harness ui`, safety rules, demo `--fake`, restart/next stage), the commit range and the
   test count. Add separate lines for anything substantive you *learned* (a surprise, a
   limit). If you stop early, update the Working-on line instead with what's done and
   what's next.
3. Report back with:
   - the commit list;
   - deviations from this file, and why;
   - the browser-check results;
   - what's still unverified, at least: a **real** (non-fake) planner session from the
     page. The user runs that, and it costs about $1. Real-mode `SdkPlannerSession` progress
     has only been tested with a fake client.
   - the exact click-through the user should do by hand: `uv run harness ui --repo <copy>
     --fake`, then the §1 list.
