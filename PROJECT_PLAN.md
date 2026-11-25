# Planning speed and cost: implementation plan

Status: draft
Repo: /Users/javi.gonzalezpaterna/dev/projects/agent-harness

## Goal

Cut the time and cost of planning. One Opus session writes the project plan, stage work orders
and quick plans (8-9 min, then 3-17 min and $1.3-4.2 more for work orders, per
`docs/SPEED_REPORT.md`). This project (1) reports where planning time goes, (2) moves quick
plans and stage orders to Sonnet behind a mechanical gate with fallback to Opus, and (3) gives
those cold Sonnet sessions a cached repo digest.

### MVP

- `harness planning-report` prints planner timings and cost per session, phase and tool.
- Sonnet writes quick plans and stage orders (the project plan stays on Opus), redone on Opus when a check or the session fails; order prompts carry a cached repo digest.
- `HARNESS_PLANNER_ALL_OPUS=1` restores today's behaviour.

**MVP done when:** each stage's `Done when` command exits 0.

### Out of scope

- A single-pass flow where Opus implements in-session (design A in `docs/SPEED_REPORT.md`).
- Changes to workers, router, verifier or scheduler; Orchestrator routing time.

## Shared context

- **Repo:** existing Python package `harness` (src layout); work merges into `main`.
- **Stack:** Python 3.12, uv, typer, pydantic v2, claude-agent-sdk, pytest, ruff.
- **Commands:** install `uv sync`, test `uv run pytest -q <targeted test files>`, lint `uv run ruff check src tests`. Never run the whole suite.
- **Layout:** `src/harness/` (UI in `src/harness/ui/`), `tests/`, `docs/`. Planner events: `.harness/planner/events.jsonl` (`harness.models.Event`, `run_id` = planner session id).
- **Conventions:** module docstring on new modules; logging and caching failures never end a planner session; tests use tmp git repos and `FakePlannerSession`.

## Decisions

- **Rollout:** Sonnet is the default for quick plans and stage orders, falling back to Opus (asked); `HARNESS_PLANNER_ALL_OPUS=1` reverts.
- **Plan file:** at the repo root; the old run-viewer plan moved to `plans/run-viewer/PROJECT_PLAN.md` (asked). The planner reads `.harness/plans/PROJECT_PLAN.md`, so copy it there.
- **Model split:** project plan on Opus; quick plans and stage orders on Sonnet.
- **Gate:** `load_plan` plus mechanical checks, no model call; fall back after `MAX_FIX_ROUNDS` failed fixes, a session error, or no plan.
- **Digest:** cached in `.harness/planner/repo_digest.json`, keyed on git HEAD plus `git status --porcelain`; only for Sonnet order sessions.

## Estimate

| Stage | Delivers | Tasks | Orders | Worker time |
|---|---|---|---|---|
| 1 | Timing report | 5 | ~6 | ~3.6 h |
| 2 | Sonnet orders, gate, fallback | 5 | ~7 | ~4.2 h |
| 3 | Cached repo digest | 3 | ~4 | ~2.4 h |
| **Total** | | 13 | ~17 | ~10.2 h |

## Stage 1: Planning timing report (~3.6 h, ~6 orders)

**Delivers:** `harness planning-report` shows time per phase and tool, model time vs user wait, and cost.

**Done when:** `uv run pytest -q tests/test_planning_*.py tests/test_cli_planning_report.py` and `uv run ruff check src tests` exit 0

**Interfaces:**

```python
# src/harness/planning_models.py
from dataclasses import dataclass, field


@dataclass(frozen=True)
class ToolTiming:
    tool: str
    count: int
    total_ms: int
    max_ms: int
    missing_result: int


@dataclass(frozen=True)
class TurnTiming:
    phase: str
    duration_ms: int | None
    cost_usd: float | None
    wait_before_ms: int | None


@dataclass(frozen=True)
class SessionReport:
    session_id: str
    stage: int | None
    route: str | None
    turns: list[TurnTiming] = field(default_factory=list)
    tools: list[ToolTiming] = field(default_factory=list)
    phase_ms: dict[str, int] = field(default_factory=dict)
    model_ms: int = 0
    cost_usd: float = 0.0


# src/harness/planning_report.py
from pathlib import Path


def build_reports(
    repo: Path, last: int = 5, session_id: str | None = None
) -> list[SessionReport]: ...


# src/harness/planning_render.py
def render_markdown(reports: list[SessionReport]) -> str: ...
```

