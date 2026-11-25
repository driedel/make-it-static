"""Tests for optimize.py — CSS/JS bundling, minification, and directory optimization."""

import json
import subprocess  # nosec B404
from pathlib import Path
from unittest.mock import MagicMock, patch

from bs4 import BeautifulSoup

from deploy import BUILD_REPORT
from optimize import (
    _bundle_css,
    _bundle_js,
    _convert_videos,
    _file_hash,
    _rewrite_css_urls,
    optimize_directory,
)


# ---------------------------------------------------------------------------
# _file_hash
# ---------------------------------------------------------------------------

def test_file_hash_is_deterministic():
    """Same input always produces the same hash."""
    assert _file_hash("hello") == _file_hash("hello")


def test_file_hash_differs_for_different_input():
    """Different inputs produce different hashes."""
    assert _file_hash("a") != _file_hash("b")


def test_file_hash_length_is_sixteen():
    """Hash is always 16 characters long (SHA-256 truncated)."""
    assert len(_file_hash("anything")) == 16


# ---------------------------------------------------------------------------
# _rewrite_css_urls
# ---------------------------------------------------------------------------

def test_rewrite_css_urls_http_unchanged(tmp_path):
    """Absolute HTTP URLs inside url() are not rewritten."""
    css = tmp_path / "style.css"
    bundle = tmp_path / "bundle.css"
    result = _rewrite_css_urls(
        "body { background: url(http://example.com/img.png); }", css, bundle, tmp_path
    )
    assert "http://example.com/img.png" in result


def test_rewrite_css_urls_https_unchanged(tmp_path):
    """Absolute HTTPS URLs inside url() are not rewritten."""
    css = tmp_path / "style.css"
    bundle = tmp_path / "bundle.css"
    result = _rewrite_css_urls(
        "body { background: url(https://cdn.com/img.png); }", css, bundle, tmp_path
    )
    assert "https://cdn.com/img.png" in result


def test_rewrite_css_urls_data_uri_unchanged(tmp_path):
    """Data URIs inside url() are not rewritten."""
    css = tmp_path / "style.css"
    bundle = tmp_path / "bundle.css"
    content = "body { background: url(data:image/png;base64,abc123); }"
    result = _rewrite_css_urls(content, css, bundle, tmp_path)
    assert "data:image/png;base64,abc123" in result


def test_rewrite_css_urls_relative_is_rewritten(tmp_path):
    """Relative url() references are rewritten relative to the bundle's location."""
    sub = tmp_path / "css"
    sub.mkdir()
    css = sub / "style.css"
    bundle = tmp_path / "bundle.css"
    result = _rewrite_css_urls("body { background: url(../img/bg.png); }", css, bundle, tmp_path)
    assert "url(" in result
    assert "img/bg.png" in result


# ---------------------------------------------------------------------------
# _bundle_css
# ---------------------------------------------------------------------------

def test_bundle_css_single_file_minifies_in_place(tmp_path):
    """A single local CSS file is minified in place without creating a bundle."""
    (tmp_path / "style.css").write_text("body { color : red ; }")
    html = tmp_path / "index.html"
    html.write_text(
        '<html><head><link rel="stylesheet" href="style.css">'
        '</head><body></body></html>'
    )
    soup = BeautifulSoup(html.read_text(), "html.parser")

    result = _bundle_css(html, soup, tmp_path)

    assert result == 1
    bundles = list(tmp_path.glob("bundle.*.css"))
    assert not bundles  # single file: no bundle created
    content = (tmp_path / "style.css").read_text()
    assert content == "body{color:red}"


def test_bundle_css_multiple_files_creates_bundle(tmp_path):
    """Multiple local CSS files are merged into a single bundle file."""
    (tmp_path / "a.css").write_text("body { color: red; }")
    (tmp_path / "b.css").write_text("h1 { font-size: 2em; }")
    html = tmp_path / "index.html"
    html.write_text(
        '<html><head>'
        '<link rel="stylesheet" href="a.css">'
        '<link rel="stylesheet" href="b.css">'
        '</head><body></body></html>'
    )
    soup = BeautifulSoup(html.read_text(), "html.parser")

    result = _bundle_css(html, soup, tmp_path)

    assert result == 2
    bundles = list(tmp_path.glob("bundle.*.css"))
    assert len(bundles) == 1
    content = bundles[0].read_text()
    assert "color" in content
    assert "font-size" in content


