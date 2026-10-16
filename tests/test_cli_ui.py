from pathlib import Path

from typer.testing import CliRunner

from harness.cli import app

runner = CliRunner()


class FakeServer:
    def __init__(self, interrupt: bool = False) -> None:
        self.interrupt = interrupt
        self.closed = False
        self.served = False
        self.kwargs: dict = {}

    def serve_forever(self) -> None:
        self.served = True
        if self.interrupt:
            raise KeyboardInterrupt

    def server_close(self) -> None:
        self.closed = True


def _patch_server(monkeypatch, server: FakeServer) -> list[tuple[Path, int]]:
    calls: list[tuple[Path, int]] = []

    def fake_make(repo: Path, port: int, **kwargs) -> FakeServer:
        calls.append((repo, port))
        server.kwargs = kwargs
        return server

    monkeypatch.setattr("harness.cli.make_server", fake_make)
    return calls


def test_ui_default_serves_and_closes(tmp_path, monkeypatch):
    server = FakeServer()
    calls = _patch_server(monkeypatch, server)
    result = runner.invoke(app, ["ui", "--read-only", "--repo", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert "Serving http://127.0.0.1:8765/ (read-only; Ctrl-C to stop)" in result.output
    assert calls == [(tmp_path.resolve(), 8765)]
    assert server.served
    assert server.closed


def test_ui_port_and_run_id_in_url(tmp_path, monkeypatch):
    server = FakeServer()
    calls = _patch_server(monkeypatch, server)
    result = runner.invoke(
        app, ["ui", "--read-only", "--repo", str(tmp_path), "--port", "9000", "--run-id", "r1"]
    )
    assert result.exit_code == 0, result.output
    assert "Serving http://127.0.0.1:9000/#r1 (read-only; Ctrl-C to stop)" in result.output
    assert calls == [(tmp_path.resolve(), 9000)]


def test_ui_ctrl_c_exits_cleanly(tmp_path, monkeypatch):
    server = FakeServer(interrupt=True)
    _patch_server(monkeypatch, server)
    result = runner.invoke(app, ["ui", "--read-only", "--repo", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert server.closed


def test_ui_port_in_use_fails_clearly(tmp_path, monkeypatch):
    def busy(repo: Path, port: int, **kwargs) -> FakeServer:
        raise OSError(48, "Address already in use")

    monkeypatch.setattr("harness.cli.make_server", busy)
    result = runner.invoke(app, ["ui", "--read-only", "--repo", str(tmp_path)])
    assert result.exit_code == 1
    assert "Cannot listen on 127.0.0.1:8765: [Errno 48] Address already in use" in result.output


def test_ui_repo_not_a_directory_fails(tmp_path, monkeypatch):
    server = FakeServer()
    calls = _patch_server(monkeypatch, server)
    not_dir = tmp_path / "file.txt"
    not_dir.write_text("x")
    result = runner.invoke(app, ["ui", "--repo", str(not_dir)])
    assert result.exit_code == 1
    assert "Not a directory:" in result.output
    assert calls == []


def test_ui_port_out_of_range_is_usage_error(tmp_path, monkeypatch):
    _patch_server(monkeypatch, FakeServer())
    for port in ("0", "70000"):
        result = runner.invoke(app, ["ui", "--repo", str(tmp_path), "--port", port])
        assert result.exit_code == 2, result.output


def test_ui_help_mentions_port():
    result = runner.invoke(app, ["ui", "--help"])
    assert result.exit_code == 0
    assert "--port" in result.output


def test_ui_read_only_makes_no_office(tmp_path, monkeypatch):
    server = FakeServer()
    _patch_server(monkeypatch, server)
    result = runner.invoke(app, ["ui", "--read-only", "--repo", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert server.kwargs == {}
    assert not (tmp_path / ".harness").exists()


def test_ui_interactive_builds_an_office_and_cleans_up(tmp_path, monkeypatch):
    server = FakeServer()
    calls = _patch_server(monkeypatch, server)
    seen: dict = {}
    real_serve = server.serve_forever

    def serve() -> None:
        seen["lock"] = (tmp_path / ".harness" / "office.lock").is_file()
        real_serve()

    server.serve_forever = serve  # type: ignore[method-assign]
    result = runner.invoke(app, ["ui", "--fake", "--repo", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert "Serving http://127.0.0.1:8765/ (interactive; Ctrl-C to stop)" in result.output
    assert calls == [(tmp_path.resolve(), 8765)]
    office, token = server.kwargs["office"], server.kwargs["token"]
    assert office.fake and office.fake_delay == 2.0 and office.max_cost is None
    assert len(token) >= 32
    assert seen["lock"]
    assert not (tmp_path / ".harness" / "office.lock").exists()
    assert server.closed


def test_ui_interactive_passes_fake_delay_and_max_cost(tmp_path, monkeypatch):
    server = FakeServer()
    _patch_server(monkeypatch, server)
    args = ["ui", "--fake", "--fake-delay", "0.5", "--max-cost", "3", "--repo", str(tmp_path)]
    result = runner.invoke(app, args)
    assert result.exit_code == 0, result.output
    assert server.kwargs["office"].fake_delay == 0.5
    assert server.kwargs["office"].max_cost == 3.0


def test_ui_flag_errors(tmp_path, monkeypatch):
    _patch_server(monkeypatch, FakeServer())
    base = ["ui", "--repo", str(tmp_path)]
    cases = [
        (["--fake-delay", "1"], "--fake-delay requires --fake"),
        (["--fake", "--max-cost", "0"], "--max-cost must be greater than 0"),
        (["--read-only", "--fake"], "drop --read-only"),
    ]
    for extra, message in cases:
        result = runner.invoke(app, [*base, *extra])
        assert result.exit_code == 1, extra
        assert message in result.output
    assert not (tmp_path / ".harness").exists()


def test_ui_refuses_when_another_ui_owns_the_repo(tmp_path, monkeypatch):
    import json
    import os

    lock = tmp_path / ".harness" / "office.lock"
    lock.parent.mkdir()
    lock.write_text(json.dumps({"pid": os.getpid(), "port": 9999, "started": 0}))
    server = FakeServer()
    calls = _patch_server(monkeypatch, server)
    result = runner.invoke(app, ["ui", "--fake", "--repo", str(tmp_path)])
    assert result.exit_code == 1
    assert "Another harness ui" in result.output
    assert "--read-only" in result.output
    assert calls == []
    assert lock.is_file()  # not ours, left alone


def test_ui_port_in_use_releases_the_lock(tmp_path, monkeypatch):
    def busy(repo: Path, port: int, **kwargs) -> FakeServer:
        raise OSError(48, "Address already in use")

    monkeypatch.setattr("harness.cli.make_server", busy)
    result = runner.invoke(app, ["ui", "--fake", "--repo", str(tmp_path)])
    assert result.exit_code == 1
    assert "Cannot listen" in result.output
    assert not (tmp_path / ".harness" / "office.lock").exists()
