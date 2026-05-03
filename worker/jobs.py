"""
jobs.py — function executed by the RQ worker for each deploy.
"""

import os
import pathlib
import re
import shutil
import subprocess
from datetime import datetime
from urllib.parse import urlparse

from deploy import invalidate_cloudfront, sync_to_s3

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
    bundle_css      = opts.get("bundle_css",      True)
    bundle_js       = opts.get("bundle_js",        True)
    compress_images = opts.get("compress_images",  True)
    compress_html   = opts.get("compress_html",    True)
    convert_fonts   = opts.get("convert_fonts",    True)

    # Hostname extracted from the URL → S3 prefix and reference for rewriting absolute URLs.
    # e.g. "https://lp.mysite.com/blog/post/" → hostname="lp.mysite.com"
    hostname = urlparse(url).hostname
    page_path = url_to_prefix(url)

    print(f"[job] started: url={url} post_id={post_id} site={hostname} workdir={workdir}")

    try:
        # 1. scrape
        scrape_cmd = ["bash", "/app/scrape.sh", url, str(workdir)]
        if extra_cdn:
            scrape_cmd.append(",".join(extra_cdn))
        scrape = subprocess.run(
            scrape_cmd,
            capture_output=True,
            text=True,
            timeout=300,
        )
        print(scrape.stdout)
        if scrape.returncode != 0:
            print(scrape.stderr)
            raise RuntimeError(f"scrape failed (rc={scrape.returncode}):\n{scrape.stderr[-2000:]}")

        # 1b. CDN assets loaded via JS (createElement + .href/.src = 'https://cdn/...')
        if extra_cdn:
            dyn = download_dynamic_cdn_assets(workdir, extra_cdn)
            print(f"[job] {dyn} dynamic CDN asset(s) downloaded")

        # 2. HTML cleanup — remove absolute references to origin host and each extra CDN
        pp = subprocess.run(
            ["python", "/app/postprocess.py", str(workdir), hostname] + extra_cdn,
            capture_output=True,
            text=True,
        )
        print(pp.stdout)
        if pp.returncode != 0:
            print(pp.stderr)
            print("[job] warning: postprocess failed, continuing anyway")

        # 3. optimization (CSS/JS bundle + minification)
        opt_cmd = ["python", "/app/optimize.py", str(workdir)]
        if not bundle_css:
            opt_cmd.append("--no-bundle-css")
        if not bundle_js:
            opt_cmd.append("--no-bundle-js")
        if not compress_images:
            opt_cmd.append("--no-compress-images")
        if not compress_html:
            opt_cmd.append("--no-compress-html")
        if not convert_fonts:
            opt_cmd.append("--no-convert-fonts")
        opt = subprocess.run(
            opt_cmd,
            capture_output=True,
            text=True,
        )
        print(opt.stdout)
        if opt.returncode != 0:
            print(opt.stderr)
            print("[job] warning: optimize failed, continuing anyway")

        # 4. S3 upload
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
