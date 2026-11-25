"""Tests for postprocess.py — URL rewriting, file normalization, and HTML absolutization."""

from postprocess import (
    absolutize_html_urls,
    apply_renames_to_text_files,
    build_patterns,
    normalize_query_string_files,
)


# ---------------------------------------------------------------------------
# normalize_query_string_files
# ---------------------------------------------------------------------------

def test_normalize_renames_file_with_query_string(tmp_path):
    """File with query-string suffix is renamed to the clean name."""
    source_file = tmp_path / "style.css@ver=6.4.1"
    source_file.write_text("body{}")

    renames = normalize_query_string_files(tmp_path)

    assert (tmp_path / "style.css").exists()
    assert not source_file.exists()
    assert ("style.css@ver=6.4.1", "style.css") in renames


def test_normalize_renames_js_file(tmp_path):
    """JS file with multiple query params is renamed to the clean base name."""
    source_file = tmp_path / "app.js@ver=1.2&m=1"
    source_file.write_text("var x=1;")

    normalize_query_string_files(tmp_path)

    assert (tmp_path / "app.js").exists()
    assert not source_file.exists()


def test_normalize_duplicate_clean_name_discards_second(tmp_path):
    """When two query-string variants map to the same clean name, the second is discarded."""
    (tmp_path / "style.css@ver=1").write_text("version 1")
    (tmp_path / "style.css@ver=2").write_text("version 2")

    normalize_query_string_files(tmp_path)

    assert (tmp_path / "style.css").exists()
    assert not list(tmp_path.glob("style.css@*"))


def test_normalize_renames_version_only_query_string(tmp_path):
    """wget converts eicons.eot?5.34.0 → eicons.eot@5.34.0 (no = sign)."""
    f = tmp_path / "eicons.eot@5.34.0"
    f.write_bytes(b"\x00")

    renames = normalize_query_string_files(tmp_path)

    assert (tmp_path / "eicons.eot").exists()
    assert not f.exists()
    assert ("eicons.eot@5.34.0", "eicons.eot") in renames
    assert renames


def test_normalize_renames_v_prefixed_version(tmp_path):
    """wget converts fontawesome.woff2?v4.7.0 → fontawesome.woff2@v4.7.0."""
    f = tmp_path / "fontawesome.woff2@v4.7.0"
    f.write_bytes(b"\x00")

    renames = normalize_query_string_files(tmp_path)

    assert (tmp_path / "fontawesome.woff2").exists()
    assert not f.exists()
    assert ("fontawesome.woff2@v4.7.0", "fontawesome.woff2") in renames


def test_normalize_ignores_retina_images(tmp_path):
    """logo@2x.png is a retina image name, NOT a query-string file — must not be renamed."""
    f = tmp_path / "logo@2x.png"
    f.write_bytes(b"\x00")

    renames = normalize_query_string_files(tmp_path)

    assert f.exists()
    assert not renames


def test_normalize_ignores_clean_files(tmp_path):
    """Files without query-string suffixes are left untouched."""
    source_file = tmp_path / "style.css"
    source_file.write_text("body{}")

    normalize_query_string_files(tmp_path)

    assert source_file.exists()
    assert source_file.read_text() == "body{}"


def test_normalize_empty_directory_returns_empty_list(tmp_path):
    """An empty directory produces no renames."""
    assert not normalize_query_string_files(tmp_path)


# ---------------------------------------------------------------------------
# apply_renames_to_text_files
# ---------------------------------------------------------------------------

def test_apply_renames_updates_href_in_html(tmp_path):
    """href attribute with the old filename is rewritten to the new name."""
    html = tmp_path / "index.html"
    html.write_text('<link href="style.css@ver=6.4.1" rel="stylesheet">')

    apply_renames_to_text_files(tmp_path, [("style.css@ver=6.4.1", "style.css")])

    content = html.read_text()
    assert "style.css@ver=6.4.1" not in content
    assert "style.css" in content


def test_apply_renames_updates_src_in_html(tmp_path):
    """src attribute with the old filename is rewritten to the new name."""
    html = tmp_path / "index.html"
    html.write_text('<script src="app.js@ver=2.0"></script>')

    apply_renames_to_text_files(tmp_path, [("app.js@ver=2.0", "app.js")])

    assert "app.js@ver=2.0" not in html.read_text()


