from pathlib import Path

README = Path(__file__).parent.parent / "README.md"

REQUIRED = [
    "## Office page: images and diagrams",
    ".harness/uploads/",
    "POST /api/upload",
    "GET /api/upload/<id>",
    "8 MB",
    "Read tool",
    "### Diagrams",
]


def test_readme_has_required_strings():
    assert README.exists()
    text = README.read_text(encoding="utf-8")
    for needle in REQUIRED:
        assert needle in text, f"missing: {needle!r}"
