"""The scripted planner behind `harness ui --fake`: no model, no network.

DemoPlannerSession answers by what the message says, not by a fixed script, so "Continue
conversation" can be pressed any number of times. It writes a real draft PROJECT_PLAN.md
(shaped for the plan-project validator) and, on the stage prompt, a stage plan.json that
`load_plan` accepts and the fake worker can pass."""

import asyncio
import json
import re
import shlex
import sys
from pathlib import Path
from typing import Any

from harness.models import Usage
from harness.plan import load_plan, validate_plan
from harness.planner import PROJECT_PLAN_FILE, PlannerTurn, stage_plan_dir
from harness.worker import ProgressFn

TURN_COST_USD = 0.01

_STAGE_RE = re.compile(r"plan-stage skill on stage (\d+)")

QUESTIONS = """Two questions before I write the plan:

1. Should the demo core be **one module** or split into `store` and `cli`?
2. Do you want tests per stage, or one suite at the end?

Answer both in one message."""

DRAFT_REPLY = "Draft written; review it and approve, or tell me what to change."

DRAFT_PLAN = """# Demo: implementation plan

Status: draft
Repo: {repo}

## Goal

A tiny **demo** project that the scripted planner invents for `harness ui --fake`. It
exists so the whole office loop can be tried without a model.

### MVP

- the core files exist
- the polish stage adds two more

**MVP done when:** `python -c "print('ok')"` exits 0.

### Out of scope

- anything real

## Shared context

- **Repo:** existing; work merges into `main`
- **Stack:** Python 3.12, uv, pytest, ruff
- **Commands:** install `uv sync`, test `uv run pytest -q`, lint `uv run ruff check .`
- **Layout:** demo files live under `demo/`
- **Conventions:** one file per task
- **Principles:** small steps; nothing outside the owned files

## Decisions

- **Scope:** two stages of two to three tasks (asked) — enough to see waves
- **Tiers:** haiku for the leaves, sonnet for the join — cheapest that works

## Estimate

| Stage | Delivers | Tasks | Orders | Worker time |
|---|---|---|---|---|
| 1 | Demo core | 3 | ~3 | ~1 h |
| 2 | Demo polish | 2 | ~2 | ~1 h |
| **Total** | | 5 | ~5 | ~2 h |

## Stage 1: Demo core (~1 h, ~3 orders)

**Delivers:** three demo files, two independent and one that joins them.

**Done when:** `python -c "print('ok')"` exits 0

**Interfaces:**

```text
demo/demo-a.txt
demo/demo-b.txt
demo/demo-c.txt
```

- Every file holds one line of text.

### T1.1 Write demo a
- **Tier:** haiku
- **Depends:** none
- **Files:** `demo/demo-a.txt`
- **Do:** write the first demo file
- **Done when:** the file exists

### T1.2 Write demo b
- **Tier:** haiku
- **Depends:** none
- **Files:** `demo/demo-b.txt`
- **Do:** write the second demo file
- **Done when:** the file exists

### T1.3 Join a and b
- **Tier:** sonnet
- **Depends:** T1.1, T1.2
- **Files:** `demo/demo-c.txt`
- **Do:** write a file that mentions both
- **Done when:** the file exists

## Stage 2: Demo polish (~1 h, ~2 orders)

**Delivers:** two more demo files, the second after the first.

**Done when:** `python -c "print('ok')"` exits 0

**Interfaces:**

```text
demo/demo-s2-a.txt
demo/demo-s2-b.txt
```

### T2.1 Polish a
- **Tier:** haiku
- **Depends:** none
- **Files:** `demo/demo-s2-a.txt`
- **Do:** write the polish file
- **Done when:** the file exists

### T2.2 Polish b
- **Tier:** sonnet
- **Depends:** T2.1
- **Files:** `demo/demo-s2-b.txt`
- **Do:** write the second polish file
- **Done when:** the file exists

## Prerequisites and risks

- The repo needs `git init` and one commit, since worktrees branch from a commit.
- This is a scripted demo: nothing here was planned by a model.
"""


def _order(order_id: str, tier: str, depends_on: list[str]) -> dict[str, Any]:
    ok = f"{shlex.quote(sys.executable)} -c \"print('ok')\""
    return {
        "id": order_id,
        "title": f"Demo {order_id}",
        "goal": f"Pretend to build {order_id}; the fake worker changes nothing.",
        "tier": tier,
        "depends_on": depends_on,
        "files_owned": [f"demo/{order_id}.txt"],
        "files_readonly": [],
        "context": [],
        "steps": [f"Write demo/{order_id}.txt."],
        "constraints": [],
        "acceptance": [ok],
        "budget": {"max_turns": 5, "max_attempts": 2},
        "out_of_scope": [],
    }