def test_bundle_css_external_stylesheet_skipped(tmp_path):
    """External (CDN) stylesheets are not included in the bundle."""
    html = tmp_path / "index.html"
    html.write_text(
        '<html><head>'
        '<link rel="stylesheet" href="https://cdn.example.com/style.css">'
        '</head><body></body></html>'
    )
    soup = BeautifulSoup(html.read_text(), "html.parser")

    result = _bundle_css(html, soup, tmp_path)
    assert result == 0


def test_bundle_css_no_stylesheets_returns_zero(tmp_path):
    """An HTML file with no stylesheets returns a count of zero."""
    html = tmp_path / "index.html"
    html.write_text("<html><head></head><body></body></html>")
    soup = BeautifulSoup(html.read_text(), "html.parser")

    result = _bundle_css(html, soup, tmp_path)
    assert result == 0


# ---------------------------------------------------------------------------
# _bundle_js
# ---------------------------------------------------------------------------

def test_bundle_js_multiple_files_creates_bundle(tmp_path):
    """Multiple local JS files are merged into a single bundle file."""
    (tmp_path / "a.js").write_text("var x = 1;")
    (tmp_path / "b.js").write_text("var y = 2;")
    html = tmp_path / "index.html"
    html.write_text(
        '<html><body>'
        '<script src="a.js"></script>'
        '<script src="b.js"></script>'
        '</body></html>'
    )
    soup = BeautifulSoup(html.read_text(), "html.parser")

    result = _bundle_js(html, soup, tmp_path)

    assert result == 2
    bundles = list(tmp_path.glob("bundle.*.js"))
    assert len(bundles) == 1


def test_bundle_js_es_module_skipped(tmp_path):
    """ES module scripts (type=module) are not bundled."""
    (tmp_path / "mod.js").write_text("export default {};")
    html = tmp_path / "index.html"
    html.write_text(
        '<html><body><script type="module" src="mod.js"></script></body></html>'
    )
    soup = BeautifulSoup(html.read_text(), "html.parser")

    result = _bundle_js(html, soup, tmp_path)
    assert result == 0


def test_bundle_js_external_script_skipped(tmp_path):
    """External (CDN) scripts are not included in the bundle."""
    html = tmp_path / "index.html"
    html.write_text(
        '<html><body>'
        '<script src="https://cdn.example.com/lib.js"></script>'
        '</body></html>'
    )
    soup = BeautifulSoup(html.read_text(), "html.parser")

    result = _bundle_js(html, soup, tmp_path)
    assert result == 0


# ---------------------------------------------------------------------------
# optimize_directory — options flags
# ---------------------------------------------------------------------------

def test_optimize_directory_html_is_minified_by_default(tmp_path):
    """HTML is minified when compress_html is not explicitly disabled."""
    html = tmp_path / "index.html"
    raw = "<html>  <head>  </head>  <body>  <p>  hello  </p>  </body>  </html>"
    html.write_text(raw)

    optimize_directory(str(tmp_path), bundle_css=False, bundle_js=False)

    assert len(html.read_text()) < len(raw)


def test_optimize_directory_no_compress_html_leaves_html_unchanged(tmp_path):
    """HTML is not modified when compress_html=False."""
    html = tmp_path / "index.html"
    raw = "<html>  <head>  </head>  <body>  <p>  hello  </p>  </body>  </html>"
    html.write_text(raw)

    optimize_directory(str(tmp_path), bundle_css=False, bundle_js=False, compress_html=False)

    assert html.read_text() == raw


def test_optimize_directory_no_bundle_css_creates_no_bundle(tmp_path):
    """No CSS bundle is created when bundle_css=False."""
    (tmp_path / "a.css").write_text("body{color:red}")
    (tmp_path / "b.css").write_text("h1{font-size:2em}")
    html = tmp_path / "index.html"
    html.write_text(
        '<html><head>'
        '<link rel="stylesheet" href="a.css">'
        '<link rel="stylesheet" href="b.css">'
        '</head><body></body></html>'
    )

    optimize_directory(str(tmp_path), bundle_css=False, bundle_js=False, compress_html=False)

    assert not list(tmp_path.glob("bundle.*.css"))


def test_optimize_directory_no_bundle_js_creates_no_bundle(tmp_path):
    """No JS bundle is created when bundle_js=False."""
    (tmp_path / "a.js").write_text("var x=1;")
    (tmp_path / "b.js").write_text("var y=2;")
    html = tmp_path / "index.html"
    html.write_text(
        '<html><body>'
        '<script src="a.js"></script>'
        '<script src="b.js"></script>'
        '</body></html>'
    )

    optimize_directory(str(tmp_path), bundle_css=False, bundle_js=False, compress_html=False)

    assert not list(tmp_path.glob("bundle.*.js"))


