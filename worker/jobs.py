"""
jobs.py — function executed by the RQ worker for each deploy.
"""
from __future__ import annotations

import os
import pathlib
import re
import shutil
import subprocess
import threading
from datetime import datetime
from urllib.parse import urlparse

from deploy import invalidate_cloudfront, sync_to_s3


def _run(cmd: list[str], timeout: int | None = None) -> subprocess.CompletedProcess:
    """Run a command, streaming stdout+stderr to the console line by line."""
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    lines: list[str] = []

    def _reader():
        for line in proc.stdout:
            line = line.rstrip("\n")
            print(line, flush=True)
            lines.append(line)

    reader_thread = threading.Thread(target=_reader, daemon=True)
    reader_thread.start()
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        reader_thread.join()
        raise
    reader_thread.join()
    return subprocess.CompletedProcess(cmd, proc.returncode, "\n".join(lines), "")


WORK_ROOT = pathlib.Path("/tmp/deploys")
WORK_ROOT.mkdir(exist_ok=True)


def url_to_prefix(url: str) -> str:
    """
    Converts URL → S3 prefix.

    https://staging.mysite.com/blog/my-post/   ->  'blog/my-post'
    https://staging.mysite.com/                ->  ''  (root)
    """
    return urlparse(url).path.strip("/")


def download_dynamic_cdn_assets(workdir: pathlib.Path, extra_cdn: list[str]) -> int:
    """
    Downloads CDN assets that are injected via JavaScript strings (invisible to wget).

    wget only captures assets referenced in standard HTML attributes (href, src). Assets
    injected dynamically — e.g. createElement + .href/.src = 'https://cdn/...' — are
    missed. This function scans the downloaded HTML files, finds those literal URL strings
    pointing to domains in extra_cdn, and downloads them replicating the same path
    structure as the scrape (no host prefix, matching wget's --no-host-directories).

    Must be called BEFORE postprocess, which rewrites these URLs to local paths,
    so that the files already exist when the browser requests them.
    """
    if not extra_cdn:
        return 0

    host_alt = "|".join(re.escape(h) for h in extra_cdn)
    # Captures literal URL strings (single or double quotes) pointing to the CDNs
    url_re = re.compile(
        rf"""['"](https?://(?:{host_alt})(/[^'"?#\s]+))['""]""",
        re.IGNORECASE,
    )

    seen: set[str] = set()
    downloaded = 0

    for html_file in workdir.rglob("*.html"):
        try:
            text = html_file.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue

        for m in url_re.finditer(text):
            full_url, url_path = m.group(1), m.group(2)
            if full_url in seen:
                continue
            seen.add(full_url)

            local_path = workdir / url_path.lstrip("/")
            if local_path.exists():
                continue

            local_path.parent.mkdir(parents=True, exist_ok=True)
            result = subprocess.run(
                ["wget", "-q", "--timeout=30", "--tries=3", "-O", str(local_path), full_url],
                capture_output=True,
            )
            if result.returncode == 0:
                print(f"[job] dynamic CDN asset downloaded: {full_url}")
                downloaded += 1
            else:
                print(f"[job] warning: failed to download dynamic CDN asset {full_url}")
                local_path.unlink(missing_ok=True)

    return downloaded


# Matches chunk filenames with a content hash (16+ hex chars), e.g.:
#   toggle.5a98241a5a40d37968b0.bundle.min.js
#   tabs.3919f4174431c122f3d8.bundle.min.js
_CHUNK_FILENAME = re.compile(r'"([a-zA-Z0-9._-]+\.[a-f0-9]{16,}[^"]*\.js)"')

# Matches Elementor AssetsLoader asset paths in frontend.min.js template literals, e.g.:
#   lib/dialog/dialog${o}.js?ver=4.9.3  →  ('dialog/dialog', '4.9.3')
#   lib/swiper/v8/swiper${o}.js?ver=8.4.5  →  ('swiper/v8/swiper', '8.4.5')
_ELEMENTOR_ASSET = re.compile(
    r'lib/([a-zA-Z0-9/_.-]+?)(?:\$\{[^}]+\})?\.(?:js|css)\?ver=([\d.]+)'
)


