# Agent roles + a Researcher that ships reports and downloadable charts

Status: draft (2026-11-24). High-level plan for an implementing agent (Sonnet). Verify every
detail marked **check** before coding against it.

## Goal

1. **Roles.** Every agent in the office has a **role** (what it does) and a **tier** (which
   model runs it). These are two separate choices. Roles: **Orchestrator** (routes),
   **Planner** (the Opus planning desk), **Builder** (coding chats and run workers),
   **Reviewer** (a task desk while it verifies) and the new **Researcher**. Each role has its
   own title and its own small figure on its desk, e.g. `Researcher · sonnet · medium`.
2. **Researcher.** The Orchestrator sends evals, experiments, benchmarks, data analysis and
   "write me a report on X" requests to a Researcher. It can run on haiku, sonnet or opus,
   picked the same way as for any other agent. It always produces:
   - `REPORT.md`, which summarises the findings clearly (question, method, results, caveats,
     recommendation);
   - charts **whenever the data suits one, without the user asking**;
   - the raw data behind every chart, as CSV.
3. **UI.** The reply shows a figure gallery: thumbnails, click to enlarge, a download button
   on each chart, "Download all (.zip)", and "Open report", which opens the report in the
   reader with its figures inline.

## Context (read first)

- `src/harness/planner.py`: `ORCHESTRATOR_PROMPT`, `Routing`, `parse_routing`.
- `src/harness/ui/orchestrator.py`: `_route` (actions start/forward/escalate/switch/retune),
  `_start_target`, `Target = (tier, kind, id)`, `handoff_brief`, `routing_line`,
  `orchestrator_view_from_log`, `agent_efforts`.
