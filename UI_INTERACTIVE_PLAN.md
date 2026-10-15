# Interactive office UI: high-level plan

Status: approved 2026-10-15
Repo: /Users/javi.gonzalezpaterna/dev/projects/agent-harness
Builds on: PROJECT_PLAN.md (run viewer MVP, stages 1–2, approved 2026-10-14). That file stays
as it is.

## Goal

Replace the read-only run viewer with an "office" page. The boss (the Opus planner) sits at
his own desk. Each work order in the active run has its own desk with a speech bubble that
shows its live activity. Clicking the boss starts or continues a planner session. When a
plan is ready he flashes. You read the rendered plan and either keep talking to him or
press "Start implementing", which approves the plan, writes the stage's work orders and
starts `harness run`. The workers then fill their desks.

This reverses two items in PROJECT_PLAN.md "Out of scope": prompts in the page, and the
office view. (It is still flat CSS shapes, not pixel art.)

## The read-only principle, and what replaces it

The MVP was designed so the page could never write a file, start a run or call a model.
That made it safe to leave open and trivial to reason about. That principle is now gone
on purpose: one click costs real money (a planner session ran about $1 in T1.11) and
starts processes that edit the repo. The new rule:

> **The page can only do what `harness plan` and `harness run` already do, with fixed
> arguments the server chooses. It sends intent, never paths, commands or flags.**

In practice:
- There are three POST endpoints: `planner/start` (description), `planner/message` (text,
  which also answers a question) and `planner/implement` (no body fields). There is
  no generic "run command" endpoint, and no endpoint takes a file path or a CLI flag.
- Every write the page can cause goes to the same places the terminal writes:
  `.harness/plans/`, `.harness/planner/`, `.harness/runs/`, the worktrees, and merges into
  `main` by the scheduler.
- `harness ui --read-only` keeps today's behaviour exactly: GET only, 405 on everything
  else, and the boss can't be clicked. This option is cheap because today's handler
  already works that way.

**How the POST endpoints stay safe** (all on top of the existing 127.0.0.1 bind and the
existing `Host` check, which already stops DNS rebinding):
1. **Origin check**: a POST must carry `Origin: http://127.0.0.1:<port>` or
   `http://localhost:<port>`. Anything else, or a missing Origin, gets 403.
2. **Per-server token**: at startup the server makes a random token (`secrets`) and puts
   it into `index.html` as it serves the page. Every POST must send it back in an
   `X-Harness-Token` header. A cross-site form can't set custom headers, and can't read
   the page to learn the token.
3. **JSON only**: requests must be `Content-Type: application/json`, with the body capped
   at 64 KB. `OPTIONS` stays 405, so a browser's CORS preflight always fails.
4. **One at a time**: a single server-side `Office` state machine, guarded by one lock.
   `start` while the planner is busy, or `implement` while a run is active, returns 409
   and changes nothing. That covers the "clicking him while busy" case on the server side,
   not just in the page.
5. **No planning during a run** (see Decisions): the planner's Bash and Edit tools work in
   the repo while the scheduler merges into `main`, so the two never overlap. A run started
   from the terminal counts too: the server spots a live run from `state.json` (not
   finished, with events newer than `IDLE_MINUTES`).
6. **Escaping, as today**: the page sets text only through `textContent`, and that now
   covers the rendered Markdown too (see Decisions). The test that forbids `innerHTML`
   stays.

## The ceiling: reused vs new

**Reused unchanged or nearly so**
- `ui/view.py`: `build_view` already gives each running task its `activity` from
  `worker_progress`. Desks and bubbles are a different rendering of the same `RunView`.
  `list_runs`, `describe_event` and the idle note are unchanged.
- `ui/server.py`: the `_Server` stopping event, the `Host` check, SSE with polling and a
  heartbeat, JSON helpers and route parsing. POST handling is added beside them.
- `planner.py`: `run_planner` already has the right shape. Its `ask` callback is
  synchronous and runs through `asyncio.to_thread`, so a server can back it with a
  `queue.Queue` filled by POST. `FakePlannerSession` already scripts turns and file
  writes. `is_approved` and the fingerprint checks already detect "plan changed" and
  "plan approved". One session already runs approval and then stage 1's `plan.json`, so
  "Start implementing" for stage 1 needs no second session.
- `harness run` and `--fake`: launched as a subprocess with fixed arguments. The page then
  follows that run's files as it does now.
- `worker.describe_tool`: produces the bubble text ("Edit: src/x.py",
  "Bash: uv run pytest").

**New**
- **Server-owned planner lifecycle**: an `Office` object owns one planner thread (its own
  asyncio loop running `run_planner`) and a state of `idle → busy → asking | ready →
  busy … → staging → running → idle`. It also handles close on Ctrl-C.
- **Planner event log**: `.harness/planner/events.jsonl` (`planner_started`,
  `planner_progress`, `planner_reply`, `plan_ready`, `planner_done`, `planner_failed`).
  The page's boss state comes from this file, read the same way runs are read.
