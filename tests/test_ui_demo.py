import re
import subprocess
import sys
from pathlib import Path

from git_helpers import init_repo
from typer.testing import CliRunner

from harness.cli import app
from harness.models import Plan
from harness.plan import load_plan, validate_plan
from harness.planner import (
    PROJECT_PLAN_FILE,
    PlanResult,
    is_approved,
    run_planner,
    stage_plan_dir,
)
from harness.state import latest_run, load_state
from harness.ui.demo import DemoChatSession, DemoPlannerSession, demo_stage_plan
from harness.ui.markdown import parse_markdown
from harness.worktree import ensure_excluded

runner = CliRunner()
SKILL = Path(__file__).resolve().parents[1] / ".claude" / "skills" / "plan-project" / "SKILL.md"


def _write_plan_json(tmp_path: Path, stage: int) -> Path:
    import json

    path = tmp_path / f"stage-{stage}.json"
    path.write_text(json.dumps(demo_stage_plan(stage)), encoding="utf-8")
    return path


def test_demo_stage_plans_are_valid(tmp_path):
    for stage in (1, 2, 3):
        plan = load_plan(_write_plan_json(tmp_path, stage))
        assert isinstance(plan, Plan)
    one = load_plan(_write_plan_json(tmp_path, 1))
    assert [o.id for o in one.orders] == ["demo-a", "demo-b", "demo-c"]
    assert [o.tier for o in one.orders] == ["haiku", "haiku", "sonnet"]
    assert validate_plan(one) == [["demo-a", "demo-b"], ["demo-c"]]
    two = load_plan(_write_plan_json(tmp_path, 2))
    assert len(two.orders) == 2
    assert all(o.files_owned == [f"demo/{o.id}.txt"] for o in one.orders + two.orders)


async def test_first_message_streams_progress_then_asks(tmp_path):
    seen: list[dict] = []

    async def progress(info: dict) -> None:
        seen.append(info)

    s = DemoPlannerSession(tmp_path, progress=progress, delay_s=0)
    turn = await s.send("Use the plan-project skill to plan this project. ...")
    assert [p["tool"] for p in seen] == ["Glob", "Read", "Read"]
    assert "Two questions" in turn.text
    assert not (tmp_path / PROJECT_PLAN_FILE).exists()
    assert turn.usage.cost_usd == 0.01
    assert (await s.send("answers")).usage.cost_usd == 0.02


async def test_resume_message_asks_instead_of_writing(tmp_path):
    s = DemoPlannerSession(tmp_path, delay_s=0)
    turn = await s.send("An earlier planning session was interrupted. ...")
    assert "Two questions" in turn.text
    assert not (tmp_path / PROJECT_PLAN_FILE).exists()


async def test_draft_then_unlimited_revisions(tmp_path):
    s = DemoPlannerSession(tmp_path, delay_s=0)
    await s.send("Use the plan-project skill ...")
    await s.send("my answers")
    plan = tmp_path / PROJECT_PLAN_FILE
    draft = plan.read_text(encoding="utf-8")
    assert "Status: draft" in draft
    for n in (1, 2, 3):
        before = plan.read_bytes()
        await s.send(f"change {n}: " + "x" * 200)
        assert plan.read_bytes() != before
    text = plan.read_text(encoding="utf-8")
    assert text.count("## Revision") == 3
    echo = ("change 3: " + "x" * 200)[:80]
    assert f"> {echo}\n" in text
    assert echo + "x" not in text


def test_draft_has_every_block_type_the_page_renders(tmp_path):
    import asyncio

    s = DemoPlannerSession(tmp_path, delay_s=0)
    asyncio.run(s.send("Use the plan-project skill ..."))
    asyncio.run(s.send("answers"))
    blocks = parse_markdown((tmp_path / PROJECT_PLAN_FILE).read_text(encoding="utf-8"))
    kinds = {b["type"] for b in blocks}
    assert {"heading", "paragraph", "list", "table", "code"} <= kinds


