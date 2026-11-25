#!/usr/bin/env python3
"""
optimize.py — converts media to modern formats, bundles/minifies CSS/JS, minifies HTML.

usage: python optimize.py <OUTPUT_DIR> [--no-* flags] [--cache-bucket B --cache-prefix P]
                          [--report PATH]

For each HTML found:
  - Bundles local CSS files into bundle.[hash].css (rewriting internal url() references)
  - Bundles local JS files into bundle.[hash].js
  - Minifies the resulting HTML (including inline <style> and <script> blocks)

Also converts TTF/OTF fonts to WOFF2, raster images to AVIF/WebP, and non-web
videos (mov/avi/mkv/...) to WebM — replacing originals and rewriting references —
and minifies standalone CSS/JS files not included in any bundle.

When --cache-bucket is given, video conversions are cached in S3 (the uploaded
.webm carries the source's sha256 and the encoder tag in its metadata), so repeat
deploys of an unchanged video skip the ffmpeg run entirely.

When --report is given, a JSON build report is written listing every file the
pipeline touched ({path, kind, action, original_bytes, final_bytes, sources?}),
which jobs.py aggregates into the per-job build summary.
"""

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess  # nosec B404
# subprocess runs ffmpeg for video conversion; never with shell=True.
import sys
from pathlib import Path

import minify_html
import rcssmin
import rjsmin
from bs4 import BeautifulSoup

from deploy import VIDEO_CACHE_MANIFEST, _s3_client, file_sha256, object_metadata


