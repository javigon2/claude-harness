"""Dashboard history: Clear closes the current period, periods are windows over the runs and
logs on disk, and the Dashboard, Overview and Runs table count only the chosen window."""

import json
import threading
from datetime import UTC, datetime, timedelta

import pytest
from test_dashboard import T0, _write_run
from test_ui_server import _get

from harness.ui.history import (
    ALL_TIME,
    clear_history,
    in_window,
    load_history,
    window_of,
)
from harness.ui.metrics import build_dashboard, build_overview, planning_cost, run_rows
from harness.ui.server import make_server


def test_empty_and_broken_files_mean_no_history(tmp_path):
    assert load_history(tmp_path / "missing.json").periods == []
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    h = load_history(bad)
    assert h.periods == [] and h.current_start is None
    assert window_of(h, None) == (None, None) == window_of(h, "all")


def test_clear_closes_periods_back_to_back(tmp_path):
    path = tmp_path / "cfg" / "h.json"
    t1, t2 = T0 + timedelta(days=1), T0 + timedelta(days=2)
    first = clear_history(path, now=t1)
    assert first.start is None and first.end == t1 and "to 2026-10-27" in first.label
    second = clear_history(path, "  before the refactor  ", now=t2)
    assert second.start == t1 and second.label == "before the refactor"
    h = load_history(path)
    assert [p.id for p in h.periods] == [first.id, second.id]
    assert h.current_start == t2
    assert window_of(h, "current") == (t2, None)
    assert window_of(h, second.id) == (t1, t2)
    with pytest.raises(KeyError):
        window_of(h, "p-nope")


def test_in_window_bounds():
    w = (T0, T0 + timedelta(hours=1))
    assert in_window(T0, w) and not in_window(T0 + timedelta(hours=1), w)
    assert not in_window(T0 - timedelta(seconds=1), w)
    assert in_window(T0.replace(tzinfo=None), w)  # a naive time is UTC
    assert in_window(None, ALL_TIME) and not in_window(None, w)


def test_windows_split_runs_baseline_overview_and_planning(tmp_path):
    old, new = T0, T0 + timedelta(days=3)
    _write_run(tmp_path, "20261026-120000", cost=1.0, start=old)
    _write_run(tmp_path, "20261029-120000", cost=3.0, start=new)
    _write_run(tmp_path, "20261029-130000", cost=5.0, start=new + timedelta(hours=1))
    planner = tmp_path / ".harness" / "planner"
    planner.mkdir(parents=True)
    rows = [
        {
            "ts": ts.isoformat(),
            "run_id": rid,
            "type": "orchestrator_turn",
            "payload": {"cost_usd": c},
        }
        for ts, rid, c in ((old, "orch-a", 0.5), (new, "orch-b", 0.25))
    ]
    (planner / "events.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    fresh = (T0 + timedelta(days=2), None)

    assert [r.run_id for r in run_rows(tmp_path, fresh)] == ["20261029-130000", "20261029-120000"]
    assert len(run_rows(tmp_path)) == 3
    row = run_rows(tmp_path, fresh)[0]
    assert row.tiers == ["haiku"] and row.values["cost_usd"] == 5.0 and not row.fake

    assert build_dashboard(tmp_path, "20261029-130000").baseline_runs == 2
    assert build_dashboard(tmp_path, "20261029-130000", fresh).baseline_runs == 1
    assert build_dashboard(tmp_path, "20261026-120000", fresh).baseline_runs == 2

    assert planning_cost(tmp_path) == pytest.approx(0.75)
    assert planning_cost(tmp_path, fresh) == pytest.approx(0.25)
    o = build_overview([tmp_path], fresh)
    assert o.total_runs == 2 and o.run_cost_usd == pytest.approx(8.0)
    assert o.total_cost_usd == pytest.approx(8.25)


def test_history_routes(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _write_run(repo, "20261026-120000", cost=1.0, start=T0)
    history = tmp_path / "h.json"
    srv = make_server(repo, 0, poll_interval=0.05, heartbeat_s=0.2, history_path=history)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    try:
        code, _, body = _get(base + "/api/history")
        assert code == 200 and json.loads(body) == {"current_start": None, "periods": []}
        code, _, body = _get(base + "/api/dashboard-runs")
        assert code == 200 and [r["run_id"] for r in json.loads(body)["runs"]] == [
            "20261026-120000"
        ]
        clear_history(history, "old", now=datetime.now(UTC))
        (pid,) = [p["id"] for p in json.loads(_get(base + "/api/history")[2])["periods"]]
        assert json.loads(_get(base + "/api/dashboard-runs")[2])["runs"] == []
        assert len(json.loads(_get(base + f"/api/dashboard-runs?period={pid}")[2])["runs"]) == 1
        assert len(json.loads(_get(base + "/api/dashboard-runs?period=all")[2])["runs"]) == 1
        assert json.loads(_get(base + "/api/overview")[2])["total_runs"] == 0
        assert json.loads(_get(base + f"/api/overview?period={pid}")[2])["total_runs"] == 1
        d = json.loads(_get(base + "/api/dashboard/20261026-120000?period=all")[2])
        assert d["run_id"] == "20261026-120000"
        assert _get(base + "/api/overview?period=p-nope")[0] == 404
        assert _get(base + "/api/history/clear", method="POST")[0] == 405  # read-only server
    finally:
        srv.shutdown()
        srv.server_close()
        t.join(5)


def test_clear_over_http_needs_the_token_and_a_clean_body(tmp_path):
    from test_ui_project_server import HostSite

    cwd = tmp_path / "cwd"
    cwd.mkdir()
    site = HostSite(cwd, tmp_path / "cfg" / "recent.json")
    try:
        code, _, out = site.post("/api/history/clear", {"label": 5})
        assert code == 400 and "label must be a string" in out["error"]
        code, _, out = site.post("/api/history/clear", {"extra": 1})
        assert code == 400
        code, _, out = site.post("/api/history/clear", {"label": "sprint 1"})
        assert code == 200 and out["period"]["label"] == "sprint 1"
        assert out["history"]["current_start"] == out["period"]["end"]
        saved = json.loads((tmp_path / "cfg" / "dashboard-history.json").read_text())
        assert [p["label"] for p in saved["periods"]] == ["sprint 1"]
        code, _, out = site.post("/api/history/clear", {})
        assert code == 200 and out["period"]["start"] == saved["periods"][0]["end"]
    finally:
        site.stop()