def test_apply_renames_version_query_string_in_css(tmp_path):
    """CSS url() with ?5.34.0#iefix (no --convert-links) is fixed to clean name."""
    css = tmp_path / "all.css"
    css.write_text("src: url('eicons.eot?5.34.0#iefix') format('embedded-opentype');")

    apply_renames_to_text_files(tmp_path, [("eicons.eot@5.34.0", "eicons.eot")])

    result = css.read_text()
    assert "eicons.eot?5.34.0" not in result
    assert "eicons.eot#iefix" in result


def test_apply_renames_also_replaces_query_string_form(tmp_path):
    """Without --convert-links, HTML keeps style.css?ver=x instead of style.css@ver=x."""
    html = tmp_path / "index.html"
    html.write_text('<link href="style.css?ver=6.4.1" rel="stylesheet">')

    apply_renames_to_text_files(tmp_path, [("style.css@ver=6.4.1", "style.css")])

    assert "style.css?ver=6.4.1" not in html.read_text()
    assert "style.css" in html.read_text()


def test_apply_renames_empty_list_is_noop(tmp_path):
    """An empty renames list leaves all files unchanged."""
    html = tmp_path / "index.html"
    original = "<p>unchanged</p>"
    html.write_text(original)

    apply_renames_to_text_files(tmp_path, [])

    assert html.read_text() == original


# ---------------------------------------------------------------------------
# build_patterns
# ---------------------------------------------------------------------------

def test_build_patterns_strips_http_origin_host():
    """HTTP origin host prefix is removed from href attributes."""
    patterns = build_patterns("staging.example.com")
    text = 'href="http://staging.example.com/page/"'
    for pat, repl in patterns:
        text = pat.sub(repl, text)
    assert "staging.example.com" not in text
    assert "/page/" in text


def test_build_patterns_strips_https_origin_host():
    """HTTPS origin host prefix is removed from src attributes."""
    patterns = build_patterns("staging.example.com")
    text = 'src="https://staging.example.com/assets/img.png"'
    for pat, repl in patterns:
        text = pat.sub(repl, text)
    assert "staging.example.com" not in text


def test_build_patterns_strips_extra_cdn():
    """Extra CDN host prefix is removed from src attributes."""
    patterns = build_patterns("origin.com", ["cdn.example.com"])
    text = 'src="https://cdn.example.com/assets/font.woff2"'
    for pat, repl in patterns:
        text = pat.sub(repl, text)
    assert "cdn.example.com" not in text


def test_build_patterns_strips_origin_host_in_css_url(tmp_path):
    """CSS url() with absolute origin URLs must have the domain stripped."""
    css = tmp_path / "bundle.css"
    css.write_text(
        'src: url("https://lp.example.com/wp-content/uploads/font.woff2") format("woff2");'
    )
    patterns = build_patterns("lp.example.com")
    text = css.read_text()
    for pat, repl in patterns:
        text = pat.sub(repl, text)
    css.write_text(text)
    result = css.read_text()
    assert "lp.example.com" not in result
    assert "/wp-content/uploads/font.woff2" in result


def test_build_patterns_strips_json_escaped_origin_host():
    """WordPress embeds URLs as \"https:\\/\\/host\\/path\" in inline JS config blocks."""
    patterns = build_patterns("lp.example.com")
    text = r'"ajaxurl":"https:\/\/lp.example.com\/wp-admin\/admin-ajax.php"'
    for pat, repl in patterns:
        text = pat.sub(repl, text)
    assert "lp.example.com" not in text
    assert r"\/wp-admin\/admin-ajax.php" in text


def test_build_patterns_removes_wp_generator_meta():
    """WordPress generator meta tag is stripped."""
    patterns = build_patterns("example.com")
    text = '<meta name="generator" content="WordPress 6.4">'
    for pat, repl in patterns:
        text = pat.sub(repl, text)
    assert "generator" not in text


def test_build_patterns_removes_wp_emoji_script():
    """WordPress emoji inline script block is stripped."""
    patterns = build_patterns("example.com")
    text = '<script type="text/javascript">\nwindow._wpemojiSettings = {};\n</script>'
    for pat, repl in patterns:
        text = pat.sub(repl, text)
    assert "_wpemojiSettings" not in text


def test_build_patterns_removes_pingback_link():
    """XML-RPC pingback link tag is stripped."""
    patterns = build_patterns("example.com")
    text = '<link rel="pingback" href="https://example.com/xmlrpc.php">'
    for pat, repl in patterns:
        text = pat.sub(repl, text)
    assert "pingback" not in text


