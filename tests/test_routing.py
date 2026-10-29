import json
from pathlib import Path

from harness.config import MODEL_IDS
from harness.planner import ORCHESTRATOR_PROMPT, Routing, SdkRoutingSession, parse_routing
from harness.ui.demo import DemoPlannerSession


def block(data: object) -> str:
    return f"```json\n{json.dumps(data)}\n```\n"


class Recorder:
    def __init__(self) -> None:
        self.events: list[dict] = []

    async def __call__(self, event: dict) -> None:
        self.events.append(event)


def test_parse_routing_cases() -> None:
    valid = {"to": "opus", "plan": "quick", "reason": "big"}
    assert parse_routing(block(valid)) == Routing(to="opus", plan="quick", reason="big")
    assert parse_routing("no block") is None
    assert parse_routing("```json\n{not json\n```\n") is None
    assert parse_routing(block({"to": "gpt"})) is None
    assert parse_routing(block({"to": "haiku", "extra": 1})) is None
    assert parse_routing(block({"to": "haiku"}) + block(valid)) == Routing(
        to="opus", plan="quick", reason="big"
    )
    assert parse_routing(block(valid) + block({"to": "gpt"})) is None
    assert parse_routing(block({"to": "haiku"})) == Routing(to="haiku")
    assert Routing(to="haiku").plan == "quick"
    assert parse_routing(block({"to": "opus"})) == Routing(to="opus", plan="quick")
    assert Routing(to="haiku").reason == ""


def test_orchestrator_prompt_shape() -> None:
    prompt = ORCHESTRATOR_PROMPT.format(
        repo="/r", holder="nobody (new work)", tail="(none)", text="fix x"
    )
    assert prompt.startswith("Route this message in the harness office;")
    assert '{"to": "haiku" | "sonnet" | "opus"' in prompt
    assert "Holder: nobody (new work)." in prompt
    assert prompt.endswith("Conversation so far:\n(none)\n\nMessage:\n\nfix x")
    assert "Default to a direct chat" in prompt
    assert "even across several files" in prompt
    assert "2-8 separable work orders" in prompt
    assert "only for multiple stages or choices only the user can make" in prompt
    assert "without reading the repo" in prompt


def test_office_router_is_one_tool_free_sonnet_turn(tmp_path: Path) -> None:
    options = SdkRoutingSession(tmp_path).options()
    assert options.model == MODEL_IDS["sonnet"]
    assert options.max_turns == 1
    assert options.tools == []
    assert options.allowed_tools == []
    assert options.skills == []
    assert options.setting_sources == []


async def test_demo_orchestrator_routes_by_first_word(tmp_path: Path) -> None:
    cases = [
        ("haiku x", "haiku", "project"),
        ("opus quick x", "opus", "quick"),
        ("fix x", "sonnet", "project"),
    ]
    for message, to, plan in cases:
        recorder = Recorder()
        s = DemoPlannerSession(tmp_path, progress=recorder, delay_s=0)
        prompt = ORCHESTRATOR_PROMPT.format(
            repo=tmp_path, holder="haiku", tail="You: Message:\n\nopus", text=message
        )
        turn = await s.send(prompt)
        assert parse_routing(turn.text) == Routing(to=to, plan=plan, reason=f"demo routing: {to}")
        assert turn.text.startswith(f"Routing to {to}.")
        assert recorder.events == [{"tool": "Read", "detail": "README.md"}]
        assert turn.usage.cost_usd == 0.01
    assert not any(tmp_path.iterdir())
