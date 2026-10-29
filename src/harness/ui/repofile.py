"""Read one text file of the project for the page's reader (GET /api/file?path=...).

The path comes from agent messages, so it is untrusted: it is resolved (symlinks included)
and must land inside the repo, name a text or code file (`TEXT_EXTS`) of at most
MAX_TEXT_BYTES of UTF-8, and stay out of `.git`, `.harness` (except `.harness/plans`) and
`.env*` files. Pure reads; nothing is written."""

from pathlib import Path
from typing import Any

from harness.ui.markdown import parse_markdown
from harness.ui.uploads import MAX_TEXT_BYTES, text_ext

MAX_PATH_CHARS = 400


def read_repo_file(repo: Path, raw: object) -> dict[str, Any] | None:
    """{id, name, ext, text, blocks} (the shape of uploads.preview_document, `id` is the
    path relative to the repo) for `raw`, a path relative to the repo or absolute inside it;
    None when it is anything else. Never raises."""
    if not isinstance(raw, str) or not raw.strip() or len(raw) > MAX_PATH_CHARS or "\0" in raw:
        return None
    try:
        root = Path(repo).resolve()
        target = (root / raw.strip()).resolve()
        rel = target.relative_to(root)
    except (OSError, ValueError, RuntimeError):
        return None
    parts = rel.parts
    if not parts or ".git" in parts or target.name.startswith(".env"):
        return None
    if parts[0] == ".harness" and not (len(parts) > 1 and parts[1] == "plans"):
        return None
    ext = text_ext(target.name)
    if ext is None:
        return None
    try:
        if not target.is_file() or target.stat().st_size > MAX_TEXT_BYTES:
            return None
        data = target.read_bytes()
        text = data.decode("utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    if b"\0" in data:
        return None
    return {
        "id": rel.as_posix(),
        "name": target.name,
        "ext": ext,
        "text": text,
        "blocks": parse_markdown(text) if ext == "md" else None,
    }
