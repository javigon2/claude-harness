"""Planner behind `harness plan`: one interactive Opus session that runs the plan-project
skill until the user approves PROJECT_PLAN.md, then plan-stage on stage N until it writes a
plan.json that load_plan accepts. Every file goes under <repo>/.harness/plans/. run_planner can
also triage a request (direct, quick or project) in a separate one-turn Sonnet session with
read-only tools, and write a quick one-stage plan under .harness/plans/quick-<stamp>/."""

import asyncio
import hashlib
import json
import re
import time
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any, Literal, Protocol, runtime_checkable

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    ResultMessage,
    SystemMessage,
    TextBlock,
    ToolUseBlock,
)
from pydantic import BaseModel, ConfigDict, Field

from harness.config import MODEL_IDS
from harness.models import Usage
from harness.plan import PlanError, load_plan
from harness.worker import (
    WORKER_ENV,
    ProgressFn,
    describe_tool,
    session_init_facts,
    usage_from_result,
)

HARNESS_ROOT = Path(__file__).resolve().parents[2]

PLANNER_SKILLS: tuple[str, ...] = ("plan-project", "plan-stage")

PLANNER_TOOLS: tuple[str, ...] = ("Read", "Write", "Edit", "Glob", "Grep", "Bash")

PLANNER_MAX_TURNS = 80

TRIAGE_TOOLS: tuple[str, ...] = ("Read", "Glob", "Grep")

TRIAGE_MAX_TURNS = 20

PLANS_DIR = Path(".harness") / "plans"

PROJECT_PLAN_FILE = PLANS_DIR / "PROJECT_PLAN.md"

STAGE_PLAN_DIR = PLANS_DIR / "stage-1"

MAX_ROUNDS = 20

MAX_FIX_ROUNDS = 2

APPROVED_RE = re.compile(r"^Status: approved \d{4}-\d{2}-\d{2}$", re.MULTILINE)

PLANNER_APPEND = (
    "You are running headless inside `harness plan`. The user reads your replies as plain "
    "terminal text and answers by typing; ask questions and request approval in your reply "
    "text, then end your turn."
)

PROJECT_PROMPT = (
    "Use the plan-project skill to plan this project. Target repo: {repo}\n"
    "Write the plan to {project_plan} (exactly this path, not the repo root).\n\n"
    "Project description:\n\n{description}"
)

STAGE_PROMPT = (
    "{project_plan} is approved. Now use the plan-stage skill on stage {stage} of "
    "{project_plan}. Write plan.json and plan.md to {stage_dir}/ (exactly this "
    "directory). Run the skill's validation snippet before you finish."
)

FIX_PROMPT = (
    "`harness validate` rejected {plan_path}: {error}\n\n"
    "Fix the plan and write it again to the same path."
)

PLANNER_INIT_FILE = Path(".harness") / "planner" / "session_init.json"

Route = Literal["direct", "quick", "project"]

RouteChoice = Literal["auto", "quick", "project"]

APPROVE_QUICK = "\x00approve-quick"

TRIAGE_PROMPT = (
    "Decide how much planning this request needs. Target repo: {repo}\n"
    "Read only what you need and write no file. End with a ```json block: "
    '{{"route": "direct" | "quick" | "project", "tier": "haiku" | "sonnet", '
    '"reason": "<one line>"}}.\n'
    "direct: one agent in a short chat (1-3 files); haiku if mechanical, else sonnet. "
    "quick: one stage of 2-8 work orders, no open question. "
    "project: several stages or user choices.\n\nRequest:\n\n{description}"
)

QUICK_PROMPT = (
    "Use the plan-stage skill in quick mode on the request below. Target repo: {repo}\n"
    "Write plan.json and plan.md to {stage_dir}/ (exactly this directory) and run the "
    "skill's validation snippet.\n\nRequest:\n\n{description}"
)

WRITE_PLAN_FIRST = "Write the plan first."

NO_ROUTE_REASON = "triage gave no route; planning in full"

_JSON_BLOCK_RE = re.compile(r"```json[ \t]*\n(.*?)```", re.DOTALL)


