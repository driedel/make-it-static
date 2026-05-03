#!/usr/bin/env python3
"""
optimize.py — bundles and minifies CSS/JS and minifies HTML.

usage: python optimize.py <OUTPUT_DIR>

For each HTML found:
  - Bundles local CSS files into bundle.[hash].css (rewriting internal url() references)
  - Bundles local JS files into bundle.[hash].js
  - Minifies the resulting HTML (including inline <style> and <script> blocks)

Also minifies standalone CSS/JS files not included in any bundle.
"""

import argparse
import hashlib
import os
import re
import sys
from pathlib import Path

import htmlmin
import rcssmin
import rjsmin
from bs4 import BeautifulSoup


def _file_hash(content: str) -> str:
    return hashlib.md5(content.encode("utf-8")).hexdigest()[:8]


def _rewrite_css_urls(css_content: str, css_path: Path, bundle_path: Path, root: Path) -> str:
    """Rewrites url() references in CSS to be relative to the bundle's location."""
    css_dir = css_path.parent
    bundle_dir = bundle_path.parent.resolve()

    def rewrite(match):
        raw = match.group(1).strip()
        if (raw.startswith('"') and raw.endswith('"')) or (
            raw.startswith("'") and raw.endswith("'")
        ):
            url = raw[1:-1]
            quotes = raw[0]
        else:
            url = raw
            quotes = ""

        # Keep data URIs, protocol-absolute URLs, and fragments unchanged
        if url.startswith(("data:", "http://", "https://", "//", "#")):
            return match.group(0)

        url_path = url.split("?")[0].split("#")[0]
        suffix = url[len(url_path):]

        try:
            # Absolute paths (/assets/...) → resolve from output root
            if url_path.startswith("/"):
                abs_target = (root / url_path.lstrip("/")).resolve()
            else:
                abs_target = (css_dir / url_path).resolve()

            # os.path.relpath supports .. traversal (unlike relative_to)
            rel_path = Path(os.path.relpath(abs_target, bundle_dir))
            return f"url({quotes}{rel_path.as_posix()}{suffix}{quotes})"
        except (ValueError, OSError):
            return match.group(0)

    return re.sub(r"url\(([^)]+)\)", rewrite, css_content)


def _update_refs(
    root: Path,
    old_name: str,
    new_name: str,
    file_exts: tuple[str, ...],
) -> None:
    """Replaces old_name with new_name in text files under root."""
    pattern = re.compile(
        r"(?<=[/\"'(])" + re.escape(old_name) + r'(?=["\'\)\s?#,]|$)',
    )
    for ext in file_exts:
        for fpath in root.rglob(f"*{ext}"):
            try:
                text = fpath.read_text(encoding="utf-8", errors="ignore")
                new_text = pattern.sub(new_name, text)
                if new_text != text:
                    fpath.write_text(new_text, encoding="utf-8")
            except Exception:
                pass


def _convert_fonts(root: Path) -> int:
    """Converts TTF/OTF → WOFF2 and updates references in CSS/HTML."""
    try:
        from fontTools.ttLib import TTFont
    except ImportError:
        print("[optimize] warning: fonttools not installed — font conversion skipped", file=sys.stderr)
        return 0

    fmt_hint = re.compile(r"""format\(\s*['"](?:truetype|opentype)['"]\s*\)""", re.IGNORECASE)
    converted = 0

    for glob_pat in ("*.ttf", "*.otf"):
        for font_path in list(root.rglob(glob_pat)):
            old_name = font_path.name
            woff2_path = font_path.with_suffix(".woff2")
            new_name = woff2_path.name
            try:
                if not woff2_path.exists():
                    font = TTFont(str(font_path))
                    font.flavor = "woff2"
                    font.save(str(woff2_path))
                    font.close()

                font_path.unlink()
                _update_refs(root, old_name, new_name, (".css", ".html"))

                # Fix format() hint only in CSS that already references the new woff2
                for css_path in root.rglob("*.css"):
                    try:
                        css = css_path.read_text(encoding="utf-8", errors="ignore")
                        if new_name not in css:
                            continue
                        fixed = fmt_hint.sub("format('woff2')", css)
                        if fixed != css:
                            css_path.write_text(fixed, encoding="utf-8")
                    except Exception:
                        pass

                converted += 1
                print(f"[optimize] font: {old_name} → {new_name}")
            except Exception as exc:
                print(f"[optimize] warning: could not convert {old_name}: {exc}", file=sys.stderr)

    return converted