- **Planner progress**: `SdkPlannerSession.send` gets an optional `progress` callback,
  copied from `SdkWorker` (tool use → `describe_tool`, throttled). Today the planner only
  reports when its turn ends.
- **Question vs. plan ready**: `run_planner` can't tell these apart today, since both just
  call `ask`. The rule: if PROJECT_PLAN.md's fingerprint changed during the turn, the plan
  is ready (flash). Otherwise it's a reply or question (show the text and an answer box).
- **Approval → stage plan → run handoff**: the server writes the Status line, `run_planner`
  continues to the stage prompt, and once `plan.json` is valid the server launches
  `harness run`.
- **Markdown rendering**: a stdlib Python parser for the subset the plan-project skill
  writes, sent to the page as a block tree.
- **A `--fake` that looks alive**: `FakeWorker` gets an optional scripted delay plus
  progress (today it ignores `progress` and returns at once). `harness ui --fake` uses a
  scripted demo planner, so every "done when" below costs nothing.
- **The office page itself**: flat CSS desks, bubbles, the boss's states, a modal for the
  prompt, questions and the plan.

## Stages

Sizes use PROJECT_PLAN.md's units (work orders, worker hours). Each stage ends with
`uv run pytest -q`, `uv run ruff check src tests` and `uv run ruff format --check src tests`
passing, plus a browser check against a scratch copy of a toy repo.

### Stage 1: office view, still read-only (~3 orders, ~2.5 h)
Delivers: the desks layout in place of the table header area (the table and feed stay
below it, collapsible). The boss's desk is shown but not clickable yet. One desk per order
of the selected run, labelled `tier · id`, with a bubble from `TaskView.activity`.
`FakeWorker` gets `delay_s` and scripted progress steps, and `harness run --fake` gets
`--fake-delay S` (default 0, so existing tests are unchanged).

**Done when:** `uv run harness run <toy plan> --repo <copy> --fake --fake-delay 3` in one
terminal and `uv run harness ui --repo <copy>` in another show each wave's desks occupied
in turn. Bubbles change (`Read: …`, `Edit: …`, `Bash: …`), and a desk turns green when its
order is done and red when it's blocked or failed.

### Stage 2: talk to the boss (~5 orders, ~4 h)
Delivers: the `Office` state machine and planner thread, the planner event log and progress
callback, the POST endpoints with all six safety rules, and a question/answer round-trip.
The plan becomes ready → the boss flashes → you click him → PROJECT_PLAN.md opens rendered
as Markdown. `harness ui --fake` swaps in a scripted `FakePlannerSession`: progress → one
question → a draft plan → (on approval) a stage plan the fake run can pass.

**Done when:** with `uv run harness ui --repo <copy> --fake`: clicking the idle boss opens a
prompt. Submitting it makes him busy with streaming progress lines. His question appears,
and answering it in the page makes him busy again. He then flashes until clicked, and the
click shows a formatted plan (headings, lists, a table, code). Clicking him while he's busy
opens only a status view. In a terminal,
`curl -X POST -H 'Origin: http://evil.example' …/api/planner/start` returns 403, and the
same request without the token returns 403.

### Stage 3: continue and implement (~3 orders, ~2.5 h)
Delivers: "Continue conversation" (a message on the same session, busy, flashes again) and
"Start implementing": the Status line is set, the session writes stage 1's `plan.json`,
the plan is validated, and `harness run` is launched as a subprocess (with `--fake` when
the UI runs `--fake`). The page switches to the new run by itself. `implement` during a
run returns 409.

**Done when:** in `--fake` mode the whole loop runs in the browser: prompt → question →
flash → plan → Continue → flash again → Start implementing → boss shows "staging" → desks
fill and update live → run finished, boss idle. A second "Start implementing" during the
run is refused with a visible message. `harness status --repo <copy>` in the terminal shows
the same run.

### Stage 4: restarts and leftovers (~3 orders, ~2 h)
Delivers: the behaviours on restart (see Risks), cleanup on Ctrl-C, an idle timeout for the
planner session, and "Plan next stage" on the boss once a run has finished (a new session,
`run_planner(stage=N)`, as `harness plan --stage N` does).

**Done when:** in `--fake` mode, killing the UI mid-plan and starting it again shows the
boss "interrupted" with the draft still viewable, and starting fresh works. Killing it
mid-run leaves the run going, and the restarted page picks it up live. After a finished
stage-1 run, "Plan next stage" produces a stage-2 run.

**Total:** ~14 orders, ~11 worker hours.

## Main risks

1. **A long-lived SDK session inside the server process.** The `ClaudeSDKClient` spawns a
   `claude` CLI child that lives as long as the session. That can be hours while it waits
   for your answer. If it's not closed, Ctrl-C on `harness ui` leaves the child orphaned.
   *Mitigation:* the planner thread owns its own event loop. Server shutdown sets the stop
   event, puts a sentinel on the answer queue (so `ask` returns None and `run_planner`
   closes the session in its `finally`), and joins the thread with a timeout. An idle
   timeout (decision 7) closes a forgotten session. Tests use only `FakePlannerSession`.
