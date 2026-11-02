import pytest

from harness.config import PLANNER_ALL_OPUS_ENV


@pytest.fixture(autouse=True)
def _planner_all_opus(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tests that fake only the Opus planner session must never build a real Sonnet order
    session; tests of the order session unset this themselves."""
    monkeypatch.setenv(PLANNER_ALL_OPUS_ENV, "1")
