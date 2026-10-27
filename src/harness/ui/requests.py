"""Requests: one entry per thing the user asked for, so the Run dropdown reads like a
conversation list.

A request starts at the first `orchestrator_user` event of an Orchestrator thread
(`orch-<stamp>` in the planner log), or at a `planner_started` event whose session no
Orchestrator thread handed work to (a plan started straight from the composer, or by
`harness plan`). The links already in the logs tie the rest to it: `orchestrator_turn`
carries `target_kind`/`target_id` (a chat id in .harness/chats/events.jsonl, or a planner
session id), and `run_launched` carries the run id. Everything is rebuilt from the logs on
each call and nothing is written. A damaged line is skipped by the readers; a request that
cannot be built is left out."""

from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from harness.models import Event
from harness.planner_log import read_planner_events
from harness.state import StateError, load_state, run_dir
from harness.titles import fallback_title, read_title, read_titles
from harness.ui.chat import _pid_alive, read_chat_events
from harness.ui.markdown import parse_markdown
from harness.ui.office import TranscriptEntry, transcript_of
from harness.ui.orchestrator import OrchEntry, OrchestratorView, orchestrator_view_from_log
from harness.ui.runs import pid_alive

RequestKind = Literal["chat", "plan"]
RequestState = Literal["running", "waiting", "done", "attention"]
FIRST_MESSAGE_CHARS = 2000
_CHAT_HOLDER: dict[str, RequestState] = {
    "working": "running",
    "waiting": "waiting",
    "failed": "attention",
    "closed": "done",
}


class RequestEntry(BaseModel):
    """One dropdown entry. `id` is an Orchestrator thread id or a planner session id;
    `state` is running, waiting (for the user), done or attention (failed, interrupted or a
    stopped run); `runs` are the run ids it launched, oldest first (the last is the latest)."""

    model_config = ConfigDict(extra="forbid")

    id: str
    title: str
    first_message: str
    started_at: str
    kind: RequestKind
    state: RequestState
    cost_usd: float = 0.0
    runs: list[str] = Field(default_factory=list)


def _text(payload: dict[str, Any]) -> str | None:
    t = payload.get("text")
    return t if isinstance(t, str) and t.strip() else None


def _cost(payload: dict[str, Any]) -> float:
    v = payload.get("cost_usd")
    if isinstance(v, bool) or not isinstance(v, int | float):
        return 0.0
    return float(v)


def _group(events: list[Event], prefix: str) -> dict[str, list[Event]]:
    groups: dict[str, list[Event]] = {}
    for e in events:
        if e.run_id.startswith(prefix):
            groups.setdefault(e.run_id, []).append(e)
    return groups


def _chat_info(events: list[Event]) -> tuple[str, float]:
    """(working | waiting | closed | failed, cost so far) of one chat's events."""
    ended: str | None = None
    last_user = False
    cost = 0.0
    for e in events[1:]:
        if e.type == "chat_user":
            last_user = True
        elif e.type == "chat_reply":
            last_user = False
            cost = _cost(e.payload) or cost
        elif e.type == "chat_failed":
            ended = "failed"
            cost = _cost(e.payload) or cost
        elif e.type == "chat_closed" and ended != "failed":
            ended = "closed"
    if ended == "failed":
        return "failed", cost
    if ended == "closed" or not _pid_alive(events[0].payload.get("pid")):
        return "closed", cost
    return ("working" if last_user else "waiting"), cost


def _session_info(events: list[Event]) -> tuple[RequestState, float]:
    """The state of one planner session and its cost so far (the largest cost any of its
    events carries: they are running totals)."""
    cost = max((_cost(e.payload) for e in events), default=0.0)
    if any(e.type == "planner_failed" for e in events):
        return "attention", cost
    if any(e.type == "planner_done" for e in events):
        return "done", cost
    if not pid_alive(events[0].payload.get("pid")):
        return "attention", cost  # interrupted: the server that ran it is gone
    turns = [
        e
        for e in events
        if e.type in ("planner_started", "planner_user", "planner_reply", "plan_ready")
    ]
    waiting = bool(turns) and turns[-1].type in ("planner_reply", "plan_ready")
    return ("waiting" if waiting else "running"), cost


