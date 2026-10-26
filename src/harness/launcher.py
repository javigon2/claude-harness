"""`agent-orchestrator`: serve the office from any folder, then do everything on the page.

It starts a Host (no project yet, or the one asked for) behind the office server on 127.0.0.1
and opens the page in the browser. Ctrl-C stops the server and leaves launched runs going.
`harness` and its subcommands are unchanged."""

import os
import secrets
import socket
import sys
import webbrowser
from pathlib import Path
from typing import Annotated, NoReturn

import typer

from harness.planner import PlannerError, check_skills
from harness.ui.host import Host, HostConflict
from harness.ui.server import make_server

PORT_RANGE = range(8765, 8776)


class LauncherError(RuntimeError):
    """The launcher cannot start. The message is a sentence for the user."""


app = typer.Typer(add_completion=False, help="Serve the agent-harness office page.")


def _port_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


def pick_port(preferred: int | None) -> int:
    """`preferred` when given (a busy one fails when the server binds it), else the first
    free port in PORT_RANGE. Raises LauncherError `No free port in 8765-8775`."""
    if preferred is not None:
        return preferred
    for port in PORT_RANGE:
        if _port_free(port):
            return port
    raise LauncherError(f"No free port in {PORT_RANGE.start}-{PORT_RANGE.stop - 1}")


def startup_project(project: Path | None, cwd: Path) -> Path | None:
    """The folder to open at start: `--project` (relative to `cwd`), else `cwd` when it is a
    git repo, else None (the page starts at the folder browser)."""
    if project is not None:
        return Path(os.path.abspath(Path(cwd) / project.expanduser()))
    if (Path(cwd) / ".git").exists():
        return Path(cwd)
    return None


def restart_argv(
    port: int,
    project: Path | None,
    fake: bool,
    fake_delay: float,
    max_cost: float | None,
) -> list[str]:
    """The full argv (program first) that starts this launcher again on the same port with
    the same project, without opening the browser: for os.execv(sys.executable, argv)."""
    argv = [sys.executable, "-m", "harness.launcher", "--port", str(port), "--no-browser"]
    if project is not None:
        argv += ["--project", str(project)]
    if fake:
        argv += ["--fake", "--fake-delay", str(fake_delay)]
    if max_cost is not None:
        argv += ["--max-cost", str(max_cost)]
    return argv


def _fail(message: str) -> NoReturn:
    typer.echo(message, err=True)
    raise typer.Exit(code=1)


@app.command()
def serve(
    port: Annotated[
        int | None,
        typer.Option(
            "--port",
            min=1,
            max=65535,
            help="Port on 127.0.0.1 (default: 8765, or the next free one up to 8775).",
        ),
    ] = None,
    project: Annotated[
        Path | None,
        typer.Option("--project", help="Folder to open (default: the current folder if a repo)."),
    ] = None,
    fake: Annotated[
        bool, typer.Option("--fake", help="Scripted planner and fake workers: no model.")
    ] = False,
    fake_delay: Annotated[
        float | None,
        typer.Option(
            "--fake-delay", min=0, help="With --fake: seconds per scripted step (default 2)."
        ),
    ] = None,
    max_cost: Annotated[
        float | None,
        typer.Option(
            "--max-cost", help="Default --max-cost USD for the runs started from the page."
        ),
    ] = None,
    no_browser: Annotated[
        bool, typer.Option("--no-browser", help="Do not open the page in the browser.")
    ] = False,
) -> None:
    """Serve the office page on 127.0.0.1 and open it."""
    if fake_delay is not None and not fake:
        _fail("--fake-delay requires --fake")
    if max_cost is not None and max_cost <= 0:
        _fail("--max-cost must be greater than 0")
    if not fake:
        try:
            check_skills()
        except PlannerError as err:
            _fail(str(err))
    try:
        chosen = pick_port(port)
    except LauncherError as err:
        _fail(str(err))
    delay = 2.0 if fake_delay is None else fake_delay
    cwd = Path.cwd()
    host = Host(port=chosen, cwd=cwd, fake=fake, fake_delay=delay, max_cost=max_cost)
    wanted = startup_project(project, cwd)
    if wanted is not None:
        try:
            host.open_project(wanted)
        except HostConflict as err:
            typer.echo(f"Cannot open {wanted}: {err}", err=True)
    try:
        server = make_server(cwd, chosen, host=host, token=secrets.token_urlsafe(32))
    except OSError as err:
        host.close()
        _fail(f"Cannot listen on 127.0.0.1:{chosen}: {err}")
    url = f"http://127.0.0.1:{chosen}/"
    opened = host.current()[0]
    typer.echo(
        f"Serving {url} (project: {opened if opened is not None else 'none'}; Ctrl-C to stop)"
    )
    if not no_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        last_project = host.current()[0]
        still_running = host.close()
        server.server_close()
    if still_running:
        typer.echo(
            f"Run {still_running} keeps running; check it in the page or with: "
            f"harness status --repo {last_project} --run-id {still_running}"
        )
    if host.restart_requested:
        os.execv(sys.executable, restart_argv(chosen, last_project, fake, delay, max_cost))


def main() -> None:
    """The `agent-orchestrator` entry point."""
    app()


if __name__ == "__main__":
    main()