2. **Server restart mid-plan.** The session is in memory, so it's lost, and today nothing
   records the SDK session id. Files survive: PROJECT_PLAN.md, the planner event log.
   *Mitigation (stage 4):* the event log shows the boss "interrupted". The draft stays
   viewable, and the next click starts a fresh session told to continue from the draft on
   disk. Real SDK `resume=<session_id>` is possible but untested on SDK 0.2.167. It's
   worth a free probe later, not something to build on now.
3. **Server restart mid-run.** *Mitigation:* `harness run` runs as a detached subprocess
   (`start_new_session=True`), so it doesn't depend on the page's server. A restarted UI
   finds it through `state.json` and `events.jsonl`, as it does for terminal runs. Ctrl-C
   on the UI doesn't stop the run; it prints the run id and `harness status`.
4. **Concurrency.** Request threads, the planner thread and a run subprocess can all touch
   `.harness/`. *Mitigation:* the `Office` state changes under one lock. The planner and a
   run never overlap (rule 5). Two browser tabs see the same server state, and the second
   one's POST gets 409. A second `harness ui` on the same repo can't detect the first, so
   it relies on a lock file `.harness/office.lock` (pid, removed on exit, ignored if the pid
   is dead).
5. **Plan-ready detection is a heuristic.** A turn that edits the plan without finishing it
   would flash too early. *Mitigation:* the plan-project skill writes the plan once and
   then asks for approval, so the plan only changes at the end of a turn. A flash on a
   half-done edit costs one extra click.
6. **Cost by click.** *Mitigation:* the "Start implementing" dialog shows the number of
   orders, the tiers and the planner's cost so far before it starts. Runs started from the
   page pass `--max-cost` when `harness ui --max-cost` is given.

## Decisions for you (with my recommendation)

1. **Markdown without a dependency.** *Recommend:* a small stdlib Python parser in
   `ui/markdown.py` (headings, paragraphs, lists, fenced code, tables, bold/italic/inline
   code, links shown as text) that returns a JSON block tree. The page builds it with
   `createElement` and `textContent`. It's unit-tested with pytest, needs no `innerHTML`,
   and can't inject markup. Alternatives: a hand-written JS renderer (can't be tested in
   this pytest-only repo) or vendoring `marked` (needs a sanitizer, and breaks the
   no-`innerHTML` rule).
2. **Where the planner session lives.** *Recommend:* in-process, on a dedicated thread with
   its own asyncio loop, driving `run_planner` unchanged apart from callbacks. The
   question round-trip is then just a queue. The alternative, a `harness plan` subprocess
   over stdin/stdout, reuses the CLI as it is, but means parsing terminal text and
   detecting "ready" across a process boundary.
3. **Where `harness run` lives.** *Recommend:* a detached subprocess with fixed arguments
   (`harness run <plan> --repo <repo> [--fake] [--max-cost X]`). It survives UI restarts,
   behaves exactly like the terminal, and keeps the scheduler out of the server's threads.
4. **Who writes the approval.** *Recommend:* the server. It rewrites `Status: draft` to
   `Status: approved <today>` (the exact form `is_approved` checks), then sends a short
   "the user approved the plan" message as the answer. That's deterministic and costs
   nothing, and `run_planner` continues straight to the stage prompt. The alternative is
   sending the word "approve" and trusting the skill to edit the line.
5. **Do the terminal commands keep working unchanged?** *Recommend:* yes. `harness plan`,
   `harness run` and `harness ui --read-only` behave as today. The only additions are the
   optional `progress` and planner-event hooks, which also let the page show a planner
   started in the terminal as "busy (terminal)", view-only.
6. **Interactive by default?** *Recommend:* `harness ui` is interactive, and `--read-only`
   restores the MVP. If you'd rather writes be opt-in (`--interactive`), it's a one-line
   flip.
7. **Planner idle timeout.** *Recommend:* close the session after 2 h without a message,
   and say so in the page. The draft stays on disk.
8. **Which desks to show.** *Recommend:* one desk per order of the active run, not only the
   running ones. Pending orders get an empty desk, running and verifying ones a seated
   worker with a bubble, and done or blocked ones a coloured desk. Desks that vanish when
   an order finishes would hide the results.
9. **Planning during a run.** *Recommend:* not allowed. The boss shows "supervising" while a
   run is active, and "Plan next stage" appears only once it has finished.
10. **The table and feed.** *Recommend:* keep them under the office, collapsible. They cost
    nothing, and they're the detail view when a desk shows red.

## Prerequisites

- Commit this file and the current UI code first. Work orders branch from committed
  `main`.
- Known gap: `ruff format --check .` fails on Markdown code blocks under ruff 0.17, so
  orders lint `src tests` only (as in UI stage 1).