- Events (by `run_id`): `planner_started` (`stage`), `planner_triage`/`planner_done` (`route`), `planner_message` (`phase`), `planner_reply` (`phase`, `duration_ms` absent in old events, cumulative `cost_usd`), `planner_tool_finished` (`tool`, `duration_ms`, `status`).
- `build_reports`: sessions in log order, the newest `last` (or only `session_id`; empty if unknown). `wait_before_ms` = gap between the previous `planner_reply` and this `planner_message` (None for the first turn). `model_ms` sums known durations, `phase_ms` per phase, `cost_usd` is the last reply's.
- `render_markdown`, per session, seconds with one decimal: `## <session_id> (stage <n>, route <route>)`; `Model time <s> s, cost $<c>`; table `| Phase | Turns | Model time (s) |`; table `| Tool | Calls | Total (s) | Max (s) | No result |` by total descending, or `No tool timings recorded (older session).`; `User wait between turns: <s> s (not model latency)`. Unknown stage or route print `?`; no reports gives `No planner sessions found.`
- CLI `harness planning-report [--repo PATH] [--last N] [--session ID] [--json]` prints the markdown or `json.dumps([dataclasses.asdict(r) ...], indent=2)`; with no sessions it prints `No planner sessions found.` to stderr and exits 1.

### T1.1 Add the planning timing dataclasses
- **Tier:** haiku
- **Depends:** none
- **Files:** `src/harness/planning_models.py`, `tests/test_planning_models.py`
- **Do:** Create the three dataclasses as pinned. Tests check the defaults, that instances are frozen and that `asdict` works.
- **Done when:** `uv run pytest -q tests/test_planning_models.py` passes and `uv run ruff check src tests` is clean.

### T1.2 Build session reports from the planner event log
- **Tier:** sonnet
- **Depends:** T1.1
- **Files:** `src/harness/planning_report.py`, `tests/test_planning_report.py`
- **Do:** Implement `build_reports` with `harness.planner_log.read_planner_events`. Pair each `planner_message` with the next `planner_reply` of its session; a missing duration or cost is None; a `missing_result` tool adds nothing to `total_ms`. Tests write logs with `PlannerLog.emit` (two sessions, an old turn, an unfinished tool) and cover `last`, `session_id`, unknown id, missing log, wait.
- **Done when:** `uv run pytest -q tests/test_planning_report.py` passes and `uv run ruff check src tests` is clean.

### T1.3 Render planning reports as Markdown
- **Tier:** haiku
- **Depends:** T1.1
- **Files:** `src/harness/planning_render.py`, `tests/test_planning_render.py`
- **Do:** Implement `render_markdown` with the pinned layout: seconds via `f"{ms / 1000:.1f}"`, cost with two decimals, phases in order of first appearance, user wait = sum of known `wait_before_ms`. If turns lack a duration add `<n> turn(s) have no duration (logged before timings existed).` after the phase table. Tests compare one full report with an expected string, plus no reports, no tools.
- **Done when:** `uv run pytest -q tests/test_planning_render.py` passes and `uv run ruff check src tests` is clean.

### T1.4 Add the planning-report command
- **Tier:** sonnet
- **Depends:** T1.2, T1.3
- **Files:** `src/harness/cli.py`, `tests/test_cli_planning_report.py`
- **Do:** Add `@app.command("planning-report")` with the pinned options (`--repo` uses `RepoOpt`, `--last` min 1 default 5) and output; change no other command. Tests use `CliRunner` on a tmp repo: markdown, `--json`, `--last 1`, exit 1 for an unknown session and an empty repo.
- **Done when:** `uv run pytest -q tests/test_cli_planning_report.py` passes and `uv run ruff check src tests` is clean.

