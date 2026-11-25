from harness.planning_models import SessionReport, ToolTiming, TurnTiming
from harness.planning_render import render_markdown


def test_empty():
    assert render_markdown([]) == "No planner sessions found."


def test_full_report():
    r = SessionReport(
        session_id="planner-1",
        stage=2,
        route="quick",
        turns=[
            TurnTiming("project", 4000, 0.5, None),
            TurnTiming("stage", 1500, 0.75, 2000),
        ],
        tools=[ToolTiming("Read", 2, 400, 300, 0), ToolTiming("Bash", 1, 900, 900, 1)],
        phase_ms={"project": 4000, "stage": 1500},
        model_ms=5500,
        cost_usd=0.75,
    )
    assert render_markdown([r]) == (
        "## planner-1 (stage 2, route quick)\n\n"
        "Model time 5.5 s, cost $0.75\n\n"
        "| Phase | Turns | Model time (s) |\n|---|---|---|\n"
        "| project | 1 | 4.0 |\n| stage | 1 | 1.5 |\n\n"
        "| Tool | Calls | Total (s) | Max (s) | No result |\n|---|---|---|---|---|\n"
        "| Bash | 1 | 0.9 | 0.9 | 1 |\n| Read | 2 | 0.4 | 0.3 | 0 |\n\n"
        "User wait between turns: 2.0 s (not model latency)\n"
    )


def test_unknown_no_tools_no_duration():
    r = SessionReport("p", None, None, turns=[TurnTiming("project", None, None, None)])
    out = render_markdown([r])
    assert "(stage ?, route ?)" in out
    assert "No tool timings recorded (older session)." in out
    assert "1 turn(s) have no duration (logged before timings existed)." in out