def _convert_images(root: Path) -> int:
    """Converts raster images to AVIF (or WebP as fallback) and updates references."""
    try:
        from PIL import Image
    except ImportError:
        print("[optimize] warning: Pillow not installed — image conversion skipped", file=sys.stderr)
        return 0

    raster_globs = ("*.jpg", "*.jpeg", "*.png", "*.gif", "*.bmp", "*.tiff", "*.tif")
    ref_exts = (".html", ".css", ".js")
    converted = 0

    for glob_pat in raster_globs:
        for img_path in list(root.rglob(glob_pat)):
            old_name = img_path.name
            try:
                # Modern version already exists — skip conversion, remove original
                if img_path.with_suffix(".avif").exists() or img_path.with_suffix(".webp").exists():
                    continue

                with Image.open(img_path) as img:
                    # Normalize mode for AVIF/WebP compatibility
                    if img.mode == "P":
                        img = img.convert("RGBA" if "transparency" in img.info else "RGB")
                    elif img.mode not in ("RGB", "RGBA", "L", "LA"):
                        img = img.convert("RGB")

                    new_path = None
                    fmt_label = None

                    # Try AVIF; fall back to WebP if unsupported
                    for suffix, fmt, save_kw in [
                        (".avif", "AVIF", {"quality": 75}),
                        (".webp", "WEBP", {"quality": 80, "method": 6}),
                    ]:
                        candidate = img_path.with_suffix(suffix)
                        try:
                            img.save(str(candidate), fmt, **save_kw)
                            new_path = candidate
                            fmt_label = fmt
                            break
                        except Exception:
                            candidate.unlink(missing_ok=True)

                if new_path is None:
                    print(f"[optimize] warning: could not convert {old_name}", file=sys.stderr)
                    continue

                img_path.unlink()
                new_name = new_path.name
                _update_refs(root, old_name, new_name, ref_exts)
                converted += 1
                print(f"[optimize] image ({fmt_label}): {old_name} → {new_name}")

            except Exception as exc:
                print(f"[optimize] warning: error processing {old_name}: {exc}", file=sys.stderr)

    return converted


def _resolve_asset(ref: str, html_dir: Path, root: Path) -> Path:
    """Resolves href/src to an absolute filesystem path (supports /absolute paths)."""
    clean = ref.split("?")[0].split("#")[0]
    if clean.startswith("/"):
        return (root / clean.lstrip("/")).resolve()
    return (html_dir / clean).resolve()


def _bundle_css(html_path: Path, soup: BeautifulSoup, root: Path) -> int:
    """
    Bundles local CSS files into a single minified bundle and updates the HTML.
    Returns the number of files processed (0 if no local CSS).
    """
    html_dir = html_path.parent
    link_tags = soup.find_all("link", rel="stylesheet")

    local = []
    for tag in link_tags:
        href = tag.get("href", "")
        if not href or href.startswith(("http://", "https://", "//", "data:")):
            continue
        abs_path = _resolve_asset(href, html_dir, root)
        if abs_path.exists() and abs_path.is_file():
            local.append((tag, abs_path))

    if not local:
        return 0

    if len(local) == 1:
        _, css_path = local[0]
        content = css_path.read_text(encoding="utf-8", errors="ignore")
        css_path.write_text(rcssmin.cssmin(content), encoding="utf-8")
        return 1

    # Hash raw content to name the bundle
    raw_combined = "\n".join(
        p.read_text(encoding="utf-8", errors="ignore") for _, p in local
    )
    bundle_path = html_dir / f"bundle.{_file_hash(raw_combined)}.css"

    parts = []
    for _, css_path in local:
        raw = css_path.read_text(encoding="utf-8", errors="ignore")
        parts.append(_rewrite_css_urls(raw, css_path, bundle_path, root))

    bundle_path.write_text(rcssmin.cssmin("\n".join(parts)), encoding="utf-8")

    # Replace the first <link> with the bundle and remove the rest
    new_tag = soup.new_tag(
        "link", rel="stylesheet", href=bundle_path.relative_to(html_dir).as_posix()
    )
    local[0][0].replace_with(new_tag)
    for tag, _ in local[1:]:
        tag.decompose()

    return len(local)


def _insert_after_css(soup: BeautifulSoup, tag) -> None:
    """Inserts tag right after the last <link rel=stylesheet> in head, or at the end of head."""
    head = soup.find("head")
    if head:
        last_css = None
        for el in head.find_all("link", rel="stylesheet"):
            last_css = el
        if last_css:
            last_css.insert_after(tag)
        else:
            head.append(tag)
    else:
        soup.insert(0, tag)