def test_optimize_directory_no_convert_fonts_skips_font_conversion(tmp_path, monkeypatch):
    """Font conversion is skipped when convert_fonts=False."""
    calls = []
    monkeypatch.setattr(
        "optimize._convert_fonts", lambda root, report=None: calls.append(root) or 0
    )

    optimize_directory(str(tmp_path), convert_fonts=False)

    assert not calls


def test_optimize_directory_no_compress_images_skips_image_conversion(tmp_path, monkeypatch):
    """Image conversion is skipped when compress_images=False."""
    calls = []
    monkeypatch.setattr(
        "optimize._convert_images", lambda root, report=None: calls.append(root) or 0
    )

    optimize_directory(str(tmp_path), compress_images=False)

    assert not calls


def test_optimize_directory_convert_fonts_enabled_calls_converter(tmp_path, monkeypatch):
    """Font converter is called exactly once when convert_fonts=True."""
    calls = []
    monkeypatch.setattr(
        "optimize._convert_fonts", lambda root, report=None: calls.append(root) or 0
    )

    optimize_directory(str(tmp_path), convert_fonts=True)

    assert len(calls) == 1


def test_optimize_directory_no_convert_videos_skips_video_conversion(tmp_path, monkeypatch):
    """Video conversion is skipped when convert_videos=False."""
    calls = []
    monkeypatch.setattr(
        "optimize._convert_videos",
        lambda root, cache_bucket="", cache_prefix="", report=None: calls.append(root) or (0, 0),
    )

    optimize_directory(str(tmp_path), convert_videos=False)

    assert not calls


def test_optimize_directory_convert_videos_enabled_calls_converter(tmp_path, monkeypatch):
    """Video converter is called exactly once when convert_videos=True, with cache args."""
    calls = []

    def fake_convert(_root, cache_bucket="", cache_prefix="", **_kwargs):
        calls.append((cache_bucket, cache_prefix))
        return 0, 0

    monkeypatch.setattr("optimize._convert_videos", fake_convert)

    stats = optimize_directory(
        str(tmp_path), convert_videos=True, cache_bucket="b", cache_prefix="site.com"
    )

    assert calls == [("b", "site.com")]
    assert stats["videos"] == 0
    assert stats["videos_cached"] == 0


# ---------------------------------------------------------------------------
# optimize_directory — stats
# ---------------------------------------------------------------------------

def test_optimize_directory_stats_count_html_files(tmp_path):
    """Stats correctly count the number of HTML files processed."""
    (tmp_path / "a.html").write_text("<html><body>page a</body></html>")
    (tmp_path / "b.html").write_text("<html><body>page b</body></html>")

    stats = optimize_directory(str(tmp_path), bundle_css=False, bundle_js=False)

    assert stats["html"] == 2
    assert stats["css"] == 0
    assert stats["js"] == 0


def test_optimize_directory_stats_count_css_files(tmp_path):
    """Stats correctly count the number of CSS files bundled."""
    (tmp_path / "a.css").write_text("body{}")
    (tmp_path / "b.css").write_text("h1{}")
    html = tmp_path / "index.html"
    html.write_text(
        '<html><head>'
        '<link rel="stylesheet" href="a.css">'
        '<link rel="stylesheet" href="b.css">'
        '</head><body></body></html>'
    )

    stats = optimize_directory(str(tmp_path), bundle_js=False)

    assert stats["css"] == 2


# ---------------------------------------------------------------------------
# Build report (--report)
# ---------------------------------------------------------------------------

def _read_report_files(report_path):
    """Loads the files list from a build report JSON written by optimize."""
    return json.loads(report_path.read_text(encoding="utf-8"))["files"]


def test_optimize_directory_report_records_converted_image(tmp_path):
    """A converted image appears in the report with original and final sizes."""
    from PIL import Image  # pylint: disable=import-outside-toplevel

    Image.new("RGB", (32, 32), color="red").save(tmp_path / "photo.png")
    html = tmp_path / "index.html"
    html.write_text('<html><body><img src="photo.png"></body></html>')

    report_path = tmp_path / BUILD_REPORT
    optimize_directory(str(tmp_path), report_path=str(report_path))

    files = _read_report_files(report_path)
    entry = next(f for f in files if f["kind"] == "image")
    assert entry["action"] == "converted"
    assert entry["path"] == "photo.png"
    assert entry["original_bytes"] > 0
    assert entry["final_bytes"] > 0


