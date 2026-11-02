import dataclasses

import pytest

from harness.planning_models import SessionReport, ToolTiming, TurnTiming


def test_session_report_defaults():
    r = SessionReport(session_id="s", stage=None, route=None)
    assert r.turns == [] and r.tools == [] and r.phase_ms == {}
    assert r.model_ms == 0 and r.cost_usd == 0.0


def test_frozen():
    t = ToolTiming("Read", 1, 10, 10, 0)
    with pytest.raises(dataclasses.FrozenInstanceError):
        t.count = 2  # type: ignore[misc]


def test_asdict():
    r = SessionReport("s", 1, "quick", turns=[TurnTiming("project", 5, 0.1, None)])
    d = dataclasses.asdict(r)
    assert d["turns"][0]["phase"] == "project" and d["stage"] == 1