def _run_info(repo: Path, run_id: str) -> tuple[str | None, float]:
    """(running | done | stopped | None when the run has no readable state, cost)."""
    try:
        state = load_state(run_dir(repo, run_id))
    except (StateError, OSError, ValueError):
        return None, 0.0
    cost = sum(t.usage.cost_usd for t in state.tasks.values())
    if not state.finished:
        return ("running" if state.pid is None or pid_alive(state.pid) else "stopped"), cost
    done = all(t.status == "done" for t in state.tasks.values())
    return ("done" if done else "stopped"), cost


def _runs_of(sessions: list[str], launches: dict[str, list[str]]) -> list[str]:
    runs: list[str] = []
    for sid in sessions:
        for rid in launches.get(sid, []):
            if rid not in runs:
                runs.append(rid)
    return runs


def _with_runs(
    repo: Path, holder: RequestState | None, runs: list[str]
) -> tuple[RequestState, float]:
    """The request's state once its runs are counted in, and the runs' cost. The latest run
    decides when there is one; else the holder's own state."""
    cost = 0.0
    latest: str | None = None
    for rid in runs:
        latest, c = _run_info(repo, rid)
        cost += c
    if latest == "running":
        return "running", cost
    if latest == "stopped":
        return "attention", cost
    if latest == "done":
        return "done", cost
    return (holder or "done"), cost


def _entry(
    repo: Path,
    request_id: str,
    kind: RequestKind,
    first: str,
    started_at: str,
    state: RequestState,
    cost: float,
    runs: list[str],
) -> RequestEntry:
    first = first.strip()[:FIRST_MESSAGE_CHARS]
    return RequestEntry(
        id=request_id,
        title=read_title(repo, request_id) or fallback_title(first) or request_id,
        first_message=first,
        started_at=started_at,
        kind=kind,
        state=state,
        cost_usd=cost,
        runs=runs,
    )


def _thread_entry(
    repo: Path,
    tid: str,
    evs: list[Event],
    sessions: dict[str, list[Event]],
    chats: dict[str, list[Event]],
    launches: dict[str, list[str]],
) -> RequestEntry | None:
    users = [e for e in evs if e.type == "orchestrator_user" and _text(e.payload)]
    if not users:
        return None
    started = next((e for e in evs if e.type == "orchestrator_started"), evs[0])
    cost = sum(
        _cost(e.payload) for e in evs if e.type in ("orchestrator_turn", "orchestrator_failed")
    )
    targets: list[tuple[str, str]] = []
    for e in evs:
        if e.type != "orchestrator_turn":
            continue
        kind, target = e.payload.get("target_kind"), e.payload.get("target_id")
        if kind in ("chat", "opus") and isinstance(target, str) and target:
            if (kind, target) not in targets:
                targets.append((kind, target))
    plan_ids = [t for k, t in targets if k == "opus"]
    holder: RequestState | None = None
    for i, (kind, target) in enumerate(targets):
        if kind == "chat" and target in chats:
            cstate, ccost = _chat_info(chats[target])
            mine: RequestState | None = _CHAT_HOLDER[cstate]
        elif kind == "opus" and target in sessions:
            mine, ccost = _session_info(sessions[target])
        else:
            continue
        cost += ccost
        if i == len(targets) - 1:
            holder = mine
    runs = _runs_of(plan_ids, launches)
    state, run_cost = _with_runs(repo, holder, runs)
    cost += run_cost
    last = evs[-1]
    if last.type == "orchestrator_user":
        state = "running" if _pid_alive(started.payload.get("pid")) else "attention"
    elif last.type == "orchestrator_failed":
        state = "attention"
    kind_out: RequestKind = "plan" if plan_ids else "chat"
    first = _text(users[0].payload) or ""
    return _entry(repo, tid, kind_out, first, users[0].ts.isoformat(), state, cost, runs)