def quick_plan_dir(now: float) -> Path:
    """PLANS_DIR / f"quick-{UTC %Y%m%d-%H%M%S of now}", relative to the target repo."""
    return PLANS_DIR / f"quick-{time.strftime('%Y%m%d-%H%M%S', time.gmtime(now))}"


class Triage(BaseModel):
    """Sonnet's routing of a request (the triage session): how much planning it needs."""

    model_config = ConfigDict(extra="forbid")

    route: Route
    tier: Literal["haiku", "sonnet"] = "sonnet"
    reason: str = ""


def parse_triage(text: str) -> Triage | None:
    """The last ```json block of `text` as a Triage; None if there is no block or it is not
    valid JSON or not a valid Triage."""
    blocks = _JSON_BLOCK_RE.findall(text)
    if not blocks:
        return None
    try:
        return Triage.model_validate_json(blocks[-1])
    except ValueError:
        return None


def stage_plan_dir(stage: int) -> Path:
    """PLANS_DIR / f"stage-{stage}", relative to the target repo. Raises ValueError if
    stage < 1."""
    if stage < 1:
        raise ValueError(f"stage must be at least 1, got {stage}")
    return PLANS_DIR / f"stage-{stage}"


def _names(value: Any) -> str:
    """Comma-separated names of a list (an item's "name" if it is a dict that has one,
    else str(item)); "none" if value is not a non-empty list."""
    if not isinstance(value, list) or not value:
        return "none"
    return ", ".join(
        str(item["name"]) if isinstance(item, dict) and "name" in item else str(item)
        for item in value
    )


def format_session_init(facts: Mapping[str, Any]) -> str:
    """Four lines for the terminal, e.g.
    Planner session: model=<model id> apiKeySource=none permissionMode=acceptEdits
      skills: plan-project, plan-stage
      tools: Read, Write
      mcp_servers: none"""
    return "\n".join(
        [
            f"Planner session: model={facts.get('model')} "
            f"apiKeySource={facts.get('apiKeySource')} "
            f"permissionMode={facts.get('permissionMode')}",
            f"  skills: {_names(facts.get('skills'))}",
            f"  tools: {_names(facts.get('tools'))}",
            f"  mcp_servers: {_names(facts.get('mcp_servers'))}",
        ]
    )


class PlannerError(RuntimeError):
    """The planner stopped without a valid plan. The message says why."""


class PlannerTurn(BaseModel):
    """One planner reply. `usage` is the session's running total so far (the SDK reports
    cumulative usage on every turn of a ClaudeSDKClient session), not this turn alone."""

    model_config = ConfigDict(extra="forbid")

    text: str = ""
    usage: Usage = Field(default_factory=Usage)
    error: str | None = None
    session_init: dict[str, Any] | None = None


class PlanResult(BaseModel):
    """What run_planner produced. `plan_path` is None for the direct route (no plan is
    written); `triage` is the Sonnet triage's routing when the route was chosen by triage.
    `usage` includes the triage turn."""

    model_config = ConfigDict(extra="forbid")

    project_plan: Path
    plan_path: Path | None
    usage: Usage = Field(default_factory=Usage)
    rounds: int = 0
    session_init: dict[str, Any] | None = None
    route: Route = "project"
    triage: Triage | None = None


@runtime_checkable
class PlannerSession(Protocol):
    async def send(self, text: str) -> PlannerTurn:
        """Send one user message and return the planner's reply once its turn ends.
        Failures come back as PlannerTurn.error, never as an exception (cancellation still
        propagates)."""
        ...

    async def close(self) -> None:
        """End the session. Safe to call more than once, and before any send()."""
        ...


Ask = Callable[[str], str | None]
Say = Callable[[str], None]


def check_skills(root: Path = HARNESS_ROOT) -> None:
    """Raise PlannerError(f"planner skills not found under {root / '.claude' / 'skills'}")
    unless <root>/.claude/skills/<name>/SKILL.md exists for every name in PLANNER_SKILLS."""
    skills_dir = Path(root) / ".claude" / "skills"
    for name in PLANNER_SKILLS:
        if not (skills_dir / name / "SKILL.md").is_file():
            raise PlannerError(f"planner skills not found under {root / '.claude' / 'skills'}")


