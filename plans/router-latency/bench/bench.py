"""Router A/B bench: same messages through 4 router configs, real model calls.

Arms
  old       HEAD prompt, Sonnet, SDK default effort/thinking (what was committed)
  new       current prompt, Sonnet, effort=low, thinking disabled (uncommitted change)
  new_def   current prompt, Sonnet, default effort/thinking (isolates the effort setting)
  haiku     current prompt, Haiku, effort=low, thinking disabled (cheaper router candidate)

Each call mirrors Orchestrator._route: new session, send, close; wall time measured the same
way as orchestrator_turn.routing_ms. Writes one JSON line per call to results.jsonl.
"""

import asyncio
import json
import random
import sys
import time
from pathlib import Path

from claude_agent_sdk import ClaudeAgentOptions

from harness.config import MODEL_IDS
from harness.planner import ORCHESTRATOR_PROMPT, SdkRoutingSession, WORKER_ENV, parse_routing

HERE = Path(__file__).parent
CWD = HERE / "cwd"
REPO = "/Users/me/dev/projects/agent-harness"  # appears in the prompt only

OLD_PROMPT = (
    "Route this message in the harness office; never answer it or edit a file. "
    "Target repo: {repo}\n"
    "Default to a direct chat with one agent when it can finish the request: haiku for "
    "a clearly specified mechanical edit, sonnet for ordinary coding or investigation, "
    "even across several files. Do not route to opus merely because a change touches "
    "multiple files or sounds difficult. Use opus with a quick plan only when 2-8 "
    "separable work orders in one stage would save time; use a project plan only for "
    "multiple stages or choices only the user can make. When unsure between direct "
    "and planning, choose sonnet; between quick and project, choose quick. "
    "Route from the request without reading the repo.\n"
    "Holder: {holder}. A follow-up stays with the holder unless it is clearly beyond the "
    "holder; never pick a lower tier than the holder.\n"
    'End with a ```json block: {{"to": "haiku" | "sonnet" | "opus", '
    '"plan": "quick" | "project", "reason": "<one line>"}}.\n\n'
    "Conversation so far:\n{tail}\n\nMessage:\n\n{text}"
)

ARMS = {
    "old": dict(model="sonnet", prompt="old", effort=None, thinking=None),
    "new": dict(model="sonnet", prompt="new", effort="low", thinking={"type": "disabled"}),
    "new_def": dict(model="sonnet", prompt="new", effort=None, thinking=None),
    "haiku": dict(model="haiku", prompt="new", effort="low", thinking={"type": "disabled"}),
}


class ArmSession(SdkRoutingSession):
    def __init__(self, arm: dict, **kw):
        super().__init__(CWD, **kw)
        self.arm = arm

    def options(self) -> ClaudeAgentOptions:
        kw = dict(
            model=MODEL_IDS[self.arm["model"]],
            system_prompt="Route the request. Reply only with the requested JSON block.",
            tools=[],
            allowed_tools=[],
            skills=[],
            setting_sources=[],
            permission_mode="default",
            cwd=str(self.repo),
            max_turns=1,
            strict_mcp_config=True,
            env=dict(WORKER_ENV),
        )
        if self.arm["effort"]:
            kw["effort"] = self.arm["effort"]
        if self.arm["thinking"]:
            kw["thinking"] = self.arm["thinking"]
        return ClaudeAgentOptions(**kw)


def build_prompt(arm: dict, case: dict) -> str:
    holder = case.get("holder") or "nobody (new work)"
    tail = "\n".join(case.get("tail", [])) or "(none)"
    if arm["prompt"] == "old":
        return OLD_PROMPT.format(repo=REPO, holder=holder, tail=tail, text=case["text"])
    effort = case.get("holder_effort", "medium") if case.get("holder") else "none (new work)"
    return ORCHESTRATOR_PROMPT.format(
        repo=REPO, holder=holder, effort=effort, tail=tail, text=case["text"]
    )


async def one(arm_name: str, case: dict, rep: int, sem: asyncio.Semaphore) -> dict:
    arm = ARMS[arm_name]
    async with sem:
        prompt = build_prompt(arm, case)
        started = time.monotonic()
        session = ArmSession(arm)
        try:
            try:
                turn = await session.send(prompt)
            except Exception as err:  # noqa: BLE001
                turn = None
                err_s = f"{type(err).__name__}: {err}"
        finally:
            try:
                await session.close()
            except Exception:
                pass
        wall = round((time.monotonic() - started) * 1000)
    rec = {"arm": arm_name, "case": case["id"], "rep": rep, "wall_ms": wall}
    if turn is None:
        rec["error"] = err_s
        return rec
    r = parse_routing(turn.text) if not turn.error else None
    rec.update(
        error=turn.error,
        connect_ms=turn.connect_ms,
        api_ms=turn.api_ms,
        cost_usd=turn.usage.cost_usd,
        out_tokens=getattr(turn.usage, "output_tokens", None),
        to=r.to if r else None,
        plan=r.plan if r else None,
        effort=r.effort if r else None,
        parsed=r is not None,
        raw=turn.text[-400:],
    )
    return rec


async def main(reps: int, conc: int) -> None:
    cases = json.loads((HERE / "cases.json").read_text())
    jobs = [(a, c, r) for r in range(reps) for c in cases for a in ARMS]
    random.Random(7).shuffle(jobs)  # interleave arms so drift hits all equally
    sem = asyncio.Semaphore(conc)
    out = (HERE / "results.jsonl").open("a")
    done = 0

    async def run(job):
        nonlocal done
        rec = await one(*job, sem)
        out.write(json.dumps(rec) + "\n")
        out.flush()
        done += 1
        print(f"{done}/{len(jobs)} {rec['arm']:8} {rec['case']:16} {rec['wall_ms']}ms "
              f"{rec.get('to')}/{rec.get('effort')} {rec.get('error') or ''}", flush=True)

    await asyncio.gather(*(run(j) for j in jobs))
    out.close()


if __name__ == "__main__":
    asyncio.run(main(int(sys.argv[1]) if len(sys.argv) > 1 else 2,
                     int(sys.argv[2]) if len(sys.argv) > 2 else 3))