def _bundle_js(html_path: Path, soup: BeautifulSoup, root: Path) -> int:
    """
    Bundles local JS files (except ES modules) into a single minified bundle.
    Returns the number of files processed (0 if no local JS).
    """
    html_dir = html_path.parent
    script_tags = soup.find_all("script", src=True)

    local = []
    for tag in script_tags:
        # ES modules use import/export and cannot be naively concatenated
        if tag.get("type") in ("module", "text/javascript;module"):
            continue
        src = tag.get("src", "")
        if not src or src.startswith(("http://", "https://", "//", "data:")):
            continue
        abs_path = _resolve_asset(src, html_dir, root)
        if abs_path.exists() and abs_path.is_file():
            local.append((tag, abs_path))

    if not local:
        return 0

    if len(local) == 1:
        _, js_path = local[0]
        content = js_path.read_text(encoding="utf-8", errors="ignore")
        js_path.write_text(rjsmin.jsmin(content), encoding="utf-8")
        tag = local[0][0]
        tag.extract()
        _insert_after_css(soup, tag)
        return 1

    parts = [p.read_text(encoding="utf-8", errors="ignore") for _, p in local]
    # ";\n" separator prevents syntax errors when concatenating
    combined = ";\n".join(parts)
    bundle_path = html_dir / f"bundle.{_file_hash(combined)}.js"

    bundle_path.write_text(rjsmin.jsmin(combined), encoding="utf-8")

    for tag, _ in local:
        tag.decompose()
    new_tag = soup.new_tag("script", src=bundle_path.relative_to(html_dir).as_posix())
    _insert_after_css(soup, new_tag)

    return len(local)


def optimize_directory(
    output_dir: str,
    bundle_css: bool = True,
    bundle_js: bool = True,
    compress_images: bool = True,
    compress_html: bool = True,
    convert_fonts: bool = True,
) -> dict:
    root = Path(output_dir)
    stats = {"html": 0, "css": 0, "js": 0, "fonts": 0, "images": 0}

    if convert_fonts:
        stats["fonts"] = _convert_fonts(root)
    if compress_images:
        stats["images"] = _convert_images(root)

    for html_path in root.rglob("*.html"):
        try:
            raw = html_path.read_text(encoding="utf-8", errors="ignore")
            soup = BeautifulSoup(raw, "html.parser")

            if bundle_css:
                stats["css"] += _bundle_css(html_path, soup, root)
            if bundle_js:
                stats["js"] += _bundle_js(html_path, soup, root)

            if compress_html:
                html_out = htmlmin.minify(
                    str(soup),
                    remove_comments=True,
                    remove_empty_space=True,
                    reduce_boolean_attributes=True,
                    remove_optional_attribute_quotes=False,
                )
                html_path.write_text(html_out, encoding="utf-8")
                print(f"[optimize] {html_path.name}: {len(raw)} → {len(html_out)} bytes")
            elif bundle_css or bundle_js:
                html_out = str(soup)
                html_path.write_text(html_out, encoding="utf-8")
            stats["html"] += 1
        except Exception as exc:
            print(f"[optimize] warning: {html_path}: {exc}", file=sys.stderr)

    if bundle_css:
        for css_path in root.rglob("*.css"):
            if css_path.name.startswith("bundle."):
                continue
            try:
                content = css_path.read_text(encoding="utf-8", errors="ignore")
                css_path.write_text(rcssmin.cssmin(content), encoding="utf-8")
            except Exception:
                pass

    if bundle_js:
        for js_path in root.rglob("*.js"):
            if js_path.name.startswith("bundle."):
                continue
            try:
                content = js_path.read_text(encoding="utf-8", errors="ignore")
                js_path.write_text(rjsmin.jsmin(content), encoding="utf-8")
            except Exception:
                pass

    return stats


def main():
    parser = argparse.ArgumentParser(description="Optimize static site assets")
    parser.add_argument("output_dir")
    parser.add_argument("--no-bundle-css",      dest="bundle_css",      action="store_false")
    parser.add_argument("--no-bundle-js",        dest="bundle_js",       action="store_false")
    parser.add_argument("--no-compress-images",  dest="compress_images", action="store_false")
    parser.add_argument("--no-compress-html",    dest="compress_html",   action="store_false")
    parser.add_argument("--no-convert-fonts",    dest="convert_fonts",   action="store_false")
    args = parser.parse_args()

    if not Path(args.output_dir).is_dir():
        print(f"directory does not exist: {args.output_dir}", file=sys.stderr)
        sys.exit(2)

    stats = optimize_directory(
        args.output_dir,
        bundle_css=args.bundle_css,
        bundle_js=args.bundle_js,
        compress_images=args.compress_images,
        compress_html=args.compress_html,
        convert_fonts=args.convert_fonts,
    )
    print(
        f"[optimize] done — "
        f"{stats['html']} HTML, {stats['css']} CSS, {stats['js']} JS, "
        f"{stats['fonts']} fonts, {stats['images']} images processed"
    )


if __name__ == "__main__":
    main()
