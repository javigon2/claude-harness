"""POST /api/upload and GET /api/upload/<id> of the office server."""

import http.client
import json
import re
import threading
from pathlib import Path

import pytest
from git_helpers import init_repo
from ui_fixtures import write_happy_run

from harness.ui.office import Office
from harness.ui.server import make_server
from harness.ui.uploads import MAX_IMAGE_BYTES, NOT_AN_IMAGE, UPLOADS_DIR, store_image

PNG = b"\x89PNG\r\n\x1a\n" + b"\0" * 32
SVG = b"<svg xmlns='http://www.w3.org/2000/svg'></svg>"
UPLOAD = "/api/upload"


class Site:
    """A running server plus small HTTP helpers that control every header."""

    def __init__(self, repo: Path, office: Office | None) -> None:
        self.repo, self.office = repo, office
        self.server = make_server(repo, 0, poll_interval=0.05, heartbeat_s=0.2, office=office)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.port = self.server.server_address[1]
        self.origin = f"http://127.0.0.1:{self.port}"

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(5)
        if self.office is not None:
            self.office.close()

    def request(self, method, path, body=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            conn.request(method, path, body=body, headers=headers or {})
            r = conn.getresponse()
            data = r.read()
            return r.status, {k.lower(): v for k, v in r.getheaders()}, data
        finally:
            conn.close()

    @property
    def token(self) -> str:
        _, _, page = self.request("GET", "/")
        m = re.search(rb'<meta name="harness-token" content="([^"]*)">', page)
        assert m is not None
        return m.group(1).decode()

    def post(self, path, body=None, *, token="ok", origin="ok", ctype="application/json", raw=None):
        headers = {"Content-Type": ctype} if ctype else {}
        if token == "ok":
            token = self.token
        if token is not None:
            headers["X-Harness-Token"] = token
        if origin == "ok":
            origin = self.origin
        if origin is not None:
            headers["Origin"] = origin
        data = raw if raw is not None else json.dumps(body).encode()
        status, hdrs, out = self.request("POST", path, data, headers)
        return status, hdrs, (json.loads(out) if out else None)


@pytest.fixture
def site(tmp_path):
    repo = init_repo(tmp_path / "repo")
    office = Office(repo, fake=True, fake_delay=0)
    s = Site(repo, office)
    yield s
    s.stop()


@pytest.fixture
def read_only_site(tmp_path):
    write_happy_run(tmp_path)
    s = Site(tmp_path, None)
    yield s
    s.stop()


def stored(site: Site) -> list[Path]:
    d = site.repo / UPLOADS_DIR
    return list(d.iterdir()) if d.exists() else []


def test_upload_png(site):
    status, _, out = site.post(UPLOAD, raw=PNG, ctype="application/octet-stream")
    assert status == 200
    assert out["type"] == "image/png" and out["bytes"] == len(PNG)
    assert out["id"].endswith(".png")
    assert (site.repo / UPLOADS_DIR / out["id"]).read_bytes() == PNG


def test_spoofed_content_type_is_refused(site):
    status, _, out = site.post(UPLOAD, raw=SVG, ctype="image/png")
    assert status == 415 and out["error"] == NOT_AN_IMAGE
    assert stored(site) == []


def test_content_type_is_ignored(site):
    status, _, _ = site.post(UPLOAD, raw=PNG, ctype="text/plain")
    assert status == 200


def test_empty_upload(site):
    status, _, out = site.post(UPLOAD, raw=b"", ctype="application/octet-stream")
    assert status == 400 and out["error"] == "empty upload"


def test_too_large(site):
    conn = http.client.HTTPConnection("127.0.0.1", site.port, timeout=10)
    try:
        conn.putrequest("POST", UPLOAD)
        conn.putheader("Origin", site.origin)
        conn.putheader("X-Harness-Token", site.token)
        conn.putheader("Content-Length", str(MAX_IMAGE_BYTES + 1))
        conn.endheaders()
        assert conn.getresponse().status == 413
    finally:
        conn.close()


def test_missing_token_and_bad_origin(site):
    status, _, _ = site.post(UPLOAD, raw=PNG, token=None)
    assert status == 403
    status, _, _ = site.post(UPLOAD, raw=PNG, origin="http://evil.example")
    assert status == 403
    assert stored(site) == []


def test_read_only_post(read_only_site):
    status, _, _ = read_only_site.post(UPLOAD, raw=PNG)
    assert status == 405


def test_get_image(site):
    image_id = store_image(site.repo, PNG)
    status, hdrs, data = site.request("GET", f"{UPLOAD}/{image_id}")
    assert status == 200 and data == PNG
    assert hdrs["content-type"] == "image/png"
    assert hdrs["x-content-type-options"] == "nosniff"
    assert "private" in hdrs["cache-control"]


def test_get_image_read_only(read_only_site):
    image_id = store_image(read_only_site.repo, PNG)
    status, hdrs, data = read_only_site.request("GET", f"{UPLOAD}/{image_id}")
    assert status == 200 and data == PNG
    assert hdrs["content-type"] == "image/png"


@pytest.mark.parametrize("tail", ["..%2F..%2Fetc", "%2e%2e", "a" * 64 + ".svg", "0" * 64 + ".png"])
def test_get_unknown(site, tail):
    status, _, _ = site.request("GET", f"{UPLOAD}/{tail}")
    assert status == 404