def is_approved(text: str) -> bool:
    """True if `text` has a line that is exactly `Status: approved YYYY-MM-DD`."""
    return APPROVED_RE.search(text) is not None


def _fingerprint(path: Path) -> str | None:
    """sha256 hex digest of the file's bytes, or None if it does not exist."""
    try:
        data = Path(path).read_bytes()
    except FileNotFoundError:
        return None
    return hashlib.sha256(data).hexdigest()


class SdkPlannerSession:
    """A PlannerSession on one ClaudeSDKClient, connected on the first send()."""

    def __init__(
        self,
        repo: Path,
        client_factory: Callable[[ClaudeAgentOptions], Any] | None = None,
        harness_root: Path = HARNESS_ROOT,
        *,
        progress: ProgressFn | None = None,
        progress_interval_s: float = 2.0,
    ) -> None:
        """self.repo = Path(repo).resolve(); self.harness_root = Path(harness_root).resolve();
        self._factory = client_factory or ClaudeSDKClient (called as
        factory(options=<ClaudeAgentOptions>)); self._client = None; self._usage = Usage()
        (the running total from the latest ResultMessage). `progress`, when given, is awaited
        with describe_tool(...) for the tool calls of each turn, throttled like SdkWorker:
        a new tool name, or at least `progress_interval_s` since the last report."""
        self.repo = Path(repo).resolve()
        self.harness_root = Path(harness_root).resolve()
        self._progress = progress
        self.progress_interval_s = progress_interval_s
        self._factory = client_factory or ClaudeSDKClient
        self._client: Any = None
        self._usage = Usage()

    def options(self) -> ClaudeAgentOptions:
        """The exact options; values in the `decision: planner SDK options` context item."""
        return ClaudeAgentOptions(
            model=MODEL_IDS["opus"],
            system_prompt={"type": "preset", "preset": "claude_code", "append": PLANNER_APPEND},
            tools=[*PLANNER_TOOLS, "Skill"],
            allowed_tools=list(PLANNER_TOOLS),
            skills=list(PLANNER_SKILLS),
            setting_sources=["project"],
            add_dirs=[str(self.harness_root)],
            permission_mode="acceptEdits",
            cwd=str(self.repo),
            max_turns=PLANNER_MAX_TURNS,
            strict_mcp_config=True,
            env=dict(WORKER_ENV),
        )

    async def send(self, text: str) -> PlannerTurn:
        """Algorithm in the `decision: planner session` context item."""
        if self._client is None:
            client = self._factory(options=self.options())
            try:
                await client.connect()
            except Exception as err:
                return PlannerTurn(
                    usage=self._usage,
                    error=f"connect failed: {type(err).__name__}: {err}"[:2000],
                )
            self._client = client

        result_msg: ResultMessage | None = None
        last_text = ""
        error: str | None = None
        session_init: dict[str, Any] | None = None
        last_tool: str | None = None
        last_report = 0.0
        try:
            await self._client.query(text)
            async for message in self._client.receive_response():
                if isinstance(message, AssistantMessage):
                    if message.parent_tool_use_id is None:
                        if self._progress is not None:
                            for b in message.content:
                                if not isinstance(b, ToolUseBlock):
                                    continue
                                now = time.monotonic()
                                if (
                                    b.name != last_tool
                                    or now - last_report >= self.progress_interval_s
                                ):
                                    last_tool, last_report = b.name, now
                                    try:
                                        await self._progress(
                                            describe_tool(b.name, b.input, self.repo)
                                        )
                                    except Exception:  # a broken callback must not end the turn
                                        pass
                        joined = "".join(
                            b.text for b in message.content if isinstance(b, TextBlock)
                        )
                        if joined.strip():
                            last_text = joined
                elif isinstance(message, SystemMessage):
                    if session_init is None:
                        session_init = session_init_facts(message)
                elif isinstance(message, ResultMessage):
                    result_msg = message
        except Exception as err:
            error = f"{type(err).__name__}: {err}"[:2000]

        if (
            result_msg is not None
            and isinstance(result_msg.result, str)
            and result_msg.result.strip()
        ):
            reply = result_msg.result
        else:
            reply = last_text

        if result_msg is not None:
            self._usage = usage_from_result(result_msg)

        if error is None:
            if result_msg is None:
                error = "turn ended without a result message"
            elif result_msg.is_error:
                error = f"result {result_msg.subtype}"
                errors = getattr(result_msg, "errors", None)
                if isinstance(errors, list) and errors:
                    error += ": " + "; ".join(str(e) for e in errors)

        return PlannerTurn(text=reply, usage=self._usage, error=error, session_init=session_init)

    async def close(self) -> None:
        """If connected: await self._client.disconnect() (Exception ignored), then
        self._client = None."""
        if self._client is not None:
            try:
                await self._client.disconnect()
            except Exception:
                pass
            self._client = None


