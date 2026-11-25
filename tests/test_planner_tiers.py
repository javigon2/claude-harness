import pytest

from harness.config import PLANNER_ALL_OPUS_ENV, planner_tier


def test_phase_tiers():
    assert planner_tier("project", {}) == "opus"
    assert planner_tier("quick", {}) == "sonnet"
    assert planner_tier("staging", {}) == "sonnet"
    assert planner_tier("fallback", {}) == "opus"


def test_all_opus_switch():
    env = {PLANNER_ALL_OPUS_ENV: "1"}
    assert {planner_tier(p, env) for p in ("project", "quick", "staging", "fallback")} == {"opus"}


def test_zero_does_not_switch():
    assert planner_tier("quick", {PLANNER_ALL_OPUS_ENV: "0"}) == "sonnet"


def test_unknown_phase():
    with pytest.raises(ValueError):
        planner_tier("nope", {})


def test_reads_os_environ(monkeypatch):
    monkeypatch.setenv(PLANNER_ALL_OPUS_ENV, "1")
    assert planner_tier("quick") == "opus"
