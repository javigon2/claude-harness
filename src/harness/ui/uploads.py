"""Image and text-file uploads for the office chat panels.

The page uploads raw image bytes (POST /api/upload); they are checked by their magic bytes,
never by a Content-Type or file name, and stored once per content as
<repo>/.harness/uploads/<sha256>.<ext>. The file name is the image id that messages carry
in `images`. Agents see the images through `attachment_note`: absolute paths they open with
their Read tool.

Text and code files (`TEXT_EXTS`) are stored the same way, as <sha256>.<ext>. Their kind comes
from the extension of the file name the page sends, and the bytes must be UTF-8 text without
NUL bytes of at most MAX_TEXT_BYTES. The original name is kept beside the file as
<id>.name, so the agent is told what the file was called."""

import hashlib
import os
import re
import tempfile
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from harness.ui.markdown import parse_markdown

UPLOADS_DIR = Path(".harness") / "uploads"
MAX_IMAGES = 4
MAX_IMAGE_BYTES = 8 * 1024 * 1024
IMAGE_TYPES: dict[str, str] = {
    "png": "image/png",
    "jpg": "image/jpeg",
    "gif": "image/gif",
    "webp": "image/webp",
}
MAX_TEXT_BYTES = 256 * 1024
TEXT_TYPE = "text/plain; charset=utf-8"
TEXT_EXTS = frozenset(
    "md txt rst log csv tsv json jsonl yaml yml toml ini cfg conf xml html css scss "
    "js jsx ts tsx mjs py pyi ipynb sh bash zsh sql java kt go rs c h cpp hpp cs rb php swift "
    "lua r scala dart gradle dockerfile makefile".split()
)
IMAGE_ID_RE = re.compile(
    r"^[0-9a-f]{64}\.(png|jpg|gif|webp|"
    + "|".join(sorted(TEXT_EXTS, key=len, reverse=True)).replace(".", r"\.")
    + ")$"
)
NOT_AN_IMAGE = "only png, jpeg, gif or webp images"
NOT_AN_ATTACHMENT = "only images or text and code files (md, txt, py, js, json, \u2026)"
NOT_TEXT = "the file is not UTF-8 text"
TEXT_TOO_LARGE = "file too large (max 256 KB)"
TOO_LARGE = "image too large (max 8 MB)"
EMPTY_UPLOAD = "empty upload"
BAD_IMAGES = "images must be a list of 1 to 4 image ids"


class UploadError(ValueError):
    """An upload the server refuses. `code` is the HTTP status it answers with; the message
    is the error text."""

    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code


def sniff_image(data: bytes) -> str | None:
    """The extension for the image type `data` starts with: "png" for
    b"\\x89PNG\\r\\n\\x1a\\n", "jpg" for b"\\xff\\xd8\\xff", "gif" for b"GIF87a" or
    b"GIF89a", "webp" for b"RIFF" + any 4 bytes + b"WEBP" (bytes 8-11). None for anything
    else, SVG included."""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if data.startswith(b"\xff\xd8\xff"):
        return "jpg"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "gif"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    return None


def store_image(repo: Path, data: bytes) -> str:
    """Store `data` and return its id f"{sha256 hex}.{ext}". Checks in this order: empty ->
    UploadError(400, EMPTY_UPLOAD); len(data) > MAX_IMAGE_BYTES -> UploadError(413,
    TOO_LARGE); sniff_image(data) is None -> UploadError(415, NOT_AN_IMAGE). Creates
    repo/UPLOADS_DIR (parents too); when the file already exists nothing is written;
    otherwise the bytes go to a temp file in the same folder (tempfile.mkstemp), then
    os.replace onto the final name; the temp file is removed if writing fails. OSError
    propagates."""
    if not data:
        raise UploadError(400, EMPTY_UPLOAD)
    if len(data) > MAX_IMAGE_BYTES:
        raise UploadError(413, TOO_LARGE)
    ext = sniff_image(data)
    if ext is None:
        raise UploadError(415, NOT_AN_IMAGE)

    return _write(repo, data, ext)


def _write(repo: Path, data: bytes, ext: str) -> str:
    """Store `data` as <sha256>.<ext> once; the id. Temp file in the folder, then os.replace."""
    image_id = f"{hashlib.sha256(data).hexdigest()}.{ext}"
    folder = Path(repo) / UPLOADS_DIR
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / image_id
    if target.exists():
        return image_id

    fd, tmp = tempfile.mkstemp(dir=folder)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        os.replace(tmp, target)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return image_id


def text_ext(name: str | None) -> str | None:
    """The TEXT_EXTS entry a file name ends in (`Dockerfile` and `Makefile` count whole), else
    None. Case-insensitive; only the last path part is looked at."""
    base = (name or "").replace("\\", "/").rsplit("/", 1)[-1].strip().lower()
    if base in ("dockerfile", "makefile"):
        return base
    ext = base.rsplit(".", 1)[-1] if "." in base else ""
    return ext if ext in TEXT_EXTS else None