### T1.5 Document how to capture and read planning timings
- **Tier:** haiku
- **Depends:** T1.4
- **Files:** `docs/PLANNING_TIMINGS.md`
- **Do:** Write a short guide: the logged events, capturing a baseline (3 real UI requests: one quick plan, two project plans), reading `harness planning-report --last 3`, and caveats: user wait is not model latency, nested agent tools are not timed, `missing_result` is time until the turn ended.
- **Done when:** `grep -c "harness planning-report" docs/PLANNING_TIMINGS.md` and `grep -c "missing_result" docs/PLANNING_TIMINGS.md` each print 1 or more.

## Stage 2: Sonnet order sessions with gate and fallback (~4.2 h, ~7 orders)

**Delivers:** quick plans and stage orders are written by Sonnet, checked mechanically and redone on Opus on failure, in the CLI and the office page.

**Done when:** `uv run pytest -q tests/test_planner_tiers.py tests/test_order_check.py tests/test_planner_order_tier.py tests/test_planner_wiring.py` and `uv run ruff check src tests` exit 0

**Interfaces:**

```python
# src/harness/config.py (additions)
import os
from collections.abc import Mapping

PLANNER_TIERS: dict[str, Tier] = {
    "project": "opus",
    "quick": "sonnet",
    "staging": "sonnet",
    "fallback": "opus",
}
PLANNER_ALL_OPUS_ENV = "HARNESS_PLANNER_ALL_OPUS"


def planner_tier(phase: str, env: Mapping[str, str] | None = None) -> Tier: ...


# src/harness/order_check.py
from pathlib import Path

from harness.models import Plan

MAX_CONTEXT_ITEM_CHARS = 6000
MAX_CONTEXT_TOTAL_CHARS = 24000


def check_plan_quality(plan: Plan, repo: Path) -> list[str]: ...


# src/harness/planner.py (additions)
from collections.abc import Callable

OrderSessionFactory = Callable[[Tier], PlannerSession]
```

- `planner_tier`: `ValueError` for an unknown phase; `"opus"` for every phase when `(env or os.environ).get(PLANNER_ALL_OPUS_ENV) == "1"`.
- `check_plan_quality` returns one string per problem, `[]` when fine: `"<id>: files_readonly <path> does not exist and no earlier order creates it"` (missing in `repo` and not owned by a transitive dependency); `"<id>: files_owned <path> must be a relative path without .."`; `"<id>: context item '<label>' is <n> chars (max 6000)"`; `"<id>: context totals <n> chars (max 24000)"`; `"<id>: no steps"`; `"<id>: acceptance command does not parse: <command>"`.
- `SdkPlannerSession.__init__` gains `tier: Tier = "opus"` (`options()` uses `MODEL_IDS[self.tier]`). `run_planner` gains `order_session_factory: OrderSessionFactory | None = None`; `PlanResult` gains `order_usage: Usage` and `usage` = triage + Opus + order usage. New constants `QUALITY_FIX_PROMPT = "The work orders in {plan_path} have these problems:\n{issues}\n\nFix them and write the plan again to the same path."` and `FALLBACK_SAY = "Falling back to {tier} for the work orders: {reason}"`.
- Order turns (quick-route plan turns, the stage-1 draft-orders turn, staging turns) go, with a factory, to one lazily created session `order_session_factory(planner_tier("quick" or "staging"))`, closed in `finally`. Each result must pass `load_plan` and `check_plan_quality`; failures send `FIX_PROMPT` or `QUALITY_FIX_PROMPT` to the same session, at most `MAX_FIX_ROUNDS` times. On exhaustion, a turn error or no plan: close it, `say(FALLBACK_SAY.format(...))`, rerun that prompt on the Opus `session`, and stay on Opus for the call. Without a factory nothing changes.

