"""
Make it Static — API
- POST   /publish      : receives webhook, validates HMAC, enqueues job
- GET    /jobs/{id}    : query job status
- DELETE /jobs/{id}    : cancel a queued or running job
- GET    /health       : health check
"""

import hashlib
import hmac
import ipaddress
import json
import os
import time
from urllib.parse import urlparse

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from redis import Redis
from rq import Queue
from rq.job import Job

# --- Config ---
SECRET = os.environ["HMAC_SECRET"].encode()
MAX_SKEW = int(os.environ.get("HMAC_MAX_SKEW", "300"))
REDIS_URL = os.environ["REDIS_URL"]

# --- URL validation ---

def _validate_url(url: str) -> None:
    """Rejects non-http(s) URLs and internal IP addresses to prevent SSRF."""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise HTTPException(status_code=400, detail="url must be http or https")
    if not parsed.hostname:
        raise HTTPException(status_code=400, detail="url must have a valid hostname")

    # Reject literal internal IP addresses
    try:
        addr = ipaddress.ip_address(parsed.hostname)
        if addr.is_private or addr.is_loopback or addr.is_link_local or addr.is_reserved:
            raise HTTPException(status_code=400, detail="url must not use internal IP addresses")
    except ValueError:
        pass  # It's a domain name, not an IP literal


# --- Infra ---
app = FastAPI(title="Make it Static API", version="1.0.0")

# CORS: configure allowed origins via CORS_ORIGINS env var (comma-separated).
# Defaults to "*" for dev. Restrict in production.
CORS_ORIGINS = os.environ.get("CORS_ORIGINS", "*").split(",")
app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_methods=["POST", "GET", "DELETE"],
    allow_headers=["*"],
)

redis_conn = Redis.from_url(REDIS_URL)
queue = Queue("deploys", connection=redis_conn, default_timeout=600)


def verify_signature(raw_body: bytes, sig_header: str, ts_header: str) -> None:
    """Validates HMAC-SHA256 of the raw body and enforces a timestamp window against replays."""
    if not sig_header or not ts_header:
        raise HTTPException(status_code=401, detail="missing signature headers")
    try:
        ts = int(ts_header)
    except ValueError:
        raise HTTPException(status_code=401, detail="invalid timestamp")
    if abs(time.time() - ts) > MAX_SKEW:
        raise HTTPException(status_code=401, detail="timestamp out of window")

    expected = hmac.new(SECRET, raw_body, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, sig_header):
        raise HTTPException(status_code=401, detail="bad signature")


@app.get("/health")
def health():
    try:
        redis_conn.ping()
        redis_ok = True
    except Exception:
        redis_ok = False
    return {"ok": True, "redis": redis_ok}


@app.post("/publish")
async def publish(req: Request):
    raw = await req.body()
    verify_signature(
        raw_body=raw,
        sig_header=req.headers.get("x-signature", ""),
        ts_header=req.headers.get("x-timestamp", ""),
    )

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="invalid json")

    url = payload.get("url")
    post_id = payload.get("post_id")
    if not url or not post_id:
        raise HTTPException(status_code=400, detail="url and post_id required")

    _validate_url(url)

    # cloudfront_distribution_id is optional: each site can send its own.
    # Falls back to CLOUDFRONT_DISTRIBUTION_ID env var in the worker if absent.
    cloudfront_distribution_id = payload.get("cloudfront_distribution_id", "")

    # extra_cdn: additional domains for wget to include in the download.
    extra_cdn = payload.get("extra_cdn", [])
    if not isinstance(extra_cdn, list) or not all(isinstance(d, str) for d in extra_cdn):
        raise HTTPException(status_code=400, detail="extra_cdn must be a list of strings")

    # options: per-request optimization toggles (all default to true).
    raw_options = payload.get("options", {})
    if not isinstance(raw_options, dict):
        raise HTTPException(status_code=400, detail="options must be an object")
    options = {
        "bundle_css":      bool(raw_options.get("bundle_css",      True)),
        "bundle_js":       bool(raw_options.get("bundle_js",        True)),
        "compress_images": bool(raw_options.get("compress_images",  True)),
        "compress_html":   bool(raw_options.get("compress_html",    True)),
        "convert_fonts":   bool(raw_options.get("convert_fonts",    True)),
    }

    job = queue.enqueue(
        "jobs.deploy_page",
        url,
        post_id,
        cloudfront_distribution_id,
        extra_cdn,
        options,
        job_timeout=600,
        result_ttl=3600,
    )
    return {"job_id": job.id, "status": "queued"}


@app.delete("/jobs/{job_id}")
def cancel_job(job_id: str):
    try:
        job = Job.fetch(job_id, connection=redis_conn)
    except Exception:
        raise HTTPException(status_code=404, detail="job not found")

    status = job.get_status()
    if status == "queued":
        job.cancel()
        return {"id": job_id, "cancelled": True, "previous_status": "queued"}
    if status == "started":
        job.cancel()
        return {"id": job_id, "cancelled": True, "previous_status": "started"}

    raise HTTPException(
        status_code=409,
        detail=f"job cannot be cancelled in status '{status}'",
    )


@app.get("/jobs/{job_id}")
def job_status(job_id: str):
    try:
        job = Job.fetch(job_id, connection=redis_conn)
    except Exception:
        raise HTTPException(status_code=404, detail="job not found")

    return {
        "id": job.id,
        "status": job.get_status(),
        "result": job.result,
        "error": "Job failed — check server logs" if job.exc_info else None,
        "enqueued_at": job.enqueued_at.isoformat() if job.enqueued_at else None,
        "started_at": job.started_at.isoformat() if job.started_at else None,
        "ended_at": job.ended_at.isoformat() if job.ended_at else None,
    }
