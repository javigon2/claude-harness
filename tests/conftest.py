import pytest

from harness.config import PLANNER_ALL_OPUS_ENV


@pytest.fixture(autouse=True)
def _planner_all_opus(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tests that fake only the Opus planner session must never build a real Sonnet order
    session; tests of the order session unset this themselves."""
    monkeypatch.setenv(PLANNER_ALL_OPUS_ENV, "1")


@pytest.fixture(autouse=True)
def _dashboard_history(monkeypatch: pytest.MonkeyPatch, tmp_path_factory) -> None:
    """A server without a Host keeps its Dashboard history under ~/.config; tests never
    read or write the real one."""
    import harness.ui.server as server_mod

    path = tmp_path_factory.mktemp("cfg") / "dashboard-history.json"
    monkeypatch.setattr(server_mod, "default_history_path", lambda: path)
