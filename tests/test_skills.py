from pathlib import Path

SKILLS = Path(__file__).resolve().parents[1] / ".claude" / "skills"


def _quick_section() -> str:
    text = (SKILLS / "plan-stage" / "SKILL.md").read_text(encoding="utf-8")
    assert "\n## Quick mode\n" in text
    return text.split("\n## Quick mode\n", 1)[1].split("\n## ", 1)[0]


def test_plan_stage_has_a_quick_mode_section():
    section = _quick_section()
    assert "no PROJECT_PLAN.md" in section
    assert "Ask nothing" in section
    assert "2–8" in section
    assert '"stage": "quick"' in section


def test_quick_mode_is_the_last_section():
    text = (SKILLS / "plan-stage" / "SKILL.md").read_text(encoding="utf-8")
    assert text.endswith("to overrule.\n")
    assert "\n## " not in text.split("\n## Quick mode\n", 1)[1]


def test_front_matter_is_unchanged():
    text = (SKILLS / "plan-stage" / "SKILL.md").read_text(encoding="utf-8")
    assert text.startswith("---\nname: plan-stage\n")


def test_plan_project_skill_is_untouched():
    text = (SKILLS / "plan-project" / "SKILL.md").read_text(encoding="utf-8")
    assert "## Quick mode" not in text
