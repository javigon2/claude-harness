# UI chat layout: sidebar, chat, agent room

Status: draft (2026-10-27). High-level plan; each stage below is meant to go through
`plan-stage` to become work orders.

## Goal

Make the office page look and behave like the Claude chat app:

```
| sidebar 20% | chat 45%                         | agent room 35% |
| logo  [<]   | transcript (scrolls)             | run options    |
| + New chat  |                                  | desks          |
| Search      |                                  | tasks / feed   |
| Projects    |                                  |                |
| Pinned      |                                  |                |
| Today ...   | [+] [ message ...         ] [>]  |                |
```

- **Sidebar (20%)**: logo, collapse button, New chat, search, Projects, Pinned, then
  chats grouped by recency, newest first. Collapsed, the chat and the agent room take
  50% each.
- **Chat (45%)**: the transcript scrolls; the composer is pinned to the bottom.
- **Agent room (35%)**: today's right column (run options, desks, tasks table, feed).
- **The Orchestrator always decides**: the `orch-route` dropdown goes away.

## Today (what this builds on)

- `src/harness/ui/static/index.html`: one self-contained page (no external URLs, a
  test checks this). `#layout` is a 2-column grid (`#left` chats, `#right` office). The
  header has the brand plus the `#run-picker` request dropdown, which `renderRuns()`
  fills from `/api/runs` → `requests` (`src/harness/ui/requests.py`, `RequestEntry`:
  id, title, first_message, started_at, kind, state, cost_usd, runs).
- The left column swaps between five panels (`orch-view`, `past-view`, `convo`,
  `timeline-view`, `chat-view`). There are five composers (`orch-text`, `desc`,
  `answer`, `continue`, `chat-text`), each a textarea + an `.attach` row + buttons
  stacked under it.
- `orchSend()` sends to `/api/orchestrator/message` unless the dropdown says
  "Opus: quick/project plan", which calls `/api/planner/start` directly.
- Only the live Orchestrator thread takes messages; any other request opens read-only
  (`past-view`, "Past request, read only").
- "Project" already means the repo folder (`projects.py`, "Switch project").

## Stage 0: before starting

Commit or stash the current uncommitted work (the tests button, upload changes). A run
on a dirty main loses passing tasks at merge time (seen in run 20261027-000801).

## Stage 1: one composer, Orchestrator always decides (page only)

- One composer layout for all five composers: a rounded box with the textarea on top
  and a bottom row inside the box: **attach (+) bottom left**, **send bottom right**,
  both at the same height. Pending thumbnails sit inside the box, above the textarea.
- The textarea starts at one line, grows with the text up to a cap (about 40% of the
  window height), then scrolls inside. A drag handle on the composer's top edge lets
  the user make it taller or shorter by hand; the chosen height is kept per composer
  in `localStorage` (wrapped in try/catch).
- Enter sends, Shift+Enter adds a new line (as in Claude). The send button is
  disabled while the box is empty or an upload is pending.
- Each chat panel is a column: header, transcript (`overflow: auto`, flex 1),
  composer (flex none). The transcript sticks to the bottom while new replies come in,
  unless the user has scrolled up.
- Remove `#orch-route` and the route branch in `orchSend()`: the Orchestrator
  composer always posts `/api/orchestrator/message`. The server keeps accepting
  `/api/planner/start` (the Opus desk's own "Start planning" composer still uses it,
  with its Quick/Full select).
- Other buttons that sat next to Send ("New request", "Continue conversation",
  "Start implementing", "Close chat") move to a small row above the composer.
- Tests: update `tests/test_ui_page*.py` for the removed ids and new structure; add
  checks that `orch-route` is gone and the attach/send order in each composer.

## Stage 2: three-column layout (page only)

- `#layout` becomes a grid of `#sidebar` 20% / `#left` 45% / `#right` 35%, full
  height under the header; each column scrolls on its own.
- Collapse button in the sidebar header: collapsed, the sidebar shrinks to a narrow
  icon strip (expand, New chat, search) and the grid becomes 50% / 50%. The state is
  kept in `localStorage`.
- The header loses the brand and `#run-picker` (both move to the sidebar). The
  "runs of this request" picker (`#run-sub-picker`), progress bar and phase move to
  the top of the agent room. Plans, Report, Dashboard, Stop run, Run tests stay in
  the header.
- Under 900 px wide: the sidebar becomes an overlay opened from a button, and chat
  and agent room stack.

## Stage 3: chat list in the sidebar (page only)

- Built from the same `requests` list `/api/runs` already returns; selecting an
  entry does what choosing it in `#run-picker` does today (sets the URL hash).
- **New chat** = today's "New request" (`/api/orchestrator/new` + `startNew()`).
- **Search** box filters by title and first message, client-side, as you type.
- Groups: Today, Yesterday, Previous 7 days, Previous 30 days, then by month;
  newest first inside each group, by `started_at`.
- Each row: title (one line, ellipsis), a small state dot (running, needs you, needs
  attention), the selected row highlighted. Runs not tied to a request keep a
  "Runs" group at the bottom.
- Bottom of the sidebar: the repo name and "Switch project" (moved from the header).

## Stage 4: projects, pins, rename (backend + page)

- New `src/harness/ui/sidebar.py`: a small store in `.harness/ui/sidebar.json`
  (git-excluded like the rest of `.harness/`):
  `{"projects": [{"id", "name", "created_at"}], "chats": {<request id>: {"project",
  "pinned", "title", "hidden"}}}`. Read on each call, written atomically.
- Routes (same Origin/token/JSON checks as the other POSTs): `GET /api/sidebar`;
  `POST /api/sidebar/project` {name} create, `/project/rename` {id, name},
  `/project/delete` {id} (its chats go back to the plain list), `/chat` {id, project?,
  pinned?, title?, hidden?}.
- Page: "Projects" section (create, rename, delete; click one to see only its chats);
  a row menu on each chat: Pin, Rename, Move to project, Remove from list (hides it;
  the logs are never deleted).
- To avoid a clash with the repo "project", the code calls these `folders`; the page
  label is "Projects".

## Stage 5 (optional): continue any chat

In Claude every chat can be continued; here only the live thread can. Making an old
chat writable needs the Orchestrator to reopen a thread by id (`POST
/api/orchestrator/resume {thread_id}` restoring its transcript as the session's
context) and the `past-view` read-only mode to go away. Larger and riskier than
stages 1-4; decide after they ship.

## Open questions

1. Projects: chat folders (as planned above), or should a Project in the sidebar be a
   repo folder, so opening it switches the repo?
2. Is stage 5 (writing into old chats) in scope now, or later?
3. "Becomes draggable": planned as a drag handle on the composer's top edge after
   auto-grow reaches its cap. Confirm that is the behaviour you meant.
4. Should the Opus desk's "Start planning" keep its Quick/Full select? Planned: yes.

## Verify

After each stage: the stage's tests, `ruff check src tests`, then a click-through in
headless Chrome (the Browser pane refuses localhost) at 1440 px and 800 px wide.