def test_draft_passes_the_plan_project_validator(tmp_path):
    import asyncio

    s = DemoPlannerSession(tmp_path, delay_s=0)
    asyncio.run(s.send("Use the plan-project skill ..."))
    asyncio.run(s.send("answers"))
    asyncio.run(s.send("a tweak"))
    match = re.search(r"python3 - PROJECT_PLAN.md <<'EOF'\n(.*?)\nEOF\n", SKILL.read_text(), re.S)
    assert match is not None
    done = subprocess.run(
        [sys.executable, "-", str(tmp_path / PROJECT_PLAN_FILE)],
        input=match.group(1),
        capture_output=True,
        text=True,
    )
    assert done.stdout.strip().splitlines()[-1] == "OK", done.stdout + done.stderr
    assert "2 stages" in done.stdout


async def test_run_planner_with_the_demo_returns_a_plan_result(tmp_path):
    session = DemoPlannerSession(tmp_path, delay_s=0)
    plan = tmp_path / PROJECT_PLAN_FILE
    answers = ["my answers", "The user approved the plan"]
    asked: list[str] = []

    def ask(prompt: str) -> str:
        reply = answers.pop(0)
        asked.append(reply)
        if "approved" in reply:
            plan.write_text(
                plan.read_text(encoding="utf-8").replace(
                    "Status: draft", "Status: approved 2026-10-16"
                )
            )
        return reply

    said: list[str] = []
    result = await run_planner("a demo", tmp_path, session, ask, said.append)
    assert isinstance(result, PlanResult)
    assert asked == ["my answers", "The user approved the plan"]
    assert is_approved(plan.read_text(encoding="utf-8"))
    assert result.plan_path == tmp_path.resolve() / stage_plan_dir(1) / "plan.json"
    assert load_plan(result.plan_path).orders[0].id == "demo-a"
    assert any("Stage 1: 3 orders in 2 waves, validated." in t for t in said)
    assert session.closed


def test_harness_run_passes_the_demo_stage_plan_with_fake_worker(tmp_path):
    import asyncio

    repo = init_repo(tmp_path / "repo")
    ensure_excluded(repo)
    s = DemoPlannerSession(repo, delay_s=0)
    asyncio.run(s.send("Use the plan-project skill ..."))
    asyncio.run(s.send("Now use the plan-stage skill on stage 1 of x"))
    plan_path = repo / stage_plan_dir(1) / "plan.json"
    result = runner.invoke(app, ["run", str(plan_path), "--repo", str(repo), "--fake"])
    assert result.exit_code == 0, result.output
    rid = latest_run(repo)
    assert rid is not None
    state = load_state(repo / ".harness" / "runs" / rid)
    assert state.finished
    assert {t.status for t in state.tasks.values()} == {"done"}
    assert set(state.tasks) == {"demo-a", "demo-b", "demo-c"}


async def test_demo_chat_session_edits_notes(tmp_path):
    seen: list[dict] = []

    async def rec(info: dict) -> None:
        seen.append(info)

    s = DemoChatSession(tmp_path, "haiku", progress=rec, delay_s=0)
    t1 = await s.send("\n  add a line  \nmore")
    assert [p["tool"] for p in seen] == ["Read", "Edit"]
    assert [p["detail"] for p in seen] == ["README.md", "notes.md"]
    assert (tmp_path / "notes.md").read_text(encoding="utf-8") == "- add a line\n"
    assert t1.text == "Done (haiku demo): noted it in notes.md."
    assert t1.usage.cost_usd == 0.002
    t2 = await s.send("   ")
    assert (tmp_path / "notes.md").read_text(encoding="utf-8") == "- add a line\n- note\n"
    assert t2.usage.cost_usd == 0.004
    assert s.sent == ["\n  add a line  \nmore", "   "]


async def test_demo_chat_session_close(tmp_path):
    s = DemoChatSession(tmp_path, "sonnet", delay_s=0)
    assert not s.closed
    turn = await s.send("hello")
    assert turn.text == "Done (sonnet demo): noted it in notes.md."
    await s.close()
    assert s.closed