- `src/harness/ui/chat.py`: `CHAT_TIERS` (haiku, sonnet only), `CHAT_APPEND`,
  `SdkChatSession.options()`, `ChatManager.start/resume/retune`, `_turn` (it already computes
  the turn's changed `paths` for the commit; reuse them for artifacts), `_build_view`,
  `ChatView`, `ChatEntry`.
- `src/harness/ui/repofile.py`: safe path resolution for `/api/file`. Copy its rules for
  binary assets.
- `src/harness/ui/markdown.py`: block parser. It has no image block yet.
- `src/harness/ui/server.py`: GET routing (`/api/file`, `/api/upload/<id>`).
- `src/harness/ui/static/index.html`:
  - desks: `chatDesk`, `chatDeskLabel`, `updateChatDesk`, `bossDesk`, `orchDesk`,
    `taskDesk`/`updateDesk`;
  - the figure: `.desk .worker::before`, one `clip-path: path(...)` silhouette in a 100×100
    box scaled 0.28 (around line 302), coloured by the state variable `--agent`;
  - the document reader: `loadDoc`.
- Existing reference format: `plans/router-effort/PLAN.md` (how effort was threaded through).

## Stage 1: roles as a concept (no Researcher behaviour yet)

1. **Registry** `src/harness/roles.py`: `Role = Literal["orchestrator", "planner", "builder",
   "reviewer", "researcher"]` and a `ROLES` dict of `RoleSpec(title, figure, chat_append |
   None)`. `figure` is a CSS key, e.g. `builder`. A chat role is `builder` or `researcher`.
2. **Routing:** add `Routing.role: Literal["builder", "researcher"] = "builder"`. A missing or
   invalid value falls back to `builder` and never fails routing, the same pattern as
   `effort`. Add one short rubric line to `ORCHESTRATOR_PROMPT`: research means evals,
   experiments, benchmarks, measuring or comparing things, data analysis, or a findings
   report. Tier rubric for research: haiku to re-plot or tweak an existing report, sonnet for
   most evals, opus to design an experiment or for a heavy multi-source analysis. Add a
   `{role}` placeholder next to `{holder}` for the holder's role.
3. **Opus chats for researchers:** allow `opus` in a chat when the role is `researcher`.
   `CHAT_TIERS` stays as it is for builders, and Opus builders keep going to the planning
   desk. **Check** that every `CHAT_TIERS` use (view building, `_build_view` filter, the JS
   row filter `c.tier !== 'sonnet' && ...`) accepts opus research chats.
4. **Plumbing:** `ChatManager.start(tier, text, *, role=, ...)` stores the role in the
   `chat_started` payload. `_build_view`/`ChatView` expose `role`, defaulting to `builder` for
   old logs. `SdkChatSession` takes the role and uses `ROLES[role].chat_append`.
   `resume`/`retune` keep it.
5. **Role change = new agent:** in `_route`, a follow-up whose routed role differs from the
   holder's is handled like a tier switch: close the holder, start the new role with
   `handoff_brief`. A tier change within the same role keeps today's logic. Log `role` on
   `orchestrator_turn`, and say it in `routing_line` ("→ Researcher (sonnet)").
6. **Tests:** routing parse with/without role; role switch triggers a new agent; old logs
   without role load as builder; an opus researcher chat starts and shows.

## Stage 2: the Researcher's behaviour

1. **Prompt file** `src/harness/prompts/researcher_system.md` (loaded into
   `ROLES["researcher"].chat_append`). It includes the chat rules from `CHAT_APPEND` plus:
   - **Output folder:** `reports/<YYYY-MM-DD>-<slug>/`, holding `REPORT.md`, `figures/*.png`
     (plus `.svg`), `data/*.csv` and `scripts/*.py`, so every chart can be rebuilt.
   - **Report layout:** title, one-paragraph TL;DR with the headline numbers, question,
     method/setup, results (each figure embedded as `![caption](figures/x.png)` with a
     one-sentence takeaway under it), caveats/threats to validity, recommendation/next step.
   - **Charts by default:** make one whenever there are ≥2 numbers worth comparing, a trend
     or a distribution. Choose the form from the question: bar for comparisons, line for
     trends, box or histogram for distributions, scatter for relationships, heatmap for
     matrices. One message per chart, the takeaway as the title, labelled axes with units,
     and a colour-blind-safe palette. Save PNG at 150 dpi plus SVG. Never chart a single
     number.
   - **Tooling:** render with `uv run --with matplotlib --with pandas python
     scripts/<name>.py`, so the target repo needs no new dependency. If `uv` is missing, fall
     back to `python3 -m pip install --user matplotlib`. **Check** that both work in the
     worker env (`WORKER_ENV`).
   - **Reply:** a few lines with the headline findings and the report path. Do not paste the
     whole report.
2. **Artifacts on the reply:** in `ChatManager._turn`, collect the turn's changed `paths`
   that are images (`png/jpg/jpeg/svg/webp/gif`), `REPORT.md`/`*.md` under `reports/`, or
   `.csv`. Add them to the `chat_reply` payload as `artifacts: [{path, kind:
   image|report|data}]`. Fallback when the snapshot was skipped (`before is None`): scan
   `reports/` paths named in the reply text that exist. `ChatEntry.artifacts` carries them to
   the view, and `OrchEntry` gets the same field so the Orchestrator thread shows them too.
3. **Tests:** a fake session that writes `reports/x/REPORT.md` + `figures/a.png` gives a reply
   with both artifacts; non-report edits give none.

## Stage 3: serving and downloading

1. `GET /api/asset?path=<repo-relative>[&download=1]` in a new `src/harness/ui/assets.py`. It
   uses the same path rules as `repofile.py` (inside the repo, no `.git`, `.harness` or
   `.env*`) and an extension allow-list: images + csv + md. It sends the right
   `Content-Type`, a size cap (e.g. 15 MB), and `Content-Disposition: attachment;
   filename=...` when `download=1`. SVG gets `Content-Security-Policy: sandbox` and is only
   ever shown through `<img>`.
2. `GET /api/report-bundle?dir=reports/<name>` streams a zip of that folder (same rules,
   allow-listed files only, total size cap).
3. **Tests:** path traversal, `.harness`, a symlink out of the repo and a bad extension are
   refused; the download header is set; the zip holds the expected names.

## Stage 4: UI

1. **Titles:** every desk label leads with the role title: `Researcher · sonnet · medium`,
   `Builder · haiku · low`, `Planner · opus`, `Orchestrator`, and run task desks show
   `Builder · <tier>`, switching to `Reviewer · <tier>` while `verifying`. The chat panel
   header shows the same title. Free "new chat" desks stay Builder.
2. **Figures:** one `clip-path` silhouette per role in the same 100×100 frame as today. They
   keep the state glow (`--agent`) and the walk-in/typing animations. Use a class
   `fig-<role>` on the desk; the CSS picks the path. Ideas, each readable at 28 px:
   - Orchestrator: headset;
   - Planner: clipboard held up;
   - Builder: hard hat;
   - Reviewer: magnifying glass;
   - Researcher: round glasses + a little bar chart on the desk or in hand.

   Keep each figure a single CSS path (no images or external assets).
3. **Gallery under a reply** (chat panel and Orchestrator thread), when `artifacts` is set:
   - a grid of image thumbnails (`/api/asset?path=`), click to open a lightbox with the full
     image, then Download and "Copy path" buttons;
   - a per-thumbnail ⬇ button (`download=1`);
   - a row of buttons: "Open report" (`loadDoc` on the md), "Download report (.md)",
     "Download all (.zip)" (report-bundle for the report's folder), and CSV links.
   - Build all DOM with `textContent`/attributes, never `innerHTML`, like the rest of the page.
4. **Inline figures in the reader:** add an `image {alt, src}` block to `markdown.py` for a
   line that is just `![alt](path)`. In the reader, resolve `src` relative to the report
   file's folder and render it through `/api/asset`. External URLs are not loaded and stay
   as text.
5. **Check in the real app** (the `run` skill): one eval-style request gets routed to
   Researcher; the desk shows the researcher figure and title; the gallery appears; the
   downloads save files.

## Out of scope

- Interactive (hover) charts; PNG/SVG is enough for now.
- A role picker on the free desks (the router picks; a picker can come later).
- Researchers inside runs/work orders. This plan covers the chat path only.

## Open choices (defaults chosen, change before implementing if you want)

- Reports are **committed** into the target repo under `reports/` (that is how they are kept
  and found later). Alternative: `.harness/reports/`, uncommitted.
- An Opus researcher is a chat (direct agent), not the planning desk.
