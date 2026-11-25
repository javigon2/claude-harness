"""Serve repo files the page shows or downloads (GET /api/asset, GET /api/report-bundle).

Same untrusted-path rules as `repofile.py`: the path is resolved (symlinks included) and must
land inside the repo, outside `.git`, `.env*` files and `.harness` (except `.harness/plans`).
Only extensions of `ASSET_TYPES` are served. Pure reads; nothing is written."""

import io
import zipfile
from pathlib import Path

MAX_ASSET_BYTES = 15 * 1024 * 1024
MAX_BUNDLE_BYTES = 50 * 1024 * 1024
MAX_PATH_CHARS = 400

ASSET_TYPES: dict[str, str] = {
    "png": "image/png",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "gif": "image/gif",
    "webp": "image/webp",
    "svg": "image/svg+xml",
    "csv": "text/csv; charset=utf-8",
    "md": "text/markdown; charset=utf-8",
    "json": "application/json",
    "txt": "text/plain; charset=utf-8",
}


def _ext(name: str) -> str | None:
    ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    return ext if ext in ASSET_TYPES else None


def _inside(root: Path, target: Path) -> Path | None:
    """`target` relative to `root` when it passes the path rules, else None."""
    try:
        rel = target.relative_to(root)
    except ValueError:
        return None
    parts = rel.parts
    if not parts or ".git" in parts or any(p.startswith(".env") for p in parts):
        return None
    if parts[0] == ".harness" and not (len(parts) > 1 and parts[1] == "plans"):
        return None
    return rel


def _resolve(repo: Path, raw: object) -> tuple[Path, Path, Path] | None:
    """(root, target, rel) for a raw path string, or None."""
    if not isinstance(raw, str) or not raw.strip() or len(raw) > MAX_PATH_CHARS or "\0" in raw:
        return None
    try:
        root = Path(repo).resolve()
        target = (root / raw.strip()).resolve()
    except (OSError, ValueError, RuntimeError):
        return None
    rel = _inside(root, target)
    return None if rel is None else (root, target, rel)


def read_asset(repo: Path, raw: object) -> tuple[bytes, str, str] | None:
    """(bytes, content type, file name) of an allow-listed file of the repo, or None.
    Never raises."""
    found = _resolve(repo, raw)
    if found is None:
        return None
    _, target, _ = found
    ext = _ext(target.name)
    if ext is None:
        return None
    try:
        if not target.is_file() or target.stat().st_size > MAX_ASSET_BYTES:
            return None
        return target.read_bytes(), ASSET_TYPES[ext], target.name
    except OSError:
        return None


def report_bundle(repo: Path, raw_dir: object) -> tuple[bytes, str] | None:
    """(zip bytes, '<dirname>.zip') of the allow-listed files under a repo directory, or None
    when it is not a directory, holds nothing to send or is over MAX_BUNDLE_BYTES. Never
    raises."""
    found = _resolve(repo, raw_dir)
    if found is None:
        return None
    root, base, _ = found
    try:
        if not base.is_dir():
            return None
        files: list[tuple[Path, str]] = []
        total = 0
        for p in sorted(base.rglob("*")):
            real = p.resolve()  # a symlink pointing out of the repo is skipped
            if _inside(root, real) is None or not real.is_file() or _ext(p.name) is None:
                continue
            size = real.stat().st_size
            total += size
            if total > MAX_BUNDLE_BYTES:
                return None
            files.append((real, p.relative_to(base).as_posix()))
        if not files:
            return None
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for real, arcname in files:
                zf.write(real, arcname)
        return buf.getvalue(), f"{base.name or 'report'}.zip"
    except (OSError, ValueError, RuntimeError):
        return None