### T2.1 Add the mechanical work-order quality check
- **Tier:** sonnet
- **Depends:** none
- **Files:** `src/harness/order_check.py`, `tests/test_order_check.py`
- **Do:** Implement `check_plan_quality` with the six pinned messages; dependencies are transitive, file contents are never read. Tests cover each message, a clean plan, a readonly file created upstream.
- **Done when:** `uv run pytest -q tests/test_order_check.py` passes and `uv run ruff check src tests` is clean.

### T2.2 Add the planner tier map
- **Tier:** haiku
- **Depends:** none
- **Files:** `src/harness/config.py`, `tests/test_planner_tiers.py`
- **Do:** Add the pinned names to `config.py`; keep existing ones. Tests: each phase's tier, `"1"` gives opus everywhere, `"0"` does not, unknown phase raises.
- **Done when:** `uv run pytest -q tests/test_planner_tiers.py` passes and `uv run ruff check src tests` is clean.

### T2.3 Run order turns on a Sonnet session with fallback to Opus
- **Tier:** sonnet
- **Depends:** T2.1, T2.2
- **Files:** `src/harness/planner.py`, `tests/test_planner_order_tier.py`
- **Do:** Implement everything pinned for `planner.py` (the `tier` argument, `OrderSessionFactory`, `order_usage`, the constants, order-turn routing with fallback), reusing `plan_turn` by parameterising its session. `tests/test_planner.py` must pass unchanged. New tests (`FakePlannerSession` for both sessions): a pass on Sonnet, two failures then fallback (`FALLBACK_SAY`, order session closed), a Sonnet turn error, a plan failing only `check_plan_quality`, staging, `order_usage` in `usage`.
- **Done when:** `uv run pytest -q tests/test_planner_order_tier.py tests/test_planner.py` passes and `uv run ruff check src tests` is clean.

### T2.4 Wire the order session into the CLI and the office
- **Tier:** sonnet
- **Depends:** T2.3
- **Files:** `src/harness/cli.py`, `src/harness/ui/office.py`, `tests/test_planner_wiring.py`
- **Do:** In `cli.py` add `_make_order_session(repo, tier)` returning `SdkPlannerSession(repo, tier=tier)` (`_make_planner_session` stays unchanged) and pass `order_session_factory` to `run_planner`, wrapping sessions in `ObservedSession` with phase `"quick"` for the quick route, else `"staging"`. In `office.py` add the constructor argument `order_session_factory: SessionFactory | None = None`, defaulting to a real factory only where the real Opus factory is built (None when `session_factory` is overridden or in fake mode, so existing tests are unaffected); pass it on and wrap it like the Opus session. Tests (fakes): a CLI quick plan and an office staging turn log `planner_message` events with phase `quick`/`staging`; no factory use under `HARNESS_PLANNER_ALL_OPUS=1`.
- **Done when:** `uv run pytest -q tests/test_planner_wiring.py tests/test_office_routes.py tests/test_planner.py tests/test_orchestrator_flow.py` passes and `uv run ruff check src tests` is clean.

### T2.5 Document the planner tiers
- **Tier:** haiku
- **Depends:** T2.4
- **Files:** `docs/PLANNER_TIERS.md`
- **Do:** Describe the model per phase, the quality checks, the fallback rules and the `HARNESS_PLANNER_ALL_OPUS=1` switch.
- **Done when:** `grep -c "HARNESS_PLANNER_ALL_OPUS" docs/PLANNER_TIERS.md` and `grep -c "check_plan_quality" docs/PLANNER_TIERS.md` each print 1 or more.

## Stage 3: Cached repo digest in order prompts (~2.4 h, ~4 orders)

**Delivers:** Sonnet order sessions start from a cached summary of the repo, rebuilt only when git HEAD or the working tree changes.

**Done when:** `uv run pytest -q tests/test_repo_digest.py tests/test_planner_digest.py tests/test_planner_order_tier.py` and `uv run ruff check src tests` exit 0