def test_optimize_directory_report_records_bundle_with_sources(tmp_path):
    """A CSS bundle is reported with its source files and their sizes."""
    (tmp_path / "a.css").write_text("body { color: red; }")
    (tmp_path / "b.css").write_text("h1 { font-size: 2em; }")
    html = tmp_path / "index.html"
    html.write_text(
        '<html><head>'
        '<link rel="stylesheet" href="a.css">'
        '<link rel="stylesheet" href="b.css">'
        '</head><body></body></html>'
    )

    report_path = tmp_path / BUILD_REPORT
    optimize_directory(str(tmp_path), report_path=str(report_path))

    files = _read_report_files(report_path)
    entry = next(f for f in files if f["action"] == "bundled")
    assert entry["kind"] == "css"
    assert entry["path"].startswith("bundle.")
    assert [s["path"] for s in entry["sources"]] == ["a.css", "b.css"]
    assert entry["original_bytes"] == sum(s["bytes"] for s in entry["sources"])
    assert entry["final_bytes"] == (tmp_path / entry["path"]).stat().st_size


def test_optimize_directory_report_records_minified_html(tmp_path):
    """HTML minification is reported with before/after sizes."""
    html = tmp_path / "index.html"
    html.write_text(
        "<html>  <head>  </head>  <body>  <p>  hello  </p>  </body>  </html>"
    )

    report_path = tmp_path / BUILD_REPORT
    optimize_directory(
        str(tmp_path), bundle_css=False, bundle_js=False, report_path=str(report_path)
    )

    files = _read_report_files(report_path)
    entry = next(f for f in files if f["kind"] == "html")
    assert entry["action"] == "minified"
    assert entry["path"] == "index.html"
    assert entry["original_bytes"] > entry["final_bytes"]


def test_convert_videos_report_records_converted(tmp_path):
    """A converted video is reported with the source .mov and output .webm sizes."""
    (tmp_path / "clip.mov").write_bytes(b"fake-video-bytes")

    ok = subprocess.CompletedProcess([], returncode=0, stderr="")

    def fake_run(cmd, **_kwargs):
        """Simulates ffmpeg producing the output .webm."""
        Path(cmd[-1]).write_bytes(b"webm")
        return ok

    report = []
    with (
        patch("optimize.shutil.which", return_value="/usr/bin/ffmpeg"),
        patch("optimize.subprocess.run", side_effect=fake_run),
    ):
        converted, cached = _convert_videos(tmp_path, report=report)

    assert (converted, cached) == (1, 0)
    assert report == [{
        "path": "clip.mov",
        "kind": "video",
        "action": "converted",
        "original_bytes": len(b"fake-video-bytes"),
        "final_bytes": len(b"webm"),
    }]


def test_convert_videos_report_records_cache_hit(tmp_path):
    """A cache-hit video is reported as cached, sized by the S3 object metadata."""
    from deploy import file_sha256  # pylint: disable=import-outside-toplevel

    (tmp_path / "clip.mov").write_bytes(b"fake-video")

    mock_s3 = MagicMock()
    mock_s3.head_object.return_value = {
        "Metadata": {
            "source-sha256": file_sha256(tmp_path / "clip.mov"),
            "encoder": "vp9-crf33-opus96k-v1",
        },
        "ContentLength": 1234,
    }

    def fail_ffmpeg(*_args, **_kwargs):
        raise AssertionError("ffmpeg must not run on a cache hit")

    report = []
    with (
        patch("optimize._s3_client", return_value=mock_s3),
        patch("optimize.shutil.which", return_value="/usr/bin/ffmpeg"),
        patch("optimize.subprocess.run", side_effect=fail_ffmpeg),
    ):
        converted, cached = _convert_videos(
            tmp_path, cache_bucket="b", cache_prefix="site.com", report=report
        )

    assert (converted, cached) == (0, 1)
    assert report == [{
        "path": "clip.mov",
        "kind": "video",
        "action": "cached",
        "original_bytes": len(b"fake-video"),
        "final_bytes": 1234,
    }]


def test_optimize_directory_without_report_writes_no_file(tmp_path):
    """Without report_path no build report is written and behavior is unchanged."""
    (tmp_path / "index.html").write_text("<html><body>hi</body></html>")

    stats = optimize_directory(str(tmp_path), bundle_css=False, bundle_js=False)

    assert not (tmp_path / BUILD_REPORT).exists()
    assert stats["html"] == 1


def test_optimize_directory_report_write_failure_is_not_fatal(tmp_path):
    """An unwritable report path only logs a warning — optimization still succeeds."""
    (tmp_path / "index.html").write_text("<html><body>hi</body></html>")

    stats = optimize_directory(
        str(tmp_path),
        bundle_css=False,
        bundle_js=False,
        report_path=str(tmp_path / "no-such-dir" / "report.json"),
    )

    assert stats["html"] == 1