def _session_entry(
    repo: Path, sid: str, evs: list[Event], launches: dict[str, list[str]]
) -> RequestEntry | None:
    started = next((e for e in evs if e.type == "planner_started"), None)
    if started is None:
        return None
    first = next((t for e in evs if e.type == "planner_user" and (t := _text(e.payload))), None)
    if first is None:
        d = started.payload.get("description")
        first = d if isinstance(d, str) else ""
    holder, cost = _session_info(evs)
    runs = _runs_of([sid], launches)
    state, run_cost = _with_runs(repo, holder, runs)
    return _entry(repo, sid, "plan", first, started.ts.isoformat(), state, cost + run_cost, runs)


def list_requests(repo: Path) -> list[RequestEntry]:
    """Every request in the logs, newest first. Never raises: a missing log is [] and a
    request that cannot be built is skipped."""
    repo = Path(repo)
    try:
        planner = read_planner_events(repo)
        chat_events = read_chat_events(repo)
    except Exception:
        return []
    threads = _group(planner, "orch-")
    sessions = _group(planner, "planner-")
    chats: dict[str, list[Event]] = {}
    for e in chat_events:
        if e.type == "chat_started":
            chats.setdefault(e.run_id, []).append(e)
        elif e.run_id in chats:
            chats[e.run_id].append(e)
    launches: dict[str, list[str]] = {}
    for e in planner:
        rid = e.payload.get("run_id")
        if e.type == "run_launched" and isinstance(rid, str) and rid:
            if rid not in launches.setdefault(e.run_id, []):
                launches[e.run_id].append(rid)
    handed: set[str] = set()
    for evs in threads.values():
        for e in evs:
            if e.type == "orchestrator_turn" and e.payload.get("target_kind") == "opus":
                target = e.payload.get("target_id")
                if isinstance(target, str):
                    handed.add(target)
    entries: list[RequestEntry] = []
    for tid, evs in threads.items():
        try:
            entry = _thread_entry(repo, tid, evs, sessions, chats, launches)
        except Exception:
            continue
        if entry is not None:
            entries.append(entry)
    for sid, evs in sessions.items():
        if sid in handed:
            continue
        try:
            entry = _session_entry(repo, sid, evs, launches)
        except Exception:
            continue
        if entry is not None:
            entries.append(entry)
    entries.sort(key=lambda r: (r.started_at, r.id), reverse=True)
    return entries


def run_titles(repo: Path, run_ids: list[str], requests: list[RequestEntry]) -> dict[str, str]:
    """{run_id: title}: a run launched from a request shows that request's title, any other
    run its own stored title."""
    titles = read_titles(repo, run_ids)
    for r in requests:
        for rid in r.runs:
            if rid in run_ids and r.title:
                titles[rid] = r.title
    return titles


def request_title_of_run(repo: Path, run_id: str) -> str | None:
    """The title of the request that launched `run_id`, else the run's own title."""
    for r in list_requests(repo):
        if run_id in r.runs:
            return r.title
    return read_title(repo, run_id)


def _from_session(entry: TranscriptEntry) -> OrchEntry:
    if entry.role == "user":
        return OrchEntry(role="user", who="You", ts=entry.ts, text=entry.text)
    if entry.role == "orchestrator":
        return OrchEntry(
            role="orchestrator",
            who="Orchestrator",
            ts=entry.ts,
            text=entry.text,
            blocks=entry.blocks or parse_markdown(entry.text),
        )
    return OrchEntry(role="agent", who="opus", ts=entry.ts, text=entry.text, blocks=entry.blocks)


def request_thread(repo: Path, request_id: str) -> OrchestratorView | None:
    """The read-only transcript of a past request, in the shape of the live Orchestrator
    view (idle, no holder). None when `request_id` is not a thread or session in the logs."""
    repo = Path(repo)
    if request_id.startswith("orch-"):
        view = orchestrator_view_from_log(repo, request_id)
        return view if view.thread_id else None
    if request_id.startswith("planner-"):
        sess = [e for e in read_planner_events(repo) if e.run_id == request_id]
        if not any(e.type == "planner_started" for e in sess):
            return None
        return OrchestratorView(
            thread_id=request_id,
            transcript=[_from_session(t) for t in transcript_of(sess)],
            cost_usd=max((_cost(e.payload) for e in sess), default=0.0),
        )
    return None