def _file_hash(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Build report (--report)
# ---------------------------------------------------------------------------

# Extension → asset kind mapping for build-report entries. Mirrors the glob
# sets used by the converters below; jobs.py reuses it for the pre-optimize
# workdir snapshot, so both sides classify files identically.
ASSET_KINDS = {
    ".jpg": "image", ".jpeg": "image", ".png": "image", ".gif": "image",
    ".bmp": "image", ".tiff": "image", ".tif": "image", ".webp": "image",
    ".avif": "image", ".svg": "image", ".ico": "image",
    ".css": "css",
    ".js": "js", ".mjs": "js",
    ".ttf": "font", ".otf": "font", ".woff": "font", ".woff2": "font",
    ".eot": "font",
    ".html": "html", ".htm": "html",
}


def asset_kind(path: str | Path) -> str:
    """Classifies a file by extension into a build-report asset kind."""
    ext = Path(path).suffix.lower()
    if ext in VIDEO_EXTS or ext in (".mp4", ".webm"):
        return "video"
    return ASSET_KINDS.get(ext, "other")


def _record(report: list | None, entry: dict) -> None:
    """Appends an entry to the build report when collection is enabled."""
    if report is not None:
        report.append(entry)


def _rel_path(path: Path, root: Path) -> str:
    """Workdir-relative POSIX path for a report entry (best effort)."""
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        # Resolved asset paths (e.g. bundle sources) may not share the
        # unresolved root prefix; retry with both sides resolved.
        try:
            return path.resolve().relative_to(root.resolve()).as_posix()
        except (ValueError, OSError):
            return path.name


def _file_size(path: Path) -> int:
    """File size in bytes, 0 when unreadable (report data is best effort)."""
    try:
        return path.stat().st_size
    except OSError:
        return 0


def _write_report(report_path: Path, files: list) -> None:
    """Writes the build report JSON; a failure is a warning, never fatal."""
    try:
        report_path.write_text(
            json.dumps({"files": files}, indent=2), encoding="utf-8"
        )
        print(f"[optimize] build report written: {report_path} ({len(files)} entries)")
    except OSError as exc:
        print(f"[optimize] warning: could not write build report: {exc}", file=sys.stderr)


def _rewrite_css_urls(css_content: str, css_path: Path, bundle_path: Path, root: Path) -> str:
    """Rewrites url() references in CSS to be relative to the bundle's location."""
    css_dir = css_path.parent
    bundle_dir = bundle_path.parent.resolve()

    def rewrite(match):
        """Rewrites a single url() match to be relative to the bundle's location."""
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
            except OSError as exc:
                print(
                    f"[optimize] warning: could not update refs in {fpath}: {exc}",
                    file=sys.stderr,
                )


def _fix_css_format_hints(root: Path, new_name: str, fmt_hint: re.Pattern) -> None:
    """Rewrites truetype/opentype format() hints to woff2 in CSS referencing new_name."""
    for css_path in root.rglob("*.css"):
        try:
            css = css_path.read_text(encoding="utf-8", errors="ignore")
            if new_name not in css:
                continue
            fixed = fmt_hint.sub("format('woff2')", css)
            if fixed != css:
                css_path.write_text(fixed, encoding="utf-8")
        except OSError as exc:
            print(
                f"[optimize] warning: could not update format hint in {css_path}: {exc}",
                file=sys.stderr,
            )


def _convert_fonts(root: Path, report: list | None = None) -> int:
    """Converts TTF/OTF → WOFF2 and updates references in CSS/HTML."""
    try:
        from fontTools.ttLib import TTFont
    except ImportError:
        print(
            "[optimize] warning: fonttools not installed — font conversion skipped",
            file=sys.stderr,
        )
        return 0

    fmt_hint = re.compile(r"""format\(\s*['"](?:truetype|opentype)['"]\s*\)""", re.IGNORECASE)
    converted = 0

    for glob_pat in ("*.ttf", "*.otf"):
        for font_path in list(root.rglob(glob_pat)):
            old_name = font_path.name
            woff2_path = font_path.with_suffix(".woff2")
            new_name = woff2_path.name
            try:
                original_bytes = _file_size(font_path)
                if not woff2_path.exists():
                    font = TTFont(str(font_path))
                    font.flavor = "woff2"
                    font.save(str(woff2_path))
                    font.close()

                font_path.unlink()
                _update_refs(root, old_name, new_name, (".css", ".html"))

                # Fix format() hint only in CSS that already references the new woff2
                _fix_css_format_hints(root, new_name, fmt_hint)

                converted += 1
                _record(report, {
                    "path": _rel_path(font_path, root),
                    "kind": "font",
                    "action": "converted",
                    "original_bytes": original_bytes,
                    "final_bytes": _file_size(woff2_path),
                })
                print(f"[optimize] font: {old_name} → {new_name}")
            except Exception as exc:
                print(f"[optimize] warning: could not convert {old_name}: {exc}", file=sys.stderr)

    return converted


def _save_as_modern_format(img, img_path: Path) -> tuple[Path, str] | None:
    """Saves img as AVIF, falling back to WebP; returns (new path, format label) or None."""
    for suffix, fmt, save_kw in [
        (".avif", "AVIF", {"quality": 75}),
        (".webp", "WEBP", {"quality": 80, "method": 6}),
    ]:
        candidate = img_path.with_suffix(suffix)
        try:
            img.save(str(candidate), fmt, **save_kw)
            return candidate, fmt
        except Exception:
            candidate.unlink(missing_ok=True)
    return None


def _convert_images(root: Path, report: list | None = None) -> int:
    """Converts raster images to AVIF (or WebP as fallback) and updates references."""
    try:
        from PIL import Image
    except ImportError:
        print(
            "[optimize] warning: Pillow not installed — image conversion skipped",
            file=sys.stderr,
        )
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

                    saved = _save_as_modern_format(img, img_path)

                if saved is None:
                    print(f"[optimize] warning: could not convert {old_name}", file=sys.stderr)
                    continue

                new_path, fmt_label = saved
                original_bytes = _file_size(img_path)
                img_path.unlink()
                new_name = new_path.name
                _update_refs(root, old_name, new_name, ref_exts)
                converted += 1
                _record(report, {
                    "path": _rel_path(img_path, root),
                    "kind": "image",
                    "action": "converted",
                    "original_bytes": original_bytes,
                    "final_bytes": _file_size(new_path),
                })
                print(f"[optimize] image ({fmt_label}): {old_name} → {new_name}")

            except Exception as exc:
                print(f"[optimize] warning: error processing {old_name}: {exc}", file=sys.stderr)

    return converted


# Non-web video formats converted to WebM. .mp4 is intentionally absent (already
# web-friendly H.264 in practice); .webm is the target format and never a source.
VIDEO_EXTS = (".mov", ".m4v", ".avi", ".mkv", ".wmv", ".mpg", ".mpeg")

# Encoder fingerprint stored in the S3 object's metadata alongside the converted
# .webm. Bump it whenever the ffmpeg settings change so stale cached conversions
# (encoded with older settings) are re-encoded instead of reused.
VIDEO_ENCODER_TAG = "vp9-crf33-opus96k-v1"

# Per-file ffmpeg timeout in seconds; oversized/pathological videos are skipped
# with a warning rather than failing the deploy.
VIDEO_FFMPEG_TIMEOUT = 600


def _ffmpeg_convert(video_path: Path, webm_path: Path) -> bool:
    """Encodes video_path to WebM (VP9 + Opus); returns True on success."""
    cmd = [
        "ffmpeg", "-nostdin", "-y",
        "-i", str(video_path),
        "-c:v", "libvpx-vp9", "-crf", "33", "-b:v", "0", "-row-mt", "1",
        "-pix_fmt", "yuv420p",
        "-c:a", "libopus", "-b:a", "96k",
        str(webm_path),
    ]
    try:
        result = subprocess.run(  # nosec B603
            cmd, capture_output=True, text=True, timeout=VIDEO_FFMPEG_TIMEOUT, check=False,
        )
    except subprocess.TimeoutExpired:
        print(
            f"[optimize] warning: ffmpeg timed out on {video_path.name} — keeping original",
            file=sys.stderr,
        )
        return False
    if result.returncode != 0:
        tail = (result.stderr or "").strip().splitlines()[-3:]
        print(
            f"[optimize] warning: ffmpeg failed on {video_path.name}: {' | '.join(tail)}",
            file=sys.stderr,
        )
        return False
    return True


def _video_cache_hit(s3, bucket: str, key: str, source_hash: str) -> bool:
    """True when the S3 object was converted from this exact source with the current encoder."""
    meta = object_metadata(s3, bucket, key)
    return (
        meta is not None
        and meta.get("source-sha256") == source_hash
        and meta.get("encoder") == VIDEO_ENCODER_TAG
    )


def _cached_object_size(s3, bucket: str, key: str) -> int:
    """Best-effort ContentLength of the cached .webm (0 when unknown)."""
    try:
        return int(s3.head_object(Bucket=bucket, Key=key).get("ContentLength", 0))
    except Exception:
        return 0


def _process_video(  # pylint: disable=too-many-arguments,too-many-positional-arguments
    video_path: Path,
    root: Path,
    ref_exts: tuple[str, ...],
    s3,
    cache_bucket: str,
    cache_prefix: str,
    video_meta: dict,
    report: list | None = None,
) -> str | None:
    """
    Converts (or cache-resolves) one video; returns "converted", "cached" or None.

    On "converted", video_meta gains the .webm's cache entry for deploy.py.
    On failure the original is kept and None is returned.
    """
    old_name = video_path.name
    webm_path = video_path.with_suffix(".webm")
    new_name = webm_path.name

    # Target already produced in this deploy — nothing to do
    if webm_path.exists():
        return None

    rel_key = webm_path.relative_to(root).as_posix()
    source_hash = ""
    original_bytes = _file_size(video_path)

    if s3 is not None:
        source_hash = file_sha256(video_path)
        s3_key = f"{cache_prefix}/{rel_key}" if cache_prefix else rel_key
        if _video_cache_hit(s3, cache_bucket, s3_key, source_hash):
            video_path.unlink()
            _update_refs(root, old_name, new_name, ref_exts)
            _record(report, {
                "path": _rel_path(video_path, root),
                "kind": "video",
                "action": "cached",
                "original_bytes": original_bytes,
                # The .webm stays in S3; its size comes from the object's metadata.
                "final_bytes": _cached_object_size(s3, cache_bucket, s3_key),
            })
            print(f"[optimize] video (cached): {old_name} → {new_name} (already in S3)")
            return "cached"

    if not _ffmpeg_convert(video_path, webm_path):
        webm_path.unlink(missing_ok=True)
        return None

    video_path.unlink()
    _update_refs(root, old_name, new_name, ref_exts)
    if s3 is not None:
        # deploy.py merges this into the .webm object's S3 metadata so the
        # next deploy can skip re-encoding an unchanged source.
        video_meta[rel_key] = {"source-sha256": source_hash, "encoder": VIDEO_ENCODER_TAG}
    _record(report, {
        "path": _rel_path(video_path, root),
        "kind": "video",
        "action": "converted",
        "original_bytes": original_bytes,
        "final_bytes": _file_size(webm_path),
    })
    print(f"[optimize] video: {old_name} → {new_name}")
    return "converted"


def _convert_videos(  # pylint: disable=too-many-arguments,too-many-positional-arguments
    root: Path,
    cache_bucket: str = "",
    cache_prefix: str = "",
    report: list | None = None,
) -> tuple[int, int]:
    """
    Converts non-web videos (mov/avi/mkv/...) to WebM and updates references.

    When cache_bucket is set, each converted .webm is looked up in S3 before
    encoding: if the existing object's metadata has the same source sha256 and
    encoder tag, the (expensive) ffmpeg run is skipped — the local source is
    removed and refs rewritten, because the identical .webm is already deployed.
    Returns (converted, cache_hits).
    """
    if shutil.which("ffmpeg") is None:
        print(
            "[optimize] warning: ffmpeg not installed — video conversion skipped",
            file=sys.stderr,
        )
        return 0, 0

    s3 = None
    if cache_bucket:
        try:
            s3 = _s3_client()
        except Exception as exc:  # env issues must not block conversion
            print(
                f"[optimize] warning: video S3 cache unavailable ({exc}) — converting anyway",
                file=sys.stderr,
            )
            cache_bucket = ""

    ref_exts = (".html", ".css", ".js")
    converted = 0
    cached = 0
    video_meta: dict[str, dict[str, str]] = {}

    for ext in VIDEO_EXTS:
        # rglob is case-sensitive on Linux; iPhone/DSLR uploads often use .MOV etc.
        for video_path in list(root.rglob(f"*{ext}")) + list(root.rglob(f"*{ext.upper()}")):
            try:
                outcome = _process_video(
                    video_path, root, ref_exts, s3, cache_bucket, cache_prefix,
                    video_meta, report=report,
                )
            except Exception as exc:
                print(
                    f"[optimize] warning: error processing {video_path.name}: {exc}",
                    file=sys.stderr,
                )
                continue
            if outcome == "converted":
                converted += 1
            elif outcome == "cached":
                cached += 1

    if video_meta:
        try:
            (root / VIDEO_CACHE_MANIFEST).write_text(
                json.dumps(video_meta, indent=2), encoding="utf-8"
            )
        except OSError as exc:
            print(
                f"[optimize] warning: could not write video cache manifest: {exc}",
                file=sys.stderr,
            )

    return converted, cached


def _resolve_asset(ref: str, html_dir: Path, root: Path) -> Path:
    """Resolves href/src to an absolute filesystem path (supports /absolute paths)."""
    clean = ref.split("?")[0].split("#")[0]
    if clean.startswith("/"):
        return (root / clean.lstrip("/")).resolve()
    return (html_dir / clean).resolve()


def _find_local_assets(tags, attr: str, html_dir: Path, root: Path) -> list:
    """Collects (tag, absolute path) pairs for tags whose attr references an existing local file."""
    local = []
    for tag in tags:
        ref = tag.get(attr, "")
        if not ref or ref.startswith(("http://", "https://", "//", "data:")):
            continue
        abs_path = _resolve_asset(ref, html_dir, root)
        if abs_path.exists() and abs_path.is_file():
            local.append((tag, abs_path))
    return local


def _bundle_css(
    html_path: Path, soup: BeautifulSoup, root: Path, report: list | None = None
) -> int:
    """
    Bundles local CSS files into a single minified bundle and updates the HTML.
    Returns the number of files processed (0 if no local CSS).
    """
    html_dir = html_path.parent
    link_tags = soup.find_all("link", rel="stylesheet")
    local = _find_local_assets(link_tags, "href", html_dir, root)

    if not local:
        return 0

    if len(local) == 1:
        _, css_path = local[0]
        original_bytes = _file_size(css_path)
        content = css_path.read_text(encoding="utf-8", errors="ignore")
        css_path.write_text(rcssmin.cssmin(content), encoding="utf-8")
        _record(report, {
            "path": _rel_path(css_path, root),
            "kind": "css",
            "action": "minified",
            "original_bytes": original_bytes,
            "final_bytes": _file_size(css_path),
        })
        return 1

    # Hash raw content to name the bundle
    raw_combined = "\n".join(
        p.read_text(encoding="utf-8", errors="ignore") for _, p in local
    )
    bundle_path = html_dir / f"bundle.{_file_hash(raw_combined)}.css"

    parts = []
    sources = []
    for _, css_path in local:
        raw = css_path.read_text(encoding="utf-8", errors="ignore")
        parts.append(_rewrite_css_urls(raw, css_path, bundle_path, root))
        sources.append({"path": _rel_path(css_path, root), "bytes": _file_size(css_path)})

    bundle_path.write_text(rcssmin.cssmin("\n".join(parts)), encoding="utf-8")

    # Replace the first <link> with the bundle and remove the rest
    new_tag = soup.new_tag(
        "link", rel="stylesheet", href=bundle_path.relative_to(html_dir).as_posix()
    )
    local[0][0].replace_with(new_tag)
    for tag, _ in local[1:]:
        tag.decompose()

    _record(report, {
        "path": _rel_path(bundle_path, root),
        "kind": "css",
        "action": "bundled",
        "original_bytes": sum(src["bytes"] for src in sources),
        "final_bytes": _file_size(bundle_path),
        "sources": sources,
    })

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


def _bundle_js(html_path: Path, soup: BeautifulSoup, root: Path, report: list | None = None) -> int:
    """
    Bundles local JS files (except ES modules) into a single minified bundle.
    Returns the number of files processed (0 if no local JS).
    """
    html_dir = html_path.parent
    script_tags = soup.find_all("script", src=True)

    # ES modules use import/export and cannot be naively concatenated
    concatenable = [
        tag for tag in script_tags
        if tag.get("type") not in ("module", "text/javascript;module")
    ]
    local = _find_local_assets(concatenable, "src", html_dir, root)

    if not local:
        return 0

    if len(local) == 1:
        _, js_path = local[0]
        original_bytes = _file_size(js_path)
        content = js_path.read_text(encoding="utf-8", errors="ignore")
        js_path.write_text(rjsmin.jsmin(content), encoding="utf-8")
        tag = local[0][0]
        tag.extract()
        _insert_after_css(soup, tag)
        _record(report, {
            "path": _rel_path(js_path, root),
            "kind": "js",
            "action": "minified",
            "original_bytes": original_bytes,
            "final_bytes": _file_size(js_path),
        })
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

    sources = [
        {"path": _rel_path(js_path, root), "bytes": _file_size(js_path)}
        for _, js_path in local
    ]
    _record(report, {
        "path": _rel_path(bundle_path, root),
        "kind": "js",
        "action": "bundled",
        "original_bytes": sum(src["bytes"] for src in sources),
        "final_bytes": _file_size(bundle_path),
        "sources": sources,
    })

    return len(local)


def _minify_remaining_css(root: Path, report: list | None = None) -> None:
    """Minifies standalone CSS files that were not included in any bundle."""
    for css_path in root.rglob("*.css"):
        if css_path.name.startswith("bundle."):
            continue
        try:
            content = css_path.read_text(encoding="utf-8", errors="ignore")
            minified = rcssmin.cssmin(content)
            if minified == content:
                continue
            original_bytes = _file_size(css_path)
            css_path.write_text(minified, encoding="utf-8")
            _record(report, {
                "path": _rel_path(css_path, root),
                "kind": "css",
                "action": "minified",
                "original_bytes": original_bytes,
                "final_bytes": _file_size(css_path),
            })
        except OSError as exc:
            print(f"[optimize] warning: could not minify {css_path}: {exc}", file=sys.stderr)


def _minify_remaining_js(root: Path, report: list | None = None) -> None:
    """Minifies standalone JS files that were not included in any bundle."""
    for js_path in root.rglob("*.js"):
        if js_path.name.startswith("bundle."):
            continue
        try:
            content = js_path.read_text(encoding="utf-8", errors="ignore")
            minified = rjsmin.jsmin(content)
            if minified == content:
                continue
            original_bytes = _file_size(js_path)
            js_path.write_text(minified, encoding="utf-8")
            _record(report, {
                "path": _rel_path(js_path, root),
                "kind": "js",
                "action": "minified",
                "original_bytes": original_bytes,
                "final_bytes": _file_size(js_path),
            })
        except OSError as exc:
            print(f"[optimize] warning: could not minify {js_path}: {exc}", file=sys.stderr)


def optimize_directory(  # pylint: disable=too-many-arguments,too-many-positional-arguments
    output_dir: str,
    bundle_css: bool = True,
    bundle_js: bool = True,
    compress_images: bool = True,
    compress_html: bool = True,
    convert_fonts: bool = True,
    convert_videos: bool = True,
    cache_bucket: str = "",
    cache_prefix: str = "",
    report_path: str = "",
) -> dict:
    """Runs all optimizations (fonts, images, videos, CSS/JS bundling, HTML minification)."""
    root = Path(output_dir)
    stats = {"html": 0, "css": 0, "js": 0, "fonts": 0, "images": 0, "videos": 0, "videos_cached": 0}
    # The collector is only allocated when a report was requested, so the
    # record calls scattered through the converters stay no-ops otherwise.
    report: list | None = [] if report_path else None

    if convert_fonts:
        stats["fonts"] = _convert_fonts(root, report=report)
    if compress_images:
        stats["images"] = _convert_images(root, report=report)
    if convert_videos:
        stats["videos"], stats["videos_cached"] = _convert_videos(
            root, cache_bucket=cache_bucket, cache_prefix=cache_prefix, report=report
        )

    for html_path in root.rglob("*.html"):
        try:
            raw = html_path.read_text(encoding="utf-8", errors="ignore")
            soup = BeautifulSoup(raw, "html.parser")

            if bundle_css:
                stats["css"] += _bundle_css(html_path, soup, root, report=report)
            if bundle_js:
                stats["js"] += _bundle_js(html_path, soup, root, report=report)

            if compress_html:
                original_bytes = _file_size(html_path)
                html_out = minify_html.minify(
                    str(soup),
                    keep_comments=False,
                    minify_css=True,
                    minify_js=True,
                )
                html_path.write_text(html_out, encoding="utf-8")
                _record(report, {
                    "path": _rel_path(html_path, root),
                    "kind": "html",
                    "action": "minified",
                    "original_bytes": original_bytes,
                    "final_bytes": _file_size(html_path),
                })
                print(f"[optimize] {html_path.name}: {len(raw)} → {len(html_out)} bytes")
            elif bundle_css or bundle_js:
                html_path.write_text(str(soup), encoding="utf-8")
            stats["html"] += 1
        except Exception as exc:
            print(f"[optimize] warning: {html_path}: {exc}", file=sys.stderr)

    if bundle_css:
        _minify_remaining_css(root, report=report)
    if bundle_js:
        _minify_remaining_js(root, report=report)

    if report_path:
        _write_report(Path(report_path), report)

    return stats


def main():
    """CLI entrypoint: parses --no-* flags and runs optimize_directory()."""
    parser = argparse.ArgumentParser(description="Optimize static site assets")
    parser.add_argument("output_dir")
    parser.add_argument("--no-bundle-css",      dest="bundle_css",      action="store_false")
    parser.add_argument("--no-bundle-js",        dest="bundle_js",       action="store_false")
    parser.add_argument("--no-compress-images",  dest="compress_images", action="store_false")
    parser.add_argument("--no-compress-html",    dest="compress_html",   action="store_false")
    parser.add_argument("--no-convert-fonts",    dest="convert_fonts",   action="store_false")
    parser.add_argument("--no-convert-videos",   dest="convert_videos",  action="store_false")
    parser.add_argument("--cache-bucket",        default="",
                        help="S3 bucket of previous deploys (enables the video cache)")
    parser.add_argument("--cache-prefix",        default="",
                        help="S3 key prefix of this site (usually the hostname)")
    parser.add_argument("--report",              default="",
                        help="Write a JSON build report of every touched file to this path")
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
        convert_videos=args.convert_videos,
        cache_bucket=args.cache_bucket,
        cache_prefix=args.cache_prefix,
        report_path=args.report,
    )
    print(
        f"[optimize] done — "
        f"{stats['html']} HTML, {stats['css']} CSS, {stats['js']} JS, "
        f"{stats['fonts']} fonts, {stats['images']} images, "
        f"{stats['videos']} videos ({stats['videos_cached']} from cache) processed"
    )


if __name__ == "__main__":
    main()