def store_attachment(repo: Path, data: bytes, name: str | None = None) -> str:
    """An image (by its magic bytes) goes through store_image. Anything else needs a `name`
    with a TEXT_EXTS extension (else UploadError 415 NOT_AN_ATTACHMENT), at most
    MAX_TEXT_BYTES (413) of UTF-8 text without NUL bytes (415 NOT_TEXT); it is stored as
    <sha256>.<ext> with its cleaned base name beside it. Returns the id."""
    if sniff_image(data) is not None:
        return store_image(repo, data)
    ext = text_ext(name)
    if ext is None or not data:
        if name and data:
            raise UploadError(415, NOT_AN_ATTACHMENT)
        return store_image(repo, data)
    if len(data) > MAX_TEXT_BYTES:
        raise UploadError(413, TEXT_TOO_LARGE)
    try:
        data.decode("utf-8")
    except UnicodeDecodeError:
        raise UploadError(415, NOT_TEXT) from None
    if b"\0" in data:
        raise UploadError(415, NOT_TEXT)
    file_id = _write(repo, data, ext)
    label = re.sub(r"[^\w.\- ]", "_", (name or "").replace("\\", "/").rsplit("/", 1)[-1])[:100]
    (Path(repo) / UPLOADS_DIR / f"{file_id}.name").write_text(label, encoding="utf-8")
    return file_id


def file_name(repo: Path, file_id: str) -> str | None:
    """The cleaned name stored with a text file, or None."""
    try:
        text = (Path(repo) / UPLOADS_DIR / f"{file_id}.name").read_text(encoding="utf-8")
    except OSError:
        return None
    return text or None


def image_path(repo: Path, image_id: str) -> Path | None:
    """Path(repo) / UPLOADS_DIR / image_id when IMAGE_ID_RE.fullmatch(image_id) and that
    path is a regular file; None otherwise (also for a non-str id). Never raises."""
    if not isinstance(image_id, str) or not IMAGE_ID_RE.fullmatch(image_id):
        return None
    path = Path(repo) / UPLOADS_DIR / image_id
    return path if path.is_file() else None


def preview_document(repo: Path, file_id: str) -> dict[str, Any] | None:
    """What the page's reader shows for an uploaded text file: {id, name, ext, text, blocks},
    where `blocks` is the parsed Markdown tree for a .md file and None for any other text
    file. None for an image, an unknown id, or bytes that are not UTF-8. Never raises."""
    path = image_path(repo, file_id)
    if path is None:
        return None
    ext = path.name.rsplit(".", 1)[1]
    if ext in IMAGE_TYPES:
        return None
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    return {
        "id": file_id,
        "name": file_name(repo, file_id) or file_id,
        "ext": ext,
        "text": text,
        "blocks": parse_markdown(text) if ext == "md" else None,
    }


def check_image_ids(repo: Path, value: object) -> list[str]:
    """The ids of a request's `images` field, duplicates dropped, first-seen order kept.
    Raises ValueError(BAD_IMAGES) unless `value` is a list of 1 to MAX_IMAGES items that are
    all str (the length is checked before dropping duplicates), and
    ValueError(f"unknown image id: {image_id[:80]}") for the first id image_path rejects."""
    if (
        not isinstance(value, list)
        or not 1 <= len(value) <= MAX_IMAGES
        or not all(isinstance(item, str) for item in value)
    ):
        raise ValueError(BAD_IMAGES)
    ids: list[str] = []
    for item in value:
        if item not in ids:
            ids.append(item)
    for image_id in ids:
        if image_path(repo, image_id) is None:
            raise ValueError(f"unknown image id: {image_id[:80]}")
    return ids


def image_ids(payload: dict[str, Any]) -> list[str]:
    """payload.get("images") filtered to the str items IMAGE_ID_RE matches, in order; [] when
    it is missing or not a list. For rebuilding views from the logs; never raises."""
    value = payload.get("images")
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str) and IMAGE_ID_RE.fullmatch(item)]


def attachment_note(repo: Path, ids: Sequence[str]) -> str:
    """What is appended to the text an agent receives: "" when `ids` is empty, else
    "\\n\\nAttached images: " followed by the absolute path of each image id,
    str(Path(repo).resolve() / UPLOADS_DIR / image_id), joined with ", ", and for text files
    "\\n\\nAttached files: " with each path and, when known, " (name)"."""
    if not ids:
        return ""
    root = Path(repo).resolve()
    images: list[str] = []
    files: list[str] = []
    for image_id in ids:
        path = str(root / UPLOADS_DIR / image_id)
        if image_id.rsplit(".", 1)[-1] in IMAGE_TYPES:
            images.append(path)
        else:
            name = file_name(repo, image_id)
            files.append(f"{path} ({name})" if name else path)
    note = ""
    if images:
        note += "\n\nAttached images: " + ", ".join(images)
    if files:
        note += "\n\nAttached files: " + ", ".join(files)
    return note
