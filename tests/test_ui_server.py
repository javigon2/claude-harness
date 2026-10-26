import http.client
import json
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest
from ui_fixtures import append_event, write_blocked_run, write_happy_run, write_running_run

import harness.ui.server as server_mod
from harness.state import STATE_FILE, run_dir
from harness.titles import write_title
from harness.ui.server import make_server
from harness.ui.view import build_view


def _snapshot(root: Path) -> dict:
    return {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in root.rglob("*") if p.is_file()}


def _start(repo: Path):
    srv = make_server(repo, 0, poll_interval=0.05, heartbeat_s=0.2)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}", t


@pytest.fixture
def server(tmp_path):
    write_happy_run(tmp_path)
    write_blocked_run(tmp_path)
    write_running_run(tmp_path)
    srv, base, t = _start(tmp_path)
    yield srv, base
    srv.shutdown()
    srv.server_close()
    t.join(5)


def _get(url: str, method: str | None = None):
    data = b"" if method in ("POST", "PUT", "PATCH") else None
    req = urllib.request.Request(url, method=method, data=data)
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, dict(r.headers), r.read()
    except urllib.error.HTTPError as err:
        return err.code, dict(err.headers), err.read()


def test_runs_list(server, tmp_path):
    _, base = server
    code, _, body = _get(base + "/api/runs")
    assert code == 200
    assert json.loads(body) == {
        "runs": ["running1", "happy1", "blocked1"],
        "latest": "running1",
        "titles": {},
    }


def test_runs_empty(tmp_path):
    srv, base, t = _start(tmp_path)
    try:
        code, _, body = _get(base + "/api/runs")
        assert code == 200
        assert json.loads(body) == {"runs": [], "latest": None, "titles": {}}
    finally:
        srv.shutdown()
        srv.server_close()
        t.join(5)


def test_runs_list_carries_titles(server, tmp_path):
    _, base = server
    write_title(tmp_path, "happy1", "Fix the login redirect")
    _, _, body = _get(base + "/api/runs")
    assert json.loads(body)["titles"] == {"happy1": "Fix the login redirect"}


def test_dashboard(server, tmp_path):
    _, base = server
    code, headers, body = _get(base + "/api/dashboard/happy1")
    assert code == 200
    assert headers["Cache-Control"] == "no-store"
    d = json.loads(body)
    assert d["run_id"] == "happy1"
    assert {m["key"] for m in d["metrics"]} >= {"cost_usd", "wall_s", "active_s", "avg_latency_s"}
    assert d["baseline_runs"] >= 0


def test_dashboard_unknown_run(server):
    _, base = server
    code, _, _ = _get(base + "/api/dashboard/nope")
    assert code == 404


@pytest.mark.parametrize("run_id", ["happy1", "blocked1", "running1"])
def test_run_view(server, tmp_path, run_id):
    _, base = server
    code, headers, body = _get(f"{base}/api/run/{run_id}/")
    assert code == 200
    assert headers["Cache-Control"] == "no-store"
    assert json.loads(body) == json.loads(build_view(tmp_path, run_id).model_dump_json())


def test_not_found(server):
    _, base = server
    for path, err in [
        ("/api/run/nope", "unknown run"),
        ("/api/run/..%2f..%2fetc", "unknown run"),
        ("/nope", "not found"),
    ]:
        code, _, body = _get(base + path)
        assert code == 404
        assert json.loads(body) == {"error": err}


def test_read_only_methods(server):
    _, base = server
    for method in ("POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"):
        code, headers, body = _get(base + "/api/runs", method=method)
        assert code == 405
        assert headers["Allow"] == "GET"
        if method != "HEAD":
            assert json.loads(body) == {"error": "read-only"}


def test_index(server, tmp_path, monkeypatch):
    _, base = server
    static = tmp_path / "static"
    static.mkdir()
    monkeypatch.setattr(server_mod, "STATIC_DIR", static)
    code, _, body = _get(base + "/")
    assert code == 404
    assert json.loads(body) == {"error": "not found"}
    (static / "index.html").write_text("<p>hi</p>")
    code, headers, body = _get(base + "/")
    assert code == 200
    assert headers["Content-Type"].startswith("text/html")
    assert body == b"<p>hi</p>"


def test_bad_host(server):
    srv, _ = server
    conn = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=5)
    conn.putrequest("GET", "/api/runs", skip_host=True)
    conn.putheader("Host", "evil.example")
    conn.endheaders()
    r = conn.getresponse()
    assert r.status == 403
    assert json.loads(r.read()) == {"error": "bad host"}
    conn.close()


def _read_until(resp, prefix: bytes, deadline: float) -> bytes:
    while time.monotonic() < deadline:
        line = resp.readline()
        if not line:
            break
        if line.startswith(prefix):
            return line
    raise AssertionError(f"no line starting with {prefix!r}")


def test_sse_snapshots(server, tmp_path):
    _, base = server
    with urllib.request.urlopen(base + "/api/stream/running1", timeout=5) as resp:
        assert resp.headers["Content-Type"].startswith("text/event-stream")
        line = _read_until(resp, b"data:", time.monotonic() + 5)
        assert len(json.loads(line[5:])["events"]) == 14
        append_event(tmp_path, "running1", "merged", "b", {"branch": "x", "committed": True})
        line = _read_until(resp, b"data:", time.monotonic() + 5)
        assert len(json.loads(line[5:])["events"]) == 15


def test_sse_unknown(server):
    _, base = server
    code, _, body = _get(base + "/api/stream/nope")
    assert code == 404
    assert json.loads(body) == {"error": "unknown run"}


def test_sse_heartbeat(server):
    _, base = server
    with urllib.request.urlopen(base + "/api/stream/running1", timeout=5) as resp:
        _read_until(resp, b"data:", time.monotonic() + 5)
        _read_until(resp, b": keepalive", time.monotonic() + 5)


def test_sse_problem(server, tmp_path):
    _, base = server
    with urllib.request.urlopen(base + "/api/stream/running1", timeout=5) as resp:
        _read_until(resp, b"data:", time.monotonic() + 5)
        (run_dir(tmp_path, "running1") / STATE_FILE).write_text("{")
        _read_until(resp, b"event: problem", time.monotonic() + 5)


def test_read_only_filesystem(server, tmp_path):
    _, base = server
    before = _snapshot(tmp_path / ".harness")
    _get(base + "/api/runs")
    for rid in ("happy1", "blocked1", "running1"):
        _get(f"{base}/api/run/{rid}")
    _get(base + "/api/run/nope")
    _get(base + "/api/runs", method="POST")
    with urllib.request.urlopen(base + "/api/stream/running1", timeout=5) as resp:
        _read_until(resp, b"data:", time.monotonic() + 5)
    assert _snapshot(tmp_path / ".harness") == before


def test_favicon_and_tab_title(server):
    _, base = server
    code, headers, body = _get(base + "/favicon.svg")
    assert code == 200
    assert headers["Content-Type"] == "image/svg+xml"
    assert body.startswith(b"<svg") and b"#63df4e" in body
    _, _, page = _get(base + "/")
    assert b"<title>Javier Pro</title>" in page
    assert b'href="/favicon.svg?v=2"' in page
    code, _, _ = _get(base + "/favicon.svg?v=2")
    assert code == 200