def download_webpack_chunks(workdir: pathlib.Path, origin_url: str) -> int:
    """
    Downloads webpack lazy-loaded chunks that wget misses.

    webpack splits code into chunks that are loaded at runtime, not referenced in
    HTML attributes. The runtime JS (webpack.runtime.min.js) contains a chunk map
    (chunk-id → filename). Since the webpack public path is auto-computed from the
    script's own URL, each chunk lives in the same directory as its runtime file.

    This function scans every downloaded JS file that looks like a webpack runtime,
    extracts hashed chunk filenames, and downloads any missing ones from the origin.
    Must be called BEFORE postprocess so the files are present for S3 upload.
    """
    parsed = urlparse(origin_url)
    base_origin = f"{parsed.scheme}://{parsed.netloc}"

    seen: set[str] = set()
    downloaded = 0

    for js_file in workdir.rglob("*.js"):
        try:
            text = js_file.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue

        if "__webpack_require__" not in text and "webpackJsonp" not in text:
            continue

        js_dir = js_file.parent.relative_to(workdir)

        for m in _CHUNK_FILENAME.finditer(text):
            chunk_name = m.group(1)
            local_path = js_file.parent / chunk_name
            chunk_url = f"{base_origin}/{js_dir}/{chunk_name}"

            if chunk_url in seen or local_path.exists():
                seen.add(chunk_url)
                continue
            seen.add(chunk_url)

            result = subprocess.run(
                ["wget", "-q", "--timeout=30", "--tries=3", "-O", str(local_path), chunk_url],
                capture_output=True,
            )
            if result.returncode == 0:
                print(f"[job] webpack chunk downloaded: {chunk_url}")
                downloaded += 1
            else:
                print(f"[job] warning: failed to download webpack chunk {chunk_url}")
                local_path.unlink(missing_ok=True)

    return downloaded


def download_elementor_dynamic_assets(workdir: pathlib.Path, origin_url: str) -> int:
    """
    Downloads Elementor AssetsLoader scripts/styles not referenced in HTML attributes.

    Elementor's AssetsLoader (in frontend.min.js) builds asset URLs at runtime from
    elementorFrontendConfig.urls.assets + a relative lib/ path. Because the full URL
    is never a literal string anywhere, wget misses these files entirely.

    Strategy: scan every downloaded frontend.min.js for AssetsLoader lib/ patterns,
    reconstruct the origin URL, and download missing files to the same assets directory.
    The file is saved with the clean name (no ?ver= query) since S3/CloudFront strips
    query strings when looking up objects.
    """
    parsed = urlparse(origin_url)
    base_origin = f"{parsed.scheme}://{parsed.netloc}"

    seen: set[str] = set()
    downloaded = 0

    for js_file in workdir.rglob("frontend.min.js"):
        # Elementor's frontend.min.js lives inside an assets/js/ directory
        if js_file.parent.name != "js":
            continue

        try:
            text = js_file.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue

        if "AssetsLoader" not in text:
            continue

        assets_dir = js_file.parent.parent.relative_to(workdir)

        for m in _ELEMENTOR_ASSET.finditer(text):
            rel_stem, version = m.group(1), m.group(2)
            ext = ".js" if ".js" in m.group(0) else ".css"
            asset_rel = f"lib/{rel_stem}.min{ext}"
            local_path = workdir / assets_dir / asset_rel
            asset_url = f"{base_origin}/{assets_dir}/{asset_rel}?ver={version}"

            if asset_url in seen or local_path.exists():
                seen.add(asset_url)
                continue
            seen.add(asset_url)

            local_path.parent.mkdir(parents=True, exist_ok=True)
            result = subprocess.run(
                ["wget", "-q", "--timeout=30", "--tries=3", "-O", str(local_path), asset_url],
                capture_output=True,
            )
            if result.returncode == 0:
                print(f"[job] Elementor asset downloaded: {asset_url}")
                downloaded += 1
            else:
                print(f"[job] warning: failed to download Elementor asset {asset_url}")
                local_path.unlink(missing_ok=True)

    return downloaded


def _build_opt_cmd(workdir: pathlib.Path, opts: dict) -> list[str]:
    """Builds the optimize.py subprocess command from the options dict."""
    cmd = ["python", "/app/optimize.py", str(workdir)]
    for flag, arg in [
        ("bundle_css",      "--no-bundle-css"),
        ("bundle_js",       "--no-bundle-js"),
        ("compress_images", "--no-compress-images"),
        ("compress_html",   "--no-compress-html"),
        ("convert_fonts",   "--no-convert-fonts"),
    ]:
        if not opts.get(flag, True):
            cmd.append(arg)
    return cmd