**Interfaces:**

```python
# src/harness/repo_digest.py
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Digest:
    text: str
    head: str
    reused: bool


DIGEST_FILE = Path(".harness") / "planner" / "repo_digest.json"
MAX_TREE_LINES = 200


def load_or_build(repo: Path) -> Digest: ...
```

- `head` is `git rev-parse HEAD` or `"no-commit"`. Cache key: `head` plus sha256 of `git status --porcelain`; `DIGEST_FILE` stores `{"head", "state", "text"}`. `reused` is True only when both match; a missing or invalid cache rebuilds; a failed write is ignored.
- `text` sections in order: `## Tree` (sorted `git ls-files`, at most `MAX_TREE_LINES`, then `... <n> more files`), `## Manifest` (first 60 lines of the first of `pyproject.toml`, `package.json`, `Cargo.toml`, `go.mod`), `## README` (first 40 lines), `## Recent commits` (`git log --oneline -10`); a section with no source is omitted.
- `QUICK_PROMPT`, `STAGE_PROMPT` and `DRAFT_STAGE_PROMPT` each end with `\n\nRepo digest (git HEAD {head}; read files only for detail beyond it):\n\n{digest}`. `run_planner` says `Repo digest: reused` or `Repo digest: rebuilt (<n> lines)` once per order turn.

### T3.1 Build and cache the repo digest
- **Tier:** sonnet
- **Depends:** none
- **Files:** `src/harness/repo_digest.py`, `tests/test_repo_digest.py`
- **Do:** Implement `load_or_build` as pinned; run git with `subprocess.run(..., capture_output=True, text=True, check=False)` and treat any failure as "no git". Tests on tmp git repos: rebuild, reuse, rebuild after a commit, an uncommitted edit or a corrupt cache, `"no-commit"`, a non-git folder, the tree cap, omitted sections.
- **Done when:** `uv run pytest -q tests/test_repo_digest.py` passes and `uv run ruff check src tests` is clean.

### T3.2 Put the digest into the order prompts
- **Tier:** sonnet
- **Depends:** T3.1
- **Files:** `src/harness/planner.py`, `tests/test_planner_digest.py`
- **Do:** Extend the three prompt constants as pinned and fill `{head}` and `{digest}` from `load_or_build(repo)` wherever `run_planner` formats them, with the `say` line once per order turn; an exception means an empty digest. `PROJECT_PROMPT` is unchanged. Edit `tests/test_planner.py` only where an exact prompt comparison breaks. New tests (`FakePlannerSession`): the prompt contains digest and head, a second turn says `Repo digest: reused`, a failing `load_or_build` still sends the prompt.
- **Done when:** `uv run pytest -q tests/test_planner_digest.py tests/test_planner.py tests/test_planner_order_tier.py` passes and `uv run ruff check src tests` is clean.

### T3.3 Extend the guide with before and after measurements
- **Tier:** haiku
- **Depends:** T3.2
- **Files:** `docs/PLANNING_TIMINGS.md`
- **Do:** Add a section "Before and after": rerun the three baseline requests with `HARNESS_PLANNER_ALL_OPUS=1` (before) and without (after); compare model time, cost and `Repo digest: reused` lines; Sonnet orders are fine when fallbacks are rare and escalations do not rise.
- **Done when:** `grep -c "Before and after" docs/PLANNING_TIMINGS.md` and `grep -c "Repo digest" docs/PLANNING_TIMINGS.md` each print 1 or more.

## Prerequisites and risks

- Copy this file to `.harness/plans/PROJECT_PLAN.md`, set `Status: approved <date>`, run `harness plan --stage 1` or paste the path into the Orchestrator. Commit or stash first: runs start only from `main`.
- Real baselines need 3 live planner requests (about $1-5 each) after Stage 1.
- Risk: Sonnet orders may cause more escalations. Mitigation: the gate, the Opus fallback, the env switch, the before/after comparison.
