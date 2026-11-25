# Router picks thinking effort as well as model

Status: draft (2026-11-23). High-level plan for an implementing agent. Verify every SDK
detail marked **check** before coding against it.

## Goal

The Orchestrator's router decides two things per message: the **model** (haiku, sonnet,
opus, as today) and the **effort** (`low`, `medium`, `high`). Effort is applied:

1. **When an agent starts** (new request, model switch up or down, Opus hand-off): the new
   session is created with that effort.
2. **On a big jump within the same agent** (`low` <-> `high`): the chat agent is restarted on
   its own Claude session (`resume=<session_id>`) with the new effort, so it keeps its full
   history. A one-step change (`medium` <-> either side) does nothing.

The user is told in the thread whenever effort changes, like model switches today.

## Context (read first)

- `src/harness/planner.py`: `ORCHESTRATOR_PROMPT`, `Routing` (`to`, `plan`, `reason`),
  `parse_routing`, `SdkRoutingSession.options()` (the router: Sonnet, one turn, no tools),
  `SdkPlannerSession.options()` (Opus), `PlannerTurn`.
- `src/harness/ui/orchestrator.py`: `Orchestrator._route` (actions `start`, `forward`,
  `escalate`, `switch`), `_start_target`, `routing_line`, `orchestrator_view_from_log` (which
  turn lines are shown), the `Target` tuple `(tier, kind, id)`.
- `src/harness/ui/chat.py`: `ChatManager.start`, `ChatManager.resume` (restarts an ended chat
  on its saved Claude session; refused after 2 days or while open), `_Chat` (`resume_id`),
  `_serve` / `_turn`, `SdkChatSession.options()`, `ChatSessionFactory(repo, tier, progress)`.
- `src/harness/ui/office.py`: `Office.start`, `_OfficeOpusDesk.start` (Opus hand-off).
- `src/harness/ui/timing.py` and the Timing panel in `static/index.html`: per-message breakdown.
- SDK: `ClaudeAgentOptions` has `effort` and `thinking` fields (claude-agent-sdk 0.2.167).
  `ClaudeSDKClient` can `set_model` live but **cannot change effort live**, which is why item 2
  restarts the session. **Check:** the accepted `effort` values, and whether a newer SDK adds
  a live effort setter (if it does, item 2 becomes that call instead of a restart).

## Stage 0: router stays fast (prerequisite)

- Run the router itself at `effort="low"` with thinking off (**check** the `thinking` value
  that disables it). The extra field must not add routing time: today routing is 4.9 s on
  "hello" (1.2 s process start, 3.4 s Sonnet model time).
- Measure before and after with the Timing panel (`routing_api_ms` is logged per turn).
- Out of scope here: a pre-started router process (the 1.2 s start). Separate item.

## Stage 1: effort when an agent starts (item 1)

1. **Routing schema:** `Routing.effort: Literal["low", "medium", "high"] = "medium"`. A
   missing or invalid value falls back to `medium`, never fails routing.
2. **Prompt:** extend `ORCHESTRATOR_PROMPT` with a short rubric: `low` for greetings, quick
   questions and mechanical edits; `medium` for ordinary coding; `high` for debugging,
   cross-cutting changes and design. "When unsure, medium; for a follow-up, keep the current
   effort unless the jump is clear." Add `{effort}` (the holder's current effort) next to
   `{holder}`.
3. **Plumbing:** `ChatSessionFactory` and the session classes take an `effort`;
   `SdkChatSession.options()` and `SdkPlannerSession.options()` pass it to
   `ClaudeAgentOptions(effort=...)`. `ChatManager.start(..., effort=)`,
   `_OfficeOpusDesk.start(..., effort=)` -> `Office.start` -> the Opus session. Demo/fake
   sessions accept and ignore it.
4. **Holder state:** the Orchestrator keeps the holder's effort; `Target` grows an effort,
   or a dict on the Orchestrator keyed by agent id holds it. It's restored from the log after a
   restart (see logging).
5. **Logging:** `effort` in `chat_started`, in each `orchestrator_turn`, and in
   `planner_started` for Opus. This lets the Timing panel and later analysis read it.
6. **UI:** the desk label shows it (`haiku · low`); the Timing panel's Agent column shows
   `haiku/low`. The routing line for a model switch names the effort ("Switched down to
   haiku (low effort): ...").

## Stage 2: retune a live chat agent on a big jump (item 2)

1. **Rule (in `_route`, holder is a chat, same model chosen):** retune only when the effort
   moves two steps (`low` <-> `high`). New action `retune`.
2. **`ChatManager.retune(chat_id, effort, text, images)`:** for a chat that is *waiting* and
   has a saved Claude session id (from its last `chat_reply`): stop its session thread, start
   a new one on the **same chat id** with `resume_id=<session_id>` and the new effort, then
   send `text` as the next message. Log `chat_retuned {"from", "to", "session_id"}`. If there's
   no session id, keep the old effort and log a `chat_note` saying so. Reuse the existing
   resume path where possible; it's close to `resume`, but the chat is still open.
3. **Thread line:** "Effort raised to high: <reason>" or "Effort lowered to low: <reason>",
   shown like `escalate` and `switch` (see `orchestrator_view_from_log`).
4. **Timing:** the restart's process start shows as `startup` on that turn (`connect_ms`
   already covers it). Add the action to the per-message Agent column.
5. **Opus:** effort is set at start only (stage 1). Retuning a live Opus planning session is
   out of scope.

## Tests (each stage ships with its own)

- `parse_routing`: effort parsed; missing or invalid gives `medium`.
- Router options: `effort="low"`, thinking off.
- `ChatManager.start(effort=)` reaches the session factory; `SdkChatSession.options().effort`.
- `_route`: new request, switch, escalate pass the effort; a one-step change forwards with no
  retune; a two-step change calls `retune` and logs `retune` with the right line.
- `retune`: same chat id, new session gets `resume_id` and the new effort, the message is
  delivered, a turn in progress is not retuned (the existing "still working" refusal), no
  session id gives a note and no restart.
- Log view: retune lines shown; desk label and Timing column show the effort.
- Run targeted test files only; the full suite runs from the UI's "Run tests" button.

## Evaluation (after it works)

- Before and after, per message from the Timing data: routing time, model time (`api_ms`),
  response time, cost.
- Offline router check: replay ~20-30 logged messages through the router and compare its
  `(model, effort)` with the cheapest passing setting found by running each message on a
  haiku/sonnet x low/medium/high grid (extends `TIER_EVAL_PLAN.md`). Key number: how often the
  effort is too low (quality loss). Also: wasted time when it's too high, and how often a
  retune happens per conversation (flip-flopping).

## Out of scope

- A pre-started router process; skipping routing for obvious messages; retuning Opus;
  per-message prompt nudges ("think carefully").