def test_build_patterns_does_not_strip_lookalike_domain():
    """Host 'wordpress' must not strip 'https://wordpress.org/...' — dot is no boundary."""
    patterns = build_patterns("wordpress")
    text = '<a href="https://wordpress.org/gutenberg/">Gutenberg</a>'
    for pat, repl in patterns:
        text = pat.sub(repl, text)
    assert 'href="https://wordpress.org/gutenberg/"' in text


def test_build_patterns_does_not_strip_lookalike_domain_json_escaped():
    """JSON-escaped 'https:\\/\\/wordpress.org\\/...' survives a 'wordpress' host strip."""
    patterns = build_patterns("wordpress")
    text = r'"url":"https:\/\/wordpress.org\/gutenberg\/"'
    for pat, repl in patterns:
        text = pat.sub(repl, text)
    assert r'"url":"https:\/\/wordpress.org\/gutenberg\/"' in text


def test_build_patterns_strips_host_before_path():
    """Host 'wordpress' still strips plain 'https://wordpress/...' links."""
    patterns = build_patterns("wordpress")
    text = 'href="https://wordpress/wp-content/themes/theme/style.css"'
    for pat, repl in patterns:
        text = pat.sub(repl, text)
    assert "wordpress" not in text
    assert 'href="/wp-content/themes/theme/style.css"' in text


def test_build_patterns_strips_json_escaped_host_before_path():
    """Host 'wordpress' still strips the JSON-escaped form 'https:\\/\\/wordpress\\/...'."""
    patterns = build_patterns("wordpress")
    text = r'"ajaxurl":"https:\/\/wordpress\/wp-admin\/admin-ajax.php"'
    for pat, repl in patterns:
        text = pat.sub(repl, text)
    assert "wordpress" not in text
    assert r'"ajaxurl":"\/wp-admin\/admin-ajax.php"' in text


def test_build_patterns_strips_bare_host_before_quote_or_end():
    """Bare 'https://host' followed by a quote or end-of-string is still stripped."""
    patterns = build_patterns("wordpress")
    text = '<a href="https://wordpress">home</a> canonical:"https://wordpress"'
    for pat, repl in patterns:
        text = pat.sub(repl, text)
    assert "wordpress" not in text


def test_build_patterns_wordpress_org_survives_full_postprocess(tmp_path):
    """Regression: 'https://wordpress.org/' must not become '/.org/' after cleanup.

    Reproduces the original bug end-to-end: the prefix strip turned the URL into
    '.org/...' and absolutize_html_urls then resolved it as a relative path (404).
    """
    html_path = tmp_path / "index.html"
    html_path.touch()
    patterns = build_patterns("wordpress")
    text = '<footer><a href="https://wordpress.org/">Learn more</a></footer>'
    for pat, repl in patterns:
        text = pat.sub(repl, text)
    text = absolutize_html_urls(text, html_path, tmp_path)
    assert 'href="https://wordpress.org/"' in text


def test_build_patterns_removes_modern_emoji_style_block():
    """WP 6.9+ <style id="wp-emoji-styles-inline-css"> block is stripped."""
    patterns = build_patterns("example.com")
    text = (
        '<style id="wp-emoji-styles-inline-css">\n'
        "\timg.wp-smiley, img.emoji {\n"
        "\t\tdisplay: inline !important;\n"
        "\t\theight: 1em !important;\n"
        "\t}\n"
        "/*# sourceURL=wp-emoji-styles-inline-css */\n"
        "</style>"
    )
    for pat, repl in patterns:
        text = pat.sub(repl, text)
    assert "wp-emoji" not in text
    assert "wp-smiley" not in text


def test_build_patterns_removes_modern_emoji_json_settings():
    """WP 6.9+ <script id="wp-emoji-settings" type="application/json"> block is stripped."""
    patterns = build_patterns("example.com")
    text = (
        '<script id="wp-emoji-settings" type="application/json">\n'
        '{"baseUrl":"https://s.w.org/images/core/emoji/17.0.2/72x72/","ext":".png",'
        '"svgUrl":"https://s.w.org/images/core/emoji/17.0.2/svg/","svgExt":".svg",'
        '"source":{"concatemoji":"https://example.com/wp-includes/js/'
        'wp-emoji-release.min.js?ver=7.1.5"}}\n'
        "</script>"
    )
    for pat, repl in patterns:
        text = pat.sub(repl, text)
    assert "wp-emoji-settings" not in text
    assert "concatemoji" not in text
    assert "wp-emoji-release.min.js" not in text


