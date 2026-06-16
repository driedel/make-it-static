"""Integration tests — exercise real dependencies (Pillow, fontTools, boto3, subprocess)."""

import subprocess
from unittest.mock import MagicMock, patch

import pytest

from optimize import _convert_fonts, _convert_images, optimize_directory


# ---------------------------------------------------------------------------
# Subprocess _run
# ---------------------------------------------------------------------------

def test_run_successful_command():
    """_run captures stdout from a successful subprocess."""
    from jobs import _run

    result = _run(["echo", "hello world"])
    assert result.returncode == 0
    assert "hello world" in result.stdout


def test_run_failing_command():
    """_run returns non-zero rc when the command fails."""
    from jobs import _run

    result = _run(["python", "-c", "import sys; sys.exit(42)"])
    assert result.returncode == 42


def test_run_command_timeout():
    """_run raises TimeoutExpired when the command exceeds the timeout."""
    from jobs import _run

    with pytest.raises(subprocess.TimeoutExpired):
        _run(["python", "-c", "import time; time.sleep(10)"], timeout=0.1)


# ---------------------------------------------------------------------------
# Font conversion (fontTools)
# ---------------------------------------------------------------------------

def test_convert_fonts_skips_without_fonttools(monkeypatch, tmp_path):
    """When fontTools is not importable, _convert_fonts returns 0."""
    original_import = __builtins__["__import__"]
    def fake_import(name, *args, **kwargs):
        if name == "fontTools.ttLib":
            raise ImportError()
        return original_import(name, *args, **kwargs)
    monkeypatch.setattr("builtins.__import__", fake_import)
    assert _convert_fonts(tmp_path) == 0


def _build_minimal_ttf(path):
    """Helper to build a minimal valid TTF using fontTools.fontBuilder."""
    from fontTools.fontBuilder import FontBuilder
    from fontTools.pens.ttGlyphPen import TTGlyphPen

    pen = TTGlyphPen(None)
    pen.moveTo((0, 0))
    pen.lineTo((100, 0))
    pen.lineTo((100, 100))
    pen.lineTo((0, 100))
    pen.closePath()
    glyph = pen.glyph()

    fb = FontBuilder(1000, isTTF=True)
    fb.setupGlyphOrder([".notdef"])
    fb.setupCharacterMap({})
    fb.setupGlyf({".notdef": glyph})
    fb.setupHorizontalMetrics({".notdef": (500, 0)})
    fb.setupHorizontalHeader(ascent=800, descent=-200)
    fb.setupNameTable({"familyName": "Test", "styleName": "Regular"})
    fb.setupOS2()
    fb.setupPost()
    fb.setupMaxp()
    fb.setupHead()
    fb.save(str(path))


def test_convert_fonts_creates_woff2_and_updates_refs(tmp_path):
    """A real TTF is converted to WOFF2 and references in CSS/HTML are rewritten."""
    ttf_path = tmp_path / "testfont.ttf"
    _build_minimal_ttf(ttf_path)

    # Create CSS referencing the TTF
    css = tmp_path / "style.css"
    css.write_text("src: url('testfont.ttf') format('truetype');")

    count = _convert_fonts(tmp_path)

    assert count == 1
    assert not ttf_path.exists()
    assert (tmp_path / "testfont.woff2").exists()
    updated = css.read_text()
    assert "testfont.woff2" in updated
    assert "format('woff2')" in updated
    assert "truetype" not in updated


def test_convert_fonts_skips_existing_woff2(tmp_path):
    """If WOFF2 already exists, the original TTF is not converted again."""
    ttf_path = tmp_path / "testfont.ttf"
    _build_minimal_ttf(ttf_path)

    # Pre-create the WOFF2
    (tmp_path / "testfont.woff2").write_bytes(b"fake")

    count = _convert_fonts(tmp_path)

    # TTF should still be removed (since woff2 exists)
    assert count == 1
    assert not ttf_path.exists()


# ---------------------------------------------------------------------------
# Image conversion (Pillow)
# ---------------------------------------------------------------------------

