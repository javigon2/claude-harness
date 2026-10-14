# Run viewer UI, Stage 2: server, page and command

Stage 2 delivers `uv run harness ui --repo <repo>`: a loopback-only, read-only stdlib server
(`/api/runs`, `/api/run/<id>`, an SSE stream of the view) and one static HTML page that follows
a live or finished run. The plan was written before the backend gained `worker_progress`, the
`paused` status and `harness run --max-cost`, so this stage first teaches the Stage 1 view model
about them (`ui-fixtures-paused`, `ui-view-backend`) and then builds server, command and page on
top. Five orders, three waves.

## Waves

| Wave | Order | Title | Tier | Depends on |
|---|---|---|---|---|
| 1 | `ui-fixtures-paused` | Add paused and cost-capped fixture runs | sonnet | - |
| 1 | `ui-server` | HTTP + SSE server (read-only, loopback) | sonnet | - |
| 2 | `ui-view-backend` | View model: paused runs, worker progress, stop reason | sonnet | ui-fixtures-paused |
| 2 | `ui-cli` | `harness ui` command | haiku | ui-server |
| 3 | `ui-page` | The page | sonnet | ui-server, ui-view-backend |

## Orders

**ui-fixtures-paused** (40 turns). Adds `write_paused_run` (rate limit; 21 events incl. 3
`worker_progress`) and `write_capped_run` (cost cap; 14 events) plus three constants, with the
event order the router and scheduler really emit. Owns `tests/ui_fixtures.py`,
`tests/test_ui_fixtures.py`.

**ui-server** (50 turns). `make_handler` / `make_server` as pinned in PROJECT_PLAN, plus a Host
check, a 15 s SSE heartbeat, an `event: problem` message and a `stopping` event so streams end
on shutdown. Owns `src/harness/ui/server.py`, `tests/test_ui_server.py` (loopback `urllib`
tests, SSE second event after `append_event`, read-only snapshot check).

**ui-view-backend** (40 turns). Wording for `worker_progress`, `task_paused`, `run_stopping`,
`budget_exceeded` and `, N paused` on `run_finished`; the feed drops `worker_progress`;
`TaskView.activity` (latest progress of a running task); `RunView.phase` and `stopped_reason`.
Owns `src/harness/ui/view.py`, `tests/test_ui_view.py`, `tests/test_ui_describe.py`.

**ui-cli** (30 turns). `harness ui [--repo] [--run-id] [--port]`, exact messages pinned, tested
with a fake server. Owns `src/harness/cli.py`, `tests/test_cli_ui.py`.

**ui-page** (50 turns). One inline HTML/CSS/JS file with the full behaviour pinned (header,
banner, 7 status classes, paused reason inline, activity cell, newest-first feed, hash and
follow-latest logic, `textContent` only). Tests: no `http(s)://`, no HTML-parsing APIs, a
contract test that every JSON key the page reads exists in the real API payload, `GET /` end
to end, `node --check` on the script when node exists. Owns `src/harness/ui/static/index.html`,
`tests/test_ui_page.py`.

Every order's acceptance ends with `uv run pytest -q`, `uv run ruff check src tests`,
`uv run ruff format --check src tests`.

## Decisions you may want to overrule

- **Feed vs progress.** `worker_progress` never enters the 50-line feed (the filter is applied
  before taking the tail). It shows instead as the running row's `activity` ("Edit: src/b.py").
  Progress is not stored history; only the latest line per running task is kept.
- **Stopped runs.** New `RunView.phase` (`running`/`stopped`/`finished`) and `stopped_reason`;
  a finished run with a paused task or a stop reason reads `stopped`, with a banner. `paused`
  rows show the reason (including the rate-limit reset time) inline, not collapsed. I did not
  add `max_cost_usd`: the cap is already in the reason text.
- **Seven statuses**, not six as PROJECT_PLAN says (`paused` added).
- **Stage grows to 5 orders** (plan said ~4): two orders touch the Stage 1 modules again.
- **Host-header check** (403 unless `127.0.0.1:<port>` or `localhost:<port>`), not in the
  approved plan. It stops DNS-rebinding pages from reading run data. Easy to drop.
- **SSE details:** HTTP/1.0 with one thread per stream, `: keepalive` every 15 s to notice
  closed tabs, `event: problem` when the run files cannot be read.
- **Page behaviour:** feed newest first; follows the latest run until you pick one (then the
  hash pins it); `--run-id` becomes `#<run_id>` in the printed URL.
- **`--port`** is limited to 1-65535 (typer usage error otherwise).
- **Tiers:** `ui-cli` on haiku, the rest on sonnet. Say if you want opus workers as in the
  T1.x runs.
- **Lint scope** is `src tests` (ruff 0.17 reformats PROJECT_PLAN.md's code blocks).

## Prerequisites and risks

- `BACKEND_FILE.md` is already committed on main (in `de65b00`, "initial drfat for UI done")
  and `git status` is clean, so there are no uncommitted edits to `docs/DRY_RUN.md` today. I
  re-check before editing it.
- `docs/DRY_RUN.md` has no section for UI Stage 1; I add Stage 2 and link to the work log
  entry for Stage 1.
- The plan files are not committed. After approval I commit `plans/ui-stage2/` through a
  WorktreeManager task branch (main only gets commits that way), so every order branches from
  a main that holds it.
- Known limit: a run whose process crashed never gets `finished`, so it shows `running`.
- Risk: SSE tests flake on a slow machine. Mitigation: `poll_interval=0.05`,
  `heartbeat_s=0.2`, 5 s timeouts; the design was prototyped in a scratch copy and its tests
  passed.
- Checked in a scratch copy: fixtures and view changes against every pinned count (21 and 14
  events, 18 non-progress, wave 2/3, resumed run clears the reason) and the server design
  against its tests.
- Screenshots: headless Chrome is installed; I'll run the UI against a `--fake` replay in a
  scratch copy and against the paused/capped fixtures, since `--fake` alone only produces the
  blocked path.