def demo_stage_plan(stage: int) -> dict[str, Any]:
    """A plan.json dict that `load_plan` accepts. Stage 1: demo-a and demo-b (haiku, wave 1)
    and demo-c (sonnet, wave 2, depends on both). Any other stage N: demo-sN-a (haiku) and
    demo-sN-b (sonnet, depends on it)."""
    if stage == 1:
        orders = [
            _order("demo-a", "haiku", []),
            _order("demo-b", "haiku", []),
            _order("demo-c", "sonnet", ["demo-a", "demo-b"]),
        ]
        name = "Stage 1: Demo core"
    else:
        a, b = f"demo-s{stage}-a", f"demo-s{stage}-b"
        orders = [_order(a, "haiku", []), _order(b, "sonnet", [a])]
        name = f"Stage {stage}: Demo polish"
    return {"stage": name, "orders": orders}


class DemoPlannerSession:
    """A PlannerSession with scripted behaviour chosen by the message:

    - the plan-project prompt, or "earlier planning session was interrupted": three progress
      steps, then a Markdown question. No file is written.
    - "Now use the plan-stage skill on stage K": progress, then stage-K/plan.json + plan.md.
    - "approved the plan": a one-line reply, no file change.
    - any other message while PROJECT_PLAN.md does not exist: progress, then a draft plan.
    - any other message after that: a `## Revision N` section echoing the message is appended.

    `delay_s` is the pause after each progress step; `usage.cost_usd` grows by 0.01 a turn."""

    def __init__(
        self, repo: Path, progress: ProgressFn | None = None, delay_s: float = 1.0
    ) -> None:
        self.repo = Path(repo).resolve()
        self._progress = progress
        self._delay = delay_s
        self._cost = 0.0
        self.sent: list[str] = []
        self.closed = False

    async def _step(self, tool: str, detail: str) -> None:
        if self._progress is not None:
            try:
                await self._progress({"tool": tool, "detail": detail})
            except Exception:
                pass
        await asyncio.sleep(self._delay)

    def _turn(self, text: str) -> PlannerTurn:
        self._cost += TURN_COST_USD
        return PlannerTurn(text=text, usage=Usage(cost_usd=round(self._cost, 4)))

    async def send(self, text: str) -> PlannerTurn:
        self.sent.append(text)
        plan = self.repo / PROJECT_PLAN_FILE
        if (
            "Use the plan-project skill" in text
            or "earlier planning session was interrupted" in text
        ):
            await self._step("Glob", "**/*.py")
            await self._step("Read", "README.md")
            await self._step("Read", "pyproject.toml")
            return self._turn(QUESTIONS)
        m = _STAGE_RE.search(text)
        if m:
            return await self._write_stage(int(m.group(1)))
        if "approved the plan" in text:
            return self._turn("Approved. Writing stage 1 next.")
        if not plan.is_file():
            await self._step("Write", PROJECT_PLAN_FILE.as_posix())
            plan.parent.mkdir(parents=True, exist_ok=True)
            plan.write_text(DRAFT_PLAN.format(repo=self.repo), encoding="utf-8")
            return self._turn(DRAFT_REPLY)
        await self._step("Edit", PROJECT_PLAN_FILE.as_posix())
        body = plan.read_text(encoding="utf-8")
        n = len(re.findall(r"^## Revision \d+$", body, re.MULTILINE)) + 1
        echo = " ".join(text.split())[:80]
        plan.write_text(f"{body.rstrip()}\n\n## Revision {n}\n\n> {echo}\n", encoding="utf-8")
        return self._turn(DRAFT_REPLY)

    async def _write_stage(self, stage: int) -> PlannerTurn:
        await self._step("Read", PROJECT_PLAN_FILE.as_posix())
        directory = self.repo / stage_plan_dir(stage)
        await self._step("Write", f"{stage_plan_dir(stage).as_posix()}/plan.json")
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / "plan.json"
        path.write_text(json.dumps(demo_stage_plan(stage), indent=2) + "\n", encoding="utf-8")
        (directory / "plan.md").write_text(
            f"# Stage {stage} (demo)\n\nWritten by the scripted demo planner.\n", encoding="utf-8"
        )
        plan = load_plan(path)
        waves = validate_plan(plan)
        return self._turn(
            f"Stage {stage}: {len(plan.orders)} orders in {len(waves)} waves, validated."
        )

    async def close(self) -> None:
        self.closed = True