def test_convert_images_skips_without_pillow(monkeypatch, tmp_path):
    """When Pillow is not importable, _convert_images returns 0."""
    original_import = __builtins__["__import__"]
    def fake_import(name, *args, **kwargs):
        if name == "PIL.Image":
            raise ImportError()
        return original_import(name, *args, **kwargs)
    monkeypatch.setattr("builtins.__import__", fake_import)
    assert _convert_images(tmp_path) == 0


def test_convert_images_png_to_avif_or_webp(tmp_path):
    """A real PNG is converted to AVIF or WebP and references updated."""
    from PIL import Image

    img = Image.new("RGB", (10, 10), color="red")
    png_path = tmp_path / "test.png"
    img.save(png_path)

    html = tmp_path / "index.html"
    html.write_text('<img src="test.png">')

    count = _convert_images(tmp_path)

    assert count == 1
    assert not png_path.exists()
    # Should create AVIF or WebP
    assert (tmp_path / "test.avif").exists() or (tmp_path / "test.webp").exists()
    updated = html.read_text()
    assert "test.png" not in updated
    assert ("test.avif" in updated or "test.webp" in updated)


def test_convert_images_skips_if_modern_format_exists(tmp_path):
    """If AVIF already exists, the original PNG is not converted."""
    from PIL import Image

    img = Image.new("RGB", (10, 10), color="red")
    png_path = tmp_path / "test.png"
    img.save(png_path)

    # Pre-create AVIF
    avif_path = tmp_path / "test.avif"
    img.save(avif_path, "AVIF")

    count = _convert_images(tmp_path)

    assert count == 0
    assert png_path.exists()  # original kept since modern format exists


# ---------------------------------------------------------------------------
# Full optimize_directory pipeline
# ---------------------------------------------------------------------------

def test_optimize_directory_full_pipeline(tmp_path):
    """Full pipeline: font + image + CSS/JS bundle + HTML minification."""
    from PIL import Image

    # Create font
    _build_minimal_ttf(tmp_path / "font.ttf")

    # Create image
    img = Image.new("RGB", (10, 10), color="blue")
    img.save(tmp_path / "img.png")

    # Create CSS
    css1 = tmp_path / "a.css"
    css1.write_text("body { color: red; background: url('img.png'); }")
    css2 = tmp_path / "b.css"
    css2.write_text("h1 { font-size: 2em; }")

    # Create JS
    js1 = tmp_path / "a.js"
    js1.write_text("var x = 1;")
    js2 = tmp_path / "b.js"
    js2.write_text("var y = 2;")

    # Create HTML
    html = tmp_path / "index.html"
    html.write_text(
        '<html><head>'
        '<link rel="stylesheet" href="a.css">'
        '<link rel="stylesheet" href="b.css">'
        '</head><body>'
        '<img src="img.png">'
        '<script src="a.js"></script>'
        '<script src="b.js"></script>'
        '</body></html>'
    )

    stats = optimize_directory(str(tmp_path))

    assert stats["html"] == 1
    assert stats["css"] == 2
    assert stats["js"] == 2
    assert stats["fonts"] == 1
    assert stats["images"] == 1

    # Verify conversions
    assert not (tmp_path / "font.ttf").exists()
    assert (tmp_path / "font.woff2").exists()
    assert not (tmp_path / "img.png").exists()
    assert (tmp_path / "img.avif").exists() or (tmp_path / "img.webp").exists()

    # Verify bundling
    assert len(list(tmp_path.glob("bundle.*.css"))) == 1
    assert len(list(tmp_path.glob("bundle.*.js"))) == 1

    # Verify HTML minification
    html_text = html.read_text()
    assert len(html_text) < 300  # significantly smaller than original


# ---------------------------------------------------------------------------
# S3 upload (mock boto3)
# ---------------------------------------------------------------------------