def test_build_patterns_removes_modern_emoji_loader_script():
    """WP 6.9+ id-less inline emoji loader is stripped by its content signature."""
    patterns = build_patterns("example.com")
    text = (
        "<script>\n"
        'var e="script#wp-emoji-settings",t=document.querySelector(e);'
        'if(!(t instanceof HTMLScriptElement))throw new Error("Element missing: "+e);'
        "const r=JSON.parse(t.text),s=(window._wpemojiSettings=r,"
        '"wpEmojiSettingsSupports"),o=["flag","emoji"];\n'
        "//# sourceURL=https://example.com/wp-includes/js/wp-emoji-loader.min.js\n"
        "</script>"
    )
    for pat, repl in patterns:
        text = pat.sub(repl, text)
    assert "_wpemojiSettings" not in text
    assert "wpEmojiSettingsSupports" not in text
    assert "wp-emoji-loader.min.js" not in text


def test_build_patterns_keeps_legitimate_scripts_around_emoji_loader():
    """Legitimate <script> blocks before/after the emoji loader must not be removed."""
    patterns = build_patterns("example.com")
    text = (
        '<script>var themeConfig={"palette":["#fff","#000"]};</script>\n'
        "<script>\n"
        'var e="script#wp-emoji-settings",t=document.querySelector(e);'
        "window._wpemojiSettings=JSON.parse(t.text);\n"
        "</script>\n"
        '<script src="/wp-content/themes/theme/js/main.js"></script>\n'
        '<script>console.log("keep me");</script>'
    )
    for pat, repl in patterns:
        text = pat.sub(repl, text)
    assert "_wpemojiSettings" not in text
    assert 'var themeConfig={"palette":["#fff","#000"]};' in text
    assert '<script src="/wp-content/themes/theme/js/main.js"></script>' in text
    assert 'console.log("keep me");' in text


def test_build_patterns_emoji_loader_match_does_not_cross_script_boundaries():
    """The content-signature match must not swallow a previous legitimate script."""
    patterns = build_patterns("example.com")
    text = (
        '<script>var config={"theme":"twentytwentyfive"};</script>'
        '<script>var e="script#wp-emoji-settings";'
        "window._wpemojiSettings=JSON.parse(t.text);</script>"
    )
    for pat, repl in patterns:
        text = pat.sub(repl, text)
    assert 'var config={"theme":"twentytwentyfive"};' in text
    assert "_wpemojiSettings" not in text


def test_build_patterns_removes_all_modern_emoji_markup():
    """Regression: full WP 6.9+/7.x emoji markup is stripped, other content survives.

    Fixture mirrors the real markup captured from a live WordPress 7.1 homepage:
    inline <style>, JSON settings block, and the id-less loader that injects
    wp-emoji-release.min.js at runtime.
    """
    patterns = build_patterns("wordpress")
    text = (
        "<html><head>\n"
        '<style id="wp-emoji-styles-inline-css">\n'
        "\timg.wp-smiley, img.emoji {\n"
        "\t\tdisplay: inline !important;\n"
        "\t\theight: 1em !important;\n"
        "\t}\n"
        "/*# sourceURL=wp-emoji-styles-inline-css */\n"
        "</style>\n"
        '<script id="wp-emoji-settings" type="application/json">\n'
        '{"baseUrl":"https://s.w.org/images/core/emoji/17.0.2/72x72/","ext":".png",'
        '"svgUrl":"https://s.w.org/images/core/emoji/17.0.2/svg/","svgExt":".svg",'
        '"source":{"concatemoji":"http://localhost:8300/wp-includes/js/'
        'wp-emoji-release.min.js?ver=7.1.5"}}\n'
        "</script>\n"
        "</head><body>\n"
        "<p>Hello</p>\n"
        "<script>\n"
        'var e="script#wp-emoji-settings",t=document.querySelector(e);'
        'if(!(t instanceof HTMLScriptElement))throw new Error("Element missing: "+e);'
        "const r=JSON.parse(t.text),s=(window._wpemojiSettings=r,"
        '"wpEmojiSettingsSupports"),o=["flag","emoji"];\n'
        "//# sourceURL=http://localhost:8300/wp-includes/js/wp-emoji-loader.min.js\n"
        "</script>\n"
        '<script src="/wp-content/themes/theme/js/main.js"></script>\n'
        "</body></html>"
    )
    for pat, repl in patterns:
        text = pat.sub(repl, text)
    assert "wp-emoji-styles-inline-css" not in text
    assert "wp-emoji-settings" not in text
    assert "_wpemojiSettings" not in text
    assert "wpEmojiSettingsSupports" not in text
    assert "wp-emoji-release.min.js" not in text
    assert "wp-emoji-loader.min.js" not in text
    assert "wp-smiley" not in text
    # unrelated content survives untouched
    assert "<p>Hello</p>" in text
    assert '<script src="/wp-content/themes/theme/js/main.js"></script>' in text


