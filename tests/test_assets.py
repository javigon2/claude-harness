"""GET /api/asset and GET /api/report-bundle, and the path rules of ui/assets.py."""

import io
import zipfile
from urllib.parse import quote

import pytest
from git_helpers import init_repo
from test_ui_upload_server import PNG, SVG, Site  # noqa: F401  (fixtures below reuse Site)

from harness.ui.assets import MAX_ASSET_BYTES, read_asset, report_bundle
from harness.ui.office import Office


@pytest.fixture
def site(tmp_path):
    repo = init_repo(tmp_path / "repo")
    s = Site(repo, Office(repo, fake=True, fake_delay=0))
    (repo / "reports" / "r1" / "sub").mkdir(parents=True)
    (repo / "reports" / "r1" / "a.png").write_bytes(PNG)
    (repo / "reports" / "r1" / "sub" / "t.csv").write_text("a,b\n1,2\n")
    (repo / "reports" / "r1" / "run.py").write_text("print(1)")
    (repo / "reports" / "r1" / "x.svg").write_bytes(SVG)
    (repo / ".harness" / "plans").mkdir(parents=True)
    (repo / ".harness" / "x.png").write_bytes(PNG)
    (repo / ".harness" / "plans" / "p.png").write_bytes(PNG)
    (repo / ".env.png").write_bytes(PNG)
    (repo / ".git" / "x.png").write_bytes(PNG)
    (tmp_path / "outside.png").write_bytes(PNG)
    (repo / "link.png").symlink_to(tmp_path / "outside.png")
    (repo / "evil").mkdir()
    (repo / "evil" / "o.png").symlink_to(tmp_path / "outside.png")
    (repo / "evil" / "ok.txt").write_text("hi")
    yield s
    s.stop()


def get(site, url):
    return site.request("GET", url)


def test_png_served_with_type_and_headers(site):
    status, h, body = get(site, "/api/asset?path=reports/r1/a.png")
    assert status == 200 and body == PNG
    assert h["content-type"] == "image/png"
    assert h["cache-control"] == "no-store" and h["x-content-type-options"] == "nosniff"
    assert "content-disposition" not in h and "content-security-policy" not in h


def test_download_header(site):
    _, h, _ = get(site, "/api/asset?path=reports/r1/a.png&download=1")
    assert h["content-disposition"].startswith('attachment; filename="a.png"')


def test_svg_gets_sandbox_csp(site):
    _, h, body = get(site, "/api/asset?path=reports/r1/x.svg")
    assert h["content-type"] == "image/svg+xml" and h["content-security-policy"] == "sandbox"
    assert body == SVG


def test_plans_dir_allowed(site):
    assert get(site, "/api/asset?path=.harness/plans/p.png")[0] == 200


@pytest.mark.parametrize(
    "path",
    [
        "../outside.png",
        ".harness/x.png",
        ".git/x.png",
        ".env.png",
        "link.png",
        "evil/o.png",
        "reports/r1/run.py",
        "reports/r1",
        "missing.png",
        "",
    ],
)
def test_refused(site, path):
    status, _, body = get(site, f"/api/asset?path={quote(path)}")
    assert status == 404 and b"error" in body


def test_oversize_refused(site):
    (site.repo / "big.png").write_bytes(b"0" * (MAX_ASSET_BYTES + 1))
    assert read_asset(site.repo, "big.png") is None


def test_read_asset_never_raises(tmp_path):
    assert read_asset(tmp_path, None) is None
    assert read_asset(tmp_path, "a\0b.png") is None
    assert read_asset(tmp_path, "x" * 1000 + ".png") is None


def test_bundle(site):
    status, h, body = get(site, "/api/report-bundle?dir=reports/r1")
    assert status == 200 and h["content-type"] == "application/zip"
    assert 'filename="r1.zip"' in h["content-disposition"]
    names = set(zipfile.ZipFile(io.BytesIO(body)).namelist())
    assert names == {"a.png", "sub/t.csv", "x.svg"}


def test_bundle_skips_escaping_symlinks(site):
    names = set(zipfile.ZipFile(io.BytesIO(report_bundle(site.repo, "evil")[0])).namelist())
    assert names == {"ok.txt"}


@pytest.mark.parametrize("d", ["../", ".harness", ".git", "reports/r1/a.png", "nope", ""])
def test_bundle_refused(site, d):
    assert get(site, f"/api/report-bundle?dir={quote(d)}")[0] == 404


def test_bundle_empty_dir(site):
    (site.repo / "empty").mkdir()
    assert report_bundle(site.repo, "empty") is None