class SdkTriageSession(SdkPlannerSession):
    """The one-turn triage session: Sonnet with read-only tools, no skills, no settings."""

    def options(self) -> ClaudeAgentOptions:
        return ClaudeAgentOptions(
            model=MODEL_IDS["sonnet"],
            system_prompt={"type": "preset", "preset": "claude_code", "append": PLANNER_APPEND},
            tools=list(TRIAGE_TOOLS),
            allowed_tools=list(TRIAGE_TOOLS),
            skills=[],
            setting_sources=[],
            permission_mode="default",
            cwd=str(self.repo),
            max_turns=TRIAGE_MAX_TURNS,
            strict_mcp_config=True,
            env=dict(WORKER_ENV),
        )


class FakePlannerSession:
    """A scripted PlannerSession for tests. Each send() takes the next step: a PlannerTurn
    (returned as a deep copy) or a callable (text) -> PlannerTurn, which may write files
    to simulate the planner's edits."""

    def __init__(self, steps: Sequence[PlannerTurn | Callable[[str], PlannerTurn]]) -> None:
        """Copy steps into a list. self.sent: list[str] = []; self.closed = False."""
        self._steps: list[PlannerTurn | Callable[[str], PlannerTurn]] = list(steps)
        self.sent: list[str] = []
        self.closed = False

    async def send(self, text: str) -> PlannerTurn:
        """Append text to self.sent FIRST, then pop and apply the front step. With no step
        left, raise RuntimeError("no scripted planner turn left")."""
        self.sent.append(text)
        if not self._steps:
            raise RuntimeError("no scripted planner turn left")
        step = self._steps.pop(0)
        if isinstance(step, PlannerTurn):
            return step.model_copy(deep=True)
        return step(text)

    async def close(self) -> None:
        """self.closed = True."""
        self.closed = True


