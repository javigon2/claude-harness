from typer.testing import CliRunner

from harness.cli import app
from harness.config import MODEL_IDS, TIER_LADDER

runner = CliRunner()


def test_help_lists_commands():
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    for name in ("run", "status", "report", "validate"):
        assert name in result.output


def test_every_tier_has_a_model():
    assert TIER_LADDER == ("haiku", "sonnet", "opus")
    assert set(MODEL_IDS) == set(TIER_LADDER)
