---
name: deploy-pipeline
description: Use when modifying the worker pipeline, adding new postprocess/optimize steps, debugging job failures, or working with S3 upload and CloudFront invalidation logic.
---

# Deploy Pipeline — make-it-staticify

## Pipeline overview

```
URL → scrape → dynamic CDN assets → postprocess → webpack chunks → Elementor assets → optimize → S3 → CloudFront
```

Entry: `worker/jobs.py::deploy_page(url, post_id, ...)`

## Step-by-step

### 1. Scrape (`scrape.sh`)
- `wget` captures the page and all static assets.
- Timeout: 300 seconds.
- If `extra_cdn` is provided, passed to `scrape.sh` as comma-separated list.

### 2. Dynamic CDN assets (`jobs.py::download_dynamic_cdn_assets`)
- Downloads assets injected via JavaScript (e.g., `createElement + .src = 'https://cdn/...'`).
- Scans HTML files for literal URL strings matching `extra_cdn` domains.
- Runs **before** postprocess so URLs can be rewritten to local paths.

### 3. Postprocess (`postprocess.py`)
- Removes absolute references to origin host and extra CDN hosts.
- Rewrites URLs to relative paths.
- Normalizes query-string filenames (e.g., `style.css@ver=6.4.1` → `style.css`).
- Failure is **non-fatal** — logs a warning and continues.

### 4. Webpack chunks (`jobs.py::download_webpack_chunks`)
- Downloads lazy-loaded JS chunks missed by `wget`.
- Scans JS files for `__webpack_require__` or `webpackJsonp`.
- Extracts hashed chunk filenames and downloads from origin.
- Runs **after** postprocess so runtime files are already renamed.

### 5. Elementor assets (`jobs.py::download_elementor_dynamic_assets`)
- Downloads Elementor AssetsLoader scripts/styles.
- Scans `frontend.min.js` for `lib/...` patterns.
- Saves clean names (no `?ver=` query) because S3 strips query strings.

### 6. Optimize (`optimize.py`)
- CSS/JS bundling and minification.
- Image conversion (AVIF/WebP).
- Font conversion (TTF/OTF → WOFF2).
- HTML minification.
- Failure is **non-fatal** — logs a warning and continues.

### Options flags
| Flag | Default | `--no-*` flag |
|------|---------|---------------|
| `bundle_css` | `true` | `--no-bundle-css` |
| `bundle_js` | `true` | `--no-bundle-js` |
| `compress_images` | `true` | `--no-compress-images` |
| `compress_html` | `true` | `--no-compress-html` |
| `convert_fonts` | `true` | `--no-convert-fonts` |

### 7. S3 upload (`deploy.py::sync_to_s3`)
- Prefix = hostname from URL (e.g., `lp.mysite.com`).
- Each site is isolated in the same bucket under its own prefix.
- Sets correct `Content-Type` and `Cache-Control`:
  - HTML: `max-age=60, s-maxage=300`
  - Assets: `max-age=31536000, immutable`

### 8. CloudFront invalidation (`deploy.py::invalidate_cloudfront`)
- Invalidates `/{page_path}/*` or `/*` for root.
- Skipped if no `cloudfront_distribution_id` (payload or env var).

## Worker configuration

- Queue: `deploys`
- Job timeout: 600 seconds
- Result TTL: 3600 seconds
- Work root: `/tmp/deploys`
- Cleanup: workdir is deleted after each job (success or failure).

## Adding a new pipeline step

1. Add the function in `worker/jobs.py` (or a new module).
2. Call it in `deploy_page()` between existing steps.
3. Make failures non-fatal (log warning, continue) unless the step is critical.
4. Add tests in `tests/test_jobs.py` using `@patch` for external calls.
5. Update this skill and `AGENTS.md` if the behavior changes externally.

## Debugging job failures

```bash
# Follow worker logs
docker compose logs -f worker

# Check a specific job status
curl http://localhost:8123/jobs/<JOB_ID>

# Re-run a failed job by publishing again with the same URL
```

Common failure modes:
- **Scrape timeout** (300s): Origin site is slow or unreachable.
- **S3 upload errors**: Check credentials and bucket permissions.
- **CloudFront invalidation fails**: Check `CLOUDFRONT_DISTRIBUTION_ID` and IAM policy.