async def run_planner(
    description: str,
    repo: Path,
    session: PlannerSession,
    ask: Ask,
    say: Say,
    *,
    stage: int = 1,
    max_rounds: int = MAX_ROUNDS,
    route: RouteChoice = "project",
    on_triage: Callable[[Triage], None] | None = None,
    quick_dir: Path | None = None,
    triage_session: PlannerSession | None = None,
) -> PlanResult:
    """Drive the planner for one request. Always closes both sessions (finally). Raises
    PlannerError when it stops without a valid plan.

    The "auto" route needs `triage_session`: it gets the one TRIAGE_PROMPT turn, and
    `session` (Opus) gets no message unless the route is quick or project. A triage turn
    that fails or gives no valid route falls back to planning in full (NO_ROUTE_REASON)."""
    repo = Path(repo).resolve()
    project_plan = repo / PROJECT_PLAN_FILE
    rounds = 0
    usage = Usage()
    triage_usage = Usage()
    session_init: dict[str, Any] | None = None
    plan_fp: str | None = None
    fixes = 0

    async def turn(msg: str) -> PlannerTurn:
        nonlocal rounds, usage, session_init
        rounds += 1
        if rounds > max_rounds:
            raise PlannerError(f"no valid plan after {max_rounds} rounds")
        t = await session.send(msg)
        usage = t.usage
        if t.session_init is not None and session_init is None:
            session_init = t.session_init
            say(format_session_init(session_init))
            init_path = repo / PLANNER_INIT_FILE
            try:
                init_path.parent.mkdir(parents=True, exist_ok=True)
                init_path.write_text(json.dumps(session_init, indent=2) + "\n", encoding="utf-8")
            except OSError:
                pass
        if t.text.strip():
            say(t.text)
        if t.error:
            raise PlannerError(f"planner session failed: {t.error}")
        return t

    async def triage_turn(sonnet: PlannerSession, msg: str) -> Triage:
        nonlocal rounds, triage_usage
        rounds += 1
        try:
            t = await sonnet.send(msg)
        finally:
            await sonnet.close()
        triage_usage = t.usage
        if t.text.strip():
            say(t.text)
        parsed = parse_triage(t.text) if not t.error else None
        return parsed or Triage(route="project", reason=NO_ROUTE_REASON)

    async def answer() -> str:
        a = await asyncio.to_thread(ask, "> ")
        if a is None:
            raise PlannerError("aborted: no answer (end of input)")
        if a.strip().lower() in ("quit", "exit"):
            raise PlannerError("aborted by the user")
        return a

    async def plan_turn(msg: str, plan_path: Path) -> bool:
        nonlocal plan_fp, fixes
        while True:
            await turn(msg)
            fp = _fingerprint(plan_path)
            if fp is None or fp == plan_fp:
                return False
            plan_fp = fp
            try:
                load_plan(plan_path)
            except (PlanError, ValueError, OSError) as err:
                fixes += 1
                if fixes > MAX_FIX_ROUNDS:
                    raise PlannerError(
                        f"{plan_path} is still invalid after {MAX_FIX_ROUNDS} fixes: {err}"
                    ) from err
                msg = FIX_PROMPT.format(plan_path=plan_path, error=err)
                continue
            return True

    def result(plan_path: Path | None, route: Route, triage: Triage | None) -> PlanResult:
        return PlanResult(
            project_plan=project_plan,
            plan_path=plan_path,
            usage=triage_usage.plus(usage),
            rounds=rounds,
            session_init=session_init,
            route=route,
            triage=triage,
        )

    try:
        if stage < 1:
            raise PlannerError("stage must be at least 1")
        if route != "project" and stage != 1:
            raise PlannerError(f"the {route} route needs stage 1")

        triage: Triage | None = None
        effective: Route
        if route == "auto":
            if triage_session is None:
                raise PlannerError("the auto route needs a triage session")
            triage = await triage_turn(
                triage_session, TRIAGE_PROMPT.format(repo=repo, description=description.strip())
            )
            if on_triage is not None:
                on_triage(triage)
            effective = triage.route
            if effective == "direct":
                return result(None, "direct", triage)
        else:
            effective = route

        if effective == "quick":
            qdir = repo / (quick_dir if quick_dir is not None else quick_plan_dir(time.time()))
            plan_path = qdir / "plan.json"
            plan_fp = _fingerprint(plan_path)
            await plan_turn(
                QUICK_PROMPT.format(repo=repo, stage_dir=qdir, description=description.strip()),
                plan_path,
            )
            while True:
                a = await answer()
                if a != APPROVE_QUICK:
                    await plan_turn(a, plan_path)
                    continue
                try:
                    load_plan(plan_path)
                except (PlanError, ValueError, OSError):
                    await plan_turn(WRITE_PLAN_FIRST, plan_path)
                    continue
                return result(plan_path, "quick", triage)

        stage_dir = repo / stage_plan_dir(stage)
        plan_path = stage_dir / "plan.json"

        if stage == 1:
            start_fp = _fingerprint(project_plan)
            msg = PROJECT_PROMPT.format(
                repo=repo, project_plan=project_plan, description=description.strip()
            )
            while True:
                await turn(msg)
                if _fingerprint(project_plan) not in (None, start_fp) and is_approved(
                    project_plan.read_text(encoding="utf-8")
                ):
                    break
                msg = await answer()
        elif not project_plan.is_file() or not is_approved(
            project_plan.read_text(encoding="utf-8")
        ):
            raise PlannerError(
                f"{project_plan} is missing or not approved; run `harness plan` for stage 1 first"
            )

        plan_fp = _fingerprint(plan_path)
        msg = STAGE_PROMPT.format(project_plan=project_plan, stage=stage, stage_dir=stage_dir)
        while not await plan_turn(msg, plan_path):
            msg = await answer()
        return result(plan_path, "project", triage)
    finally:
        if triage_session is not None:
            await triage_session.close()
        await session.close()
