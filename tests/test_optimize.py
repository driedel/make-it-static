from pathlib import Path
from unittest.mock import patch

import pytest
from bs4 import BeautifulSoup

from optimize import (
    _bundle_css,
    _bundle_js,
    _file_hash,
    _rewrite_css_urls,
    optimize_directory,
)


# ---------------------------------------------------------------------------
# _file_hash
# ---------------------------------------------------------------------------

def test_file_hash_is_deterministic():
    assert _file_hash("hello") == _file_hash("hello")


def test_file_hash_differs_for_different_input():
    assert _file_hash("a") != _file_hash("b")


def test_file_hash_length_is_eight():
    assert len(_file_hash("anything")) == 8


# ---------------------------------------------------------------------------
# _rewrite_css_urls
# ---------------------------------------------------------------------------

def test_rewrite_css_urls_http_unchanged(tmp_path):
    css = tmp_path / "style.css"
    bundle = tmp_path / "bundle.css"
    result = _rewrite_css_urls("body { background: url(http://example.com/img.png); }", css, bundle, tmp_path)
    assert "http://example.com/img.png" in result


def test_rewrite_css_urls_https_unchanged(tmp_path):
    css = tmp_path / "style.css"
    bundle = tmp_path / "bundle.css"
    result = _rewrite_css_urls("body { background: url(https://cdn.com/img.png); }", css, bundle, tmp_path)
    assert "https://cdn.com/img.png" in result


def test_rewrite_css_urls_data_uri_unchanged(tmp_path):
    css = tmp_path / "style.css"
    bundle = tmp_path / "bundle.css"
    content = "body { background: url(data:image/png;base64,abc123); }"
    result = _rewrite_css_urls(content, css, bundle, tmp_path)
    assert "data:image/png;base64,abc123" in result


def test_rewrite_css_urls_relative_is_rewritten(tmp_path):
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
    (tmp_path / "style.css").write_text("body { color : red ; }")
    html = tmp_path / "index.html"
    html.write_text('<html><head><link rel="stylesheet" href="style.css"></head><body></body></html>')
    soup = BeautifulSoup(html.read_text(), "html.parser")

    result = _bundle_css(html, soup, tmp_path)

    assert result == 1
    bundles = list(tmp_path.glob("bundle.*.css"))
    assert len(bundles) == 0  # single file: no bundle created
    content = (tmp_path / "style.css").read_text()
    assert "body{color:red}" == content


def test_bundle_css_multiple_files_creates_bundle(tmp_path):
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
    html = tmp_path / "index.html"
    html.write_text("<html><head></head><body></body></html>")
    soup = BeautifulSoup(html.read_text(), "html.parser")

    result = _bundle_css(html, soup, tmp_path)
    assert result == 0


# ---------------------------------------------------------------------------
# _bundle_js
# ---------------------------------------------------------------------------

def test_bundle_js_multiple_files_creates_bundle(tmp_path):
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
    (tmp_path / "mod.js").write_text("export default {};")
    html = tmp_path / "index.html"
    html.write_text('<html><body><script type="module" src="mod.js"></script></body></html>')
    soup = BeautifulSoup(html.read_text(), "html.parser")

    result = _bundle_js(html, soup, tmp_path)
    assert result == 0


def test_bundle_js_external_script_skipped(tmp_path):
    html = tmp_path / "index.html"
    html.write_text('<html><body><script src="https://cdn.example.com/lib.js"></script></body></html>')
    soup = BeautifulSoup(html.read_text(), "html.parser")

    result = _bundle_js(html, soup, tmp_path)
    assert result == 0


# ---------------------------------------------------------------------------
# optimize_directory — options flags
# ---------------------------------------------------------------------------

def test_optimize_directory_html_is_minified_by_default(tmp_path):
    html = tmp_path / "index.html"
    raw = "<html>  <head>  </head>  <body>  <p>  hello  </p>  </body>  </html>"
    html.write_text(raw)

    optimize_directory(str(tmp_path), bundle_css=False, bundle_js=False)

    assert len(html.read_text()) < len(raw)


def test_optimize_directory_no_compress_html_leaves_html_unchanged(tmp_path):
    html = tmp_path / "index.html"
    raw = "<html>  <head>  </head>  <body>  <p>  hello  </p>  </body>  </html>"
    html.write_text(raw)

    optimize_directory(str(tmp_path), bundle_css=False, bundle_js=False, compress_html=False)

    assert html.read_text() == raw


def test_optimize_directory_no_bundle_css_creates_no_bundle(tmp_path):
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

    assert list(tmp_path.glob("bundle.*.css")) == []


def test_optimize_directory_no_bundle_js_creates_no_bundle(tmp_path):
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

    assert list(tmp_path.glob("bundle.*.js")) == []


def test_optimize_directory_no_convert_fonts_skips_font_conversion(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr("optimize._convert_fonts", lambda root: calls.append(root) or 0)

    optimize_directory(str(tmp_path), convert_fonts=False)

    assert calls == []


def test_optimize_directory_no_compress_images_skips_image_conversion(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr("optimize._convert_images", lambda root: calls.append(root) or 0)

    optimize_directory(str(tmp_path), compress_images=False)

    assert calls == []


def test_optimize_directory_convert_fonts_enabled_calls_converter(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr("optimize._convert_fonts", lambda root: calls.append(root) or 0)

    optimize_directory(str(tmp_path), convert_fonts=True)

    assert len(calls) == 1


# ---------------------------------------------------------------------------
# optimize_directory — stats
# ---------------------------------------------------------------------------

def test_optimize_directory_stats_count_html_files(tmp_path):
    (tmp_path / "a.html").write_text("<html><body>page a</body></html>")
    (tmp_path / "b.html").write_text("<html><body>page b</body></html>")

    stats = optimize_directory(str(tmp_path), bundle_css=False, bundle_js=False)

    assert stats["html"] == 2
    assert stats["css"] == 0
    assert stats["js"] == 0


def test_optimize_directory_stats_count_css_files(tmp_path):
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