# ---------------------------------------------------------------------------
# absolutize_html_urls
# ---------------------------------------------------------------------------

def test_absolutize_relative_href_becomes_absolute(tmp_path):
    """A relative href at root level is prefixed with /."""
    html_path = tmp_path / "index.html"
    html_path.touch()
    text = absolutize_html_urls('<a href="page.html">link</a>', html_path, tmp_path)
    assert 'href="/page.html"' in text


def test_absolutize_relative_href_from_subdir(tmp_path):
    """A relative href using ../ traversal is resolved to an absolute path."""
    html_path = tmp_path / "blog" / "post" / "index.html"
    html_path.parent.mkdir(parents=True)
    html_path.touch()
    text = absolutize_html_urls('<a href="../../about.html">link</a>', html_path, tmp_path)
    assert 'href="/' in text


def test_absolutize_absolute_href_unchanged(tmp_path):
    """An already-absolute href is left unchanged."""
    html_path = tmp_path / "index.html"
    html_path.touch()
    text = absolutize_html_urls('<a href="/already/absolute">link</a>', html_path, tmp_path)
    assert 'href="/already/absolute"' in text


def test_absolutize_external_href_unchanged(tmp_path):
    """An external (https://) href is not modified."""
    html_path = tmp_path / "index.html"
    html_path.touch()
    text = absolutize_html_urls(
        '<a href="https://external.com/page">link</a>', html_path, tmp_path
    )
    assert 'href="https://external.com/page"' in text


def test_absolutize_strips_index_html_from_internal_links(tmp_path):
    """Internal links ending in /index.html are rewritten to the directory path."""
    html_path = tmp_path / "index.html"
    html_path.touch()
    text = absolutize_html_urls(
        '<a href="/blog/post/index.html">link</a>', html_path, tmp_path
    )
    assert "index.html" not in text
    assert "/blog/post/" in text


def test_absolutize_srcset_entries_resolved(tmp_path):
    """All srcset entries are resolved to absolute paths."""
    html_path = tmp_path / "index.html"
    html_path.touch()
    text = absolutize_html_urls(
        '<img srcset="img-small.png 480w, img-large.png 1080w">',
        html_path,
        tmp_path,
    )
    assert "/img-small.png" in text
    assert "/img-large.png" in text


def test_absolutize_data_href_unchanged(tmp_path):
    """A data: URI in src is not modified."""
    html_path = tmp_path / "index.html"
    html_path.touch()
    text = absolutize_html_urls(
        '<img src="data:image/png;base64,abc">', html_path, tmp_path
    )
    assert "data:image/png;base64,abc" in text


def test_absolutize_does_not_rewrite_href_inside_script(tmp_path):
    """JS template literals like href="${expr}" must not be treated as relative URLs."""
    html_path = tmp_path / "consultoria" / "aplicacao" / "index.html"
    html_path.parent.mkdir(parents=True)
    html_path.touch()
    html = (
        '<script>'
        '`<a href="${slide.btnLink}" class="card-btn">watch</a>`'
        '</script>'
        '<a href="page.html">real link</a>'
    )
    result = absolutize_html_urls(html, html_path, tmp_path)
    # JS template expression must be unchanged
    assert 'href="${slide.btnLink}"' in result
    # Real HTML relative link must be absolutized
    assert 'href="/consultoria/aplicacao/page.html"' in result


def test_absolutize_script_end_tag_with_space_still_splits(tmp_path):
    """Malformed closing tags like </script > still end the script block."""
    html_path = tmp_path / "index.html"
    html_path.touch()
    html = (
        '<script>'
        '`<a href="${slide.btnLink}">watch</a>`'
        '</script >'
        '<a href="page.html">real link</a>'
    )
    result = absolutize_html_urls(html, html_path, tmp_path)
    # JS template expression must be unchanged
    assert 'href="${slide.btnLink}"' in result
    # Real HTML relative link after </script > must be absolutized
    assert 'href="/page.html"' in result