def deploy_page(
    url: str,
    post_id: int,
    cloudfront_distribution_id: str = "",
    extra_cdn: list[str] | None = None,
    options: dict | None = None,
) -> dict:
    """
    Full job: scrape → postprocess → S3 → CloudFront invalidate.

    The hostname is always derived from the received URL — no ORIGIN_HOST in the environment.
    Each site can send its own cloudfront_distribution_id in the payload (optional;
    falls back to CLOUDFRONT_DISTRIBUTION_ID env var if absent).
    extra_cdn: additional domains to include in the download (CDNs present in the HTML).
    """
    timestamp = datetime.utcnow().strftime("%Y%m%d-%H%M%S")
    workdir = WORK_ROOT / f"{post_id}-{timestamp}"
    workdir.mkdir(parents=True)

    extra_cdn = extra_cdn or []
    opts = options or {}

    # Hostname extracted from the URL → S3 prefix and reference for rewriting absolute URLs.
    # e.g. "https://lp.mysite.com/blog/post/" → hostname="lp.mysite.com"
    hostname = urlparse(url).hostname
    page_path = url_to_prefix(url)

    print(f"[job] started: url={url} post_id={post_id} site={hostname} workdir={workdir}")

    try:
        # 1. scrape
        print(f"[job] step 1/4: scraping {url}", flush=True)
        scrape_cmd = ["bash", "/app/scrape.sh", url, str(workdir)]
        if extra_cdn:
            scrape_cmd.append(",".join(extra_cdn))
        scrape = _run(scrape_cmd, timeout=300)
        if scrape.returncode != 0:
            raise RuntimeError(f"scrape failed (rc={scrape.returncode}):\n{scrape.stdout[-2000:]}")

        # 1b. CDN assets loaded via JS (createElement + .href/.src = 'https://cdn/...')
        if extra_cdn:
            dyn = download_dynamic_cdn_assets(workdir, extra_cdn)
            print(f"[job] {dyn} dynamic CDN asset(s) downloaded", flush=True)

        # 2. HTML cleanup — remove absolute references to origin host and each extra CDN
        print("[job] step 2/4: postprocessing HTML", flush=True)
        postprocess = _run(["python", "/app/postprocess.py", str(workdir), hostname] + extra_cdn)
        if postprocess.returncode != 0:
            print("[job] warning: postprocess failed, continuing anyway", flush=True)

        # 2b. webpack lazy-loaded chunks — runs after postprocess so that webpack runtime
        #     files have been renamed (e.g. webpack.runtime.min.js@ver=3.26.3 → .min.js)
        #     and are found by the *.js glob used to detect chunk maps.
        wc = download_webpack_chunks(workdir, url)
        print(f"[job] {wc} webpack chunk(s) downloaded", flush=True)

        # 2c. Elementor AssetsLoader assets (dialog.js, swiper.js, …) — loaded at runtime
        #     via template literals; never appear as literal src= attributes in HTML.
        ea = download_elementor_dynamic_assets(workdir, url)
        print(f"[job] {ea} Elementor dynamic asset(s) downloaded", flush=True)

        # 3. optimization (CSS/JS bundle + minification)
        print("[job] step 3/4: optimizing assets", flush=True)
        opt = _run(_build_opt_cmd(workdir, opts))
        if opt.returncode != 0:
            print("[job] warning: optimize failed, continuing anyway", flush=True)

        # 4. S3 upload
        print("[job] step 4/4: uploading to S3", flush=True)
        # Top-level prefix = hostname → isolates different sites in the same bucket:
        #   workdir/blog/page/index.html → bucket/{hostname}/blog/page/index.html
        #   workdir/assets/style.css     → bucket/{hostname}/assets/style.css
        # In production, set the Origin Path of each CloudFront distribution to "/{hostname}".
        bucket = os.environ["S3_BUCKET"]
        uploaded = sync_to_s3(local_dir=workdir, bucket=bucket, prefix=hostname)
        print(f"[job] {uploaded} file(s) uploaded to s3://{bucket}/{hostname}/")

        # 5. CloudFront invalidation
        # Priority: cloudfront_distribution_id from payload → CLOUDFRONT_DISTRIBUTION_ID env var
        dist_id = cloudfront_distribution_id or os.environ.get("CLOUDFRONT_DISTRIBUTION_ID", "")
        invalidation_paths = [f"/{page_path}/*"] if page_path else ["/*"]
        invalidation_id = invalidate_cloudfront(
            distribution_id=dist_id,
            paths=invalidation_paths,
        )

        result = {
            "ok": True,
            "url": url,
            "post_id": post_id,
            "site": hostname,
            "prefix": page_path or "/",
            "files_uploaded": uploaded,
            "invalidation_id": invalidation_id,
            "bucket": bucket,
        }
        print(f"[job] OK: {result}")
        return result

    except subprocess.TimeoutExpired:
        raise RuntimeError("scrape timed out (300s)")
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
