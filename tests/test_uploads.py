import hashlib
from pathlib import Path

import pytest

from harness.ui.uploads import (
    BAD_IMAGES,
    EMPTY_UPLOAD,
    MAX_IMAGE_BYTES,
    NOT_AN_IMAGE,
    TOO_LARGE,
    UploadError,
    attachment_note,
    check_image_ids,
    image_ids,
    image_path,
    sniff_image,
    store_image,
)

PNG_HEADER = b"\x89PNG\r\n\x1a\n"
PNG = PNG_HEADER + b"\0" * 32
JPEG = b"\xff\xd8\xff\xe0" + b"\0" * 32
GIF = b"GIF89a" + b"\0" * 32
WEBP = b"RIFF\0\0\0\0WEBPVP8 " + b"\0" * 32
SVG = b"<svg xmlns='http://www.w3.org/2000/svg'></svg>"


def _uploads(tmp_path: Path) -> Path:
    return tmp_path / ".harness" / "uploads"


def test_sniff_image_returns_type_for_each_header():
    assert sniff_image(PNG) == "png"
    assert sniff_image(JPEG) == "jpg"
    assert sniff_image(b"GIF87a" + b"\0" * 8) == "gif"
    assert sniff_image(GIF) == "gif"
    assert sniff_image(WEBP) == "webp"


def test_sniff_image_returns_none_for_everything_else():
    for data in [SVG, b"hello", b"RIFF\0\0\0\0WAVE", b""]:
        assert sniff_image(data) is None


def test_store_image_writes_bytes_under_sha256_name(tmp_path):
    image_id = store_image(tmp_path, PNG)
    assert image_id == f"{hashlib.sha256(PNG).hexdigest()}.png"
    assert (_uploads(tmp_path) / image_id).read_bytes() == PNG


def test_store_image_same_bytes_twice_leaves_one_file(tmp_path):
    first = store_image(tmp_path, PNG)
    second = store_image(tmp_path, PNG)
    assert first == second
    assert sorted(p.name for p in _uploads(tmp_path).iterdir()) == [first]


def test_store_image_refuses_empty_upload(tmp_path):
    with pytest.raises(UploadError) as err:
        store_image(tmp_path, b"")
    assert err.value.code == 400
    assert str(err.value) == EMPTY_UPLOAD
    assert not _uploads(tmp_path).exists()


def test_store_image_refuses_too_large(tmp_path):
    with pytest.raises(UploadError) as err:
        store_image(tmp_path, PNG_HEADER + b"\0" * MAX_IMAGE_BYTES)
    assert err.value.code == 413
    assert str(err.value) == TOO_LARGE
    assert not _uploads(tmp_path).exists()


def test_store_image_refuses_svg(tmp_path):
    with pytest.raises(UploadError) as err:
        store_image(tmp_path, SVG)
    assert err.value.code == 415
    assert str(err.value) == NOT_AN_IMAGE
    assert not _uploads(tmp_path).exists()


def test_image_path_returns_path_only_for_stored_ids(tmp_path):
    image_id = store_image(tmp_path, PNG)
    assert image_path(tmp_path, image_id) == _uploads(tmp_path) / image_id
    assert image_path(tmp_path, "../x.png") is None
    assert image_path(tmp_path, "x.png") is None
    assert image_path(tmp_path, image_id.upper()) is None
    assert image_path(tmp_path, image_id[:-3] + "svg") is None
    assert image_path(tmp_path, "a" * 64 + ".png") is None
    dir_id = "b" * 64 + ".png"
    (_uploads(tmp_path) / dir_id).mkdir()
    assert image_path(tmp_path, dir_id) is None
    assert image_path(tmp_path, 5) is None


def test_check_image_ids_drops_duplicates(tmp_path):
    image_id = store_image(tmp_path, PNG)
    assert check_image_ids(tmp_path, [image_id, image_id]) == [image_id]


def test_check_image_ids_rejects_bad_shapes(tmp_path):
    image_id = store_image(tmp_path, PNG)
    for value in [[], [image_id] * 5, "x", [1], None]:
        with pytest.raises(ValueError) as err:
            check_image_ids(tmp_path, value)
        assert str(err.value) == BAD_IMAGES


def test_check_image_ids_rejects_unknown_ids(tmp_path):
    with pytest.raises(ValueError) as err:
        check_image_ids(tmp_path, ["../etc/passwd"])
    assert str(err.value) == "unknown image id: ../etc/passwd"

    with pytest.raises(ValueError) as err:
        check_image_ids(tmp_path, ["a" * 200])
    assert str(err.value) == "unknown image id: " + "a" * 80


def test_image_ids_keeps_matching_strings_in_order():
    first = "a" * 64 + ".png"
    second = "b" * 64 + ".gif"
    payload = {"images": [first, 5, "x", "../x.png", second]}
    assert image_ids(payload) == [first, second]
    for payload in [{}, {"images": "x"}, {"images": None}]:
        assert image_ids(payload) == []


def test_attachment_note_lists_absolute_paths(tmp_path):
    assert attachment_note(tmp_path, []) == ""
    first = "a" * 64 + ".png"
    second = "b" * 64 + ".jpg"
    base = tmp_path.resolve() / ".harness" / "uploads"
    expected = f"\n\nAttached images: {str(base / first)}, {str(base / second)}"
    assert attachment_note(tmp_path, [first, second]) == expected


def test_text_files_are_stored_with_their_name_and_noted(tmp_path):
    from harness.ui.uploads import file_name, image_path, store_attachment, text_ext

    assert text_ext("src/App.TSX") == "tsx" and text_ext("Dockerfile") == "dockerfile"
    assert text_ext("run.exe") is None and text_ext("noext") is None and text_ext(None) is None
    file_id = store_attachment(tmp_path, b"print('hi')\n", "dir/../my script.py")
    assert file_id.endswith(".py") and image_path(tmp_path, file_id) is not None
    assert file_name(tmp_path, file_id) == "my script.py"
    note = attachment_note(tmp_path, [file_id])
    assert note.startswith("\n\nAttached files: ") and note.endswith("(my script.py)")
    assert "Attached images" not in note
