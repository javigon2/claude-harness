from pathlib import Path

from harness.worker import PROGRESS_DETAIL_MAX, describe_tool


def test_bash_description_becomes_summary(tmp_path: Path) -> None:
    got = describe_tool(
        "Bash",
        {"command": "cd /a && uv run pytest -q", "description": "  Run the tests\nmore"},
        tmp_path,
    )
    assert got == {"tool": "Bash", "detail": "uv run pytest -q", "summary": "Run the tests"}


def test_blank_or_non_str_description_gives_no_summary(tmp_path: Path) -> None:
    assert describe_tool("Bash", {"command": "ls", "description": "  "}, tmp_path) == {
        "tool": "Bash",
        "detail": "ls",
    }
    assert describe_tool("Bash", {"command": "ls", "description": 3}, tmp_path) == {
        "tool": "Bash",
        "detail": "ls",
    }


def test_description_without_command(tmp_path: Path) -> None:
    got = describe_tool("Bash", {"description": "List files"}, tmp_path)
    assert got == {"tool": "Bash", "detail": "", "summary": "List files"}


def test_long_description_is_cut(tmp_path: Path) -> None:
    got = describe_tool("Bash", {"command": "ls", "description": "d" * 200}, tmp_path)
    summary = got["summary"]
    assert len(summary) == PROGRESS_DETAIL_MAX
    assert summary.endswith("…")


def test_non_bash_ignores_description(tmp_path: Path) -> None:
    got = describe_tool("Read", {"file_path": "/etc/hosts", "description": "x"}, tmp_path)
    assert got == {"tool": "Read", "detail": "/etc/hosts"}