def test_sync_to_s3_uploads_with_correct_content_type(tmp_path):
    """sync_to_s3 uploads files with correct ContentType and CacheControl."""
    from deploy import sync_to_s3

    # Create test files
    (tmp_path / "index.html").write_text("<html></html>")
    (tmp_path / "style.css").write_text("body{}")
    (tmp_path / "script.js").write_text("var x=1;")

    mock_s3 = MagicMock()
    with patch("deploy._s3_client", return_value=mock_s3):
        count = sync_to_s3(tmp_path, "test-bucket", "example.com")

    assert count == 3
    assert mock_s3.upload_file.call_count == 3

    # Check HTML cache
    html_call = [c for c in mock_s3.upload_file.call_args_list if "index.html" in str(c)][0]
    assert html_call.kwargs["ExtraArgs"]["ContentType"] == "text/html"
    assert "max-age=60" in html_call.kwargs["ExtraArgs"]["CacheControl"]

    # Check CSS cache
    css_call = [c for c in mock_s3.upload_file.call_args_list if "style.css" in str(c)][0]
    assert css_call.kwargs["ExtraArgs"]["ContentType"] == "text/css"
    assert "immutable" in css_call.kwargs["ExtraArgs"]["CacheControl"]

    # Check JS cache (macOS mimetypes may report text/javascript)
    js_call = [c for c in mock_s3.upload_file.call_args_list if "script.js" in str(c)][0]
    assert js_call.kwargs["ExtraArgs"]["ContentType"] in ("application/javascript", "text/javascript")


def test_sync_to_s3_empty_prefix(tmp_path):
    """sync_to_s3 handles empty prefix correctly."""
    from deploy import sync_to_s3

    (tmp_path / "index.html").write_text("<html></html>")

    mock_s3 = MagicMock()
    with patch("deploy._s3_client", return_value=mock_s3):
        count = sync_to_s3(tmp_path, "test-bucket", "")

    assert count == 1
    key = mock_s3.upload_file.call_args.kwargs["Key"]
    assert key == "index.html"  # no prefix/


# ---------------------------------------------------------------------------
# CloudFront invalidation (mock boto3)
# ---------------------------------------------------------------------------

def test_invalidate_cloudfront_skips_when_no_distribution_id():
    """CloudFront invalidation is skipped when distribution_id is empty."""
    from deploy import invalidate_cloudfront

    result = invalidate_cloudfront("", ["/*"])
    assert result is None


def test_invalidate_cloudfront_creates_invalidation():
    """CloudFront invalidation is created with the correct parameters."""
    from deploy import invalidate_cloudfront

    mock_cf = MagicMock()
    mock_cf.create_invalidation.return_value = {
        "Invalidation": {"Id": "I123ABC"}
    }

    with patch("boto3.client", return_value=mock_cf):
        result = invalidate_cloudfront("E1ABC123", ["/blog/*", "/page/*"])

    assert result == "I123ABC"
    mock_cf.create_invalidation.assert_called_once()
    call = mock_cf.create_invalidation.call_args
    assert call.kwargs["DistributionId"] == "E1ABC123"
    assert call.kwargs["InvalidationBatch"]["Paths"]["Quantity"] == 2
    assert "/blog/*" in call.kwargs["InvalidationBatch"]["Paths"]["Items"]


# ---------------------------------------------------------------------------
# _safe_path validation
# ---------------------------------------------------------------------------

def test_safe_path_allows_normal_paths():
    """_safe_path allows paths within workdir."""
    from jobs import _safe_path
    import pathlib

    workdir = pathlib.Path("/tmp/deploys/test")
    result = _safe_path(workdir, "assets", "style.css")
    assert result is not None
    # macOS resolves /tmp to /private/tmp, so check suffix instead of exact match
    assert str(result).endswith("/tmp/deploys/test/assets/style.css")


def test_safe_path_blocks_traversal():
    """_safe_path rejects path traversal attempts."""
    from jobs import _safe_path
    import pathlib

    workdir = pathlib.Path("/tmp/deploys/test")
    result = _safe_path(workdir, "../../etc/passwd")
    assert result is None


def test_safe_path_blocks_absolute_traversal():
    """_safe_path rejects absolute paths outside workdir."""
    from jobs import _safe_path
    import pathlib

    workdir = pathlib.Path("/tmp/deploys/test")
    result = _safe_path(workdir, "/etc/passwd")
    assert result is None
