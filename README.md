# Make it static

Service that receives a URL via webhook, captures the rendered page (HTML + static assets) with `wget`, post-processes, optimizes, and publishes to S3 with CloudFront invalidation.

## Architecture

```
Client (any HTTP client, CMS plugin, CI/CD, etc.)
       │  POST /publish  (HMAC-SHA256)
       ▼
   FastAPI ──── enqueue ────► Redis
                                 │
                                 ▼
                              Worker (RQ)
                                 │
                    wget → postprocess → optimize → S3 → CloudFront
```

### Worker pipeline

| Step | Script | What it does |
|------|--------|--------------|
| 1. Scrape | `scrape.sh` | `wget` captures the page and all static assets |
| 2. Postprocess | `postprocess.py` | Removes CMS-specific artifacts, rewrites absolute URLs to relative |
| 3. Optimize | `optimize.py` | Bundles local CSS/JS into minified bundles, minifies HTML |
| 4. Upload | `deploy.py` | Uploads everything to S3 with correct `Content-Type` and `Cache-Control` |
| 5. Invalidate | `deploy.py` | Invalidates the CloudFront cache |

## Components

- **API** (FastAPI): receives webhook, validates HMAC, enqueues job. Endpoints: `POST /publish`, `GET /jobs/{id}`, `DELETE /jobs/{id}`, `GET /health`.
- **Worker** (RQ): consumes the queue and runs the full pipeline.
- **Redis**: job queue.
- **MinIO** (dev only): local S3 replacement.

---

## Running locally

### Prerequisites

- [Docker](https://docs.docker.com/get-docker/) with Docker Compose v2
- `curl` and `openssl` (to run the test script)

### 1. Configure the environment

```bash
cp .env.example .env
```

`.env.example` is already pre-configured to run with MinIO. No AWS credentials are needed in dev. The only field you may want to adjust is `ORIGIN_HOST` — it defines which hostname is stripped from URLs in the HTML:

```bash
# .env
ORIGIN_HOST=staging.yoursite.com   # origin site domain
```

### 2. Start the services

```bash
docker network create make-it-static-network
docker compose up --build
```

Wait for the images to build on the first run. When ready you will see:

```
worker-1     | RQ Worker started. Waiting for jobs in queue 'deploys'...
api-1        | INFO:     Application startup complete.
minio-init-1 | Bucket ready.
```

Available services:

| Service | Address |
|---------|---------|
| API | http://localhost:8123 |
| Site preview | http://localhost:8080 |
| MinIO console | http://localhost:9001 (login: `minioadmin` / `minioadmin`) |
| MinIO S3 API | http://localhost:9000 |

### 3. Check API health

```bash
curl http://localhost:8123/health
```

Expected response:

```json
{"status": "ok", "redis": "ok"}
```

---

## Running a full test

### Step 1 — Publish a page

The `test-publish.sh` script generates the HMAC signature, builds the payload, and fires the `/publish` request:

```bash
./test-publish.sh https://example.com/
```

Example output:

```
[test] payload: {"url":"https://example.com/","post_id":1,"ts":1714230000}
[test] sending to http://localhost:8123/publish
{"job_id":"d3b07384-d113-4e2e-96a8-7b2e3c5f8a21","status":"queued"}
```

Note the `job_id`.

### Step 2 — Track the job status

```bash
curl http://localhost:8123/jobs/<JOB_ID>
```

While the worker is processing, the status will be `started`. When finished:

```json
{
  "job_id": "d3b07384-...",
  "status": "finished",
  "result": {
    "ok": true,
    "url": "https://example.com/",
    "post_id": 1,
    "prefix": "/",
    "files_uploaded": 12,
    "invalidation_id": null,
    "bucket": "my-static-site"
  }
}
```

### Step 3 — View the published files

Access the MinIO console at http://localhost:9001/browser/my-static-site and browse the bucket. Files appear replicating the URL path structure.

Or via CLI:

```bash
# list files in the bucket
docker compose exec minio mc ls local/my-static-site --recursive
```

### Follow worker logs in real time

```bash
docker compose logs -f worker
```

You will see each pipeline step:

```
[job] started: url=https://example.com/ post_id=1 workdir=/tmp/deploys/1-20240427-120000
[postprocess] cleaned: /tmp/deploys/1-20240427-120000/index.html
[postprocess] 1 file(s) modified
[optimize] index.html: 48320 → 31204 bytes
[optimize] done — 1 HTML, 3 CSS, 2 JS processed
[job] 12 file(s) uploaded to s3://my-static-site/
[job] OK: {"ok": true, ...}
```

---

## Running in production (real S3 + CloudFront)

1. Use `docker-compose.prod.yml` (without MinIO):

   ```bash
   docker compose -f docker-compose.prod.yml up -d
   ```

2. Adjust `.env`:

   ```bash
   HMAC_SECRET=$(openssl rand -hex 32)   # generate a new secret
   S3_ENDPOINT_URL=                          # empty = real AWS
   S3_USE_PATH_STYLE=false
   AWS_ACCESS_KEY_ID=<your-key>
   AWS_SECRET_ACCESS_KEY=<your-secret>
   S3_BUCKET=<your-bucket>
   CLOUDFRONT_DISTRIBUTION_ID=E1ABC...
   ORIGIN_HOST=staging.yourdomain.com
   ```

   The minimum required IAM policy is in `IAM_POLICY.json`.

3. Put the API behind TLS (Caddy, nginx, ALB).

4. Point your CMS plugin or deployment trigger to this API.

---

## CloudFront — critical configuration

Three points that commonly cause issues on the first setup:

- **Origin Access Control (OAC)**: private bucket, CloudFront accesses via OAC. The bucket policy must allow `cloudfront.amazonaws.com` with `AWS:SourceArn` of the distribution.
- **Default Root Object**: `index.html`.
- **CloudFront Function (viewer-request)** for directory-style URLs — without it, paths like `/blog/my-post/` return 403:

```javascript
function handler(event) {
  var req = event.request;
  var uri = req.uri;
  if (uri.endsWith('/'))       req.uri = uri + 'index.html';
  else if (!uri.includes('.')) req.uri = uri + '/index.html';
  return req;
}
```

---

## HTTP contract for `/publish` — payload fields

**Request:**

```
POST /publish
Content-Type: application/json
X-Timestamp: <unix epoch>
X-Signature: <hex hmac-sha256 of raw body>

{
  "url": "<absolute URL>",
  "post_id": <int>,
  "ts": <unix epoch>,
  "cloudfront_distribution_id": "<optional>",
  "extra_cdn": ["<domain1>", "<domain2>"],
  "options": {
    "bundle_css":      false,
    "bundle_js":       false,
    "compress_images": false,
    "compress_html":   false,
    "convert_fonts":   false
  }
}
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `url` | string | yes | Absolute URL of the page to publish |
| `post_id` | int | yes | Source content identifier (any integer — used as a job key) |
| `ts` | int | yes | Unix epoch — used in HMAC signature |
| `cloudfront_distribution_id` | string | no | CloudFront distribution ID; falls back to `CLOUDFRONT_DISTRIBUTION_ID` env var if absent |
| `extra_cdn` | string[] | no | CDN domains present in the HTML that should also be downloaded (e.g. `["cdn.example.com", "assets.lib.io"]`) |
| `options` | object | no | Per-request optimization toggles (all default to `true` when omitted) |

**`options` fields:**

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `bundle_css` | bool | `true` | Bundle and minify local CSS files |
| `bundle_js` | bool | `true` | Bundle and minify local JS files |
| `compress_images` | bool | `true` | Convert raster images to AVIF/WebP |
| `compress_html` | bool | `true` | Minify HTML |
| `convert_fonts` | bool | `true` | Convert TTF/OTF fonts to WOFF2 |

**Response:**

```json
{"job_id": "abc...", "status": "queued"}
```

**Status query:** `GET /jobs/{job_id}`

**Cancellation:** `DELETE /jobs/{job_id}`

The replay window is 300 seconds by default (configurable via `HMAC_MAX_SKEW`).

---

## Job cancellation — `DELETE /jobs/{job_id}`

Removes a job from the queue or signals cancellation to the worker, depending on the current state.

| Job state | Behavior |
|-----------|----------|
| `queued` | Removes from the queue immediately |
| `started` | Sends a cancellation signal to the worker (may not interrupt instantly) |
| `finished` / `failed` / others | Returns `409 Conflict` |

**Request:**

```
DELETE /jobs/{job_id}
```

**Response (success):**

```json
{ "id": "abc...", "cancelled": true, "previous_status": "queued" }
```

**Response (job already finished or failed):**

```json
{ "detail": "job cannot be cancelled in status 'finished'" }
```

**Example with curl:**

```bash
curl -X DELETE http://localhost:8123/jobs/<JOB_ID>
```

**Example with Python:**

```python
def cancel_job(job_id: str) -> dict:
    resp = requests.delete(f"{API_URL}/jobs/{job_id}")
    resp.raise_for_status()
    return resp.json()

cancel_job("d3b07384-d113-4e2e-96a8-7b2e3c5f8a21")
# {"id": "d3b07384-...", "cancelled": true, "previous_status": "queued"}
```

---

### Example call — Python

```python
import hashlib
import hmac
import json
import time

import requests

API_URL = "http://localhost:8123"
HMAC_SECRET = "your-secret-here"  # same value as HMAC_SECRET in .env


def publish_page(
    url: str,
    post_id: int,
    cloudfront_distribution_id: str = "",
    extra_cdn: list[str] | None = None,
    options: dict | None = None,
) -> dict:
    payload = {
        "url": url,
        "post_id": post_id,
        "ts": int(time.time()),
        **({"cloudfront_distribution_id": cloudfront_distribution_id} if cloudfront_distribution_id else {}),
        **({"extra_cdn": extra_cdn} if extra_cdn else {}),
        **({"options": options} if options else {}),
    }
    body = json.dumps(payload, separators=(",", ":")).encode()
    ts = str(int(time.time()))
    sig = hmac.new(HMAC_SECRET.encode(), body, hashlib.sha256).hexdigest()

    resp = requests.post(
        f"{API_URL}/publish",
        data=body,
        headers={
            "Content-Type": "application/json",
            "X-Timestamp": ts,
            "X-Signature": sig,
        },
    )
    resp.raise_for_status()
    return resp.json()


def get_job(job_id: str) -> dict:
    return requests.get(f"{API_URL}/jobs/{job_id}").json()


# Trigger the deploy (disabling image and font processing for this site)
result = publish_page(
    "https://www.yoursite.com/blog/my-post/",
    post_id=42,
    extra_cdn=["cdn.yoursite.com", "assets.lib.io"],
    options={"compress_images": False, "convert_fonts": False},
)
print(result)  # {"job_id": "abc...", "status": "queued"}

# Check the status
status = get_job(result["job_id"])
print(status)  # {"status": "finished", "result": {...}}
```

### Example call — Postman

#### body
```json
{
  "url": "https://lp.my-website.com.br/subpage/",
  "post_id": 1,
  "ts": 0,
  "extra_cdn": ["cdn.my-website.com.br"],
  "options": {
    "bundle_css": false,
    "bundle_js": false
  }
}
```

#### scripts > pre-request
```javascript
const secret = "your-app-secret-key"; // HMAC_SECRET from your .env
const ts = Math.floor(Date.now() / 1000).toString();

const body = JSON.stringify({
  url: "https://lp.my-website.com.br/subpage/",
  post_id: 1,
  ts: parseInt(ts),
  extra_cdn: ["cdn.my-website.com.br"], // optional — omit if no extra CDN
  options: { bundle_css: false, bundle_js: false }, // optional — omit to use defaults
});

const sig = CryptoJS.HmacSHA256(body, secret).toString(CryptoJS.enc.Hex);

pm.request.body.raw = body;
pm.request.headers.add({ key: "X-Timestamp", value: ts });
pm.request.headers.add({ key: "X-Signature", value: sig });
pm.request.headers.add({ key: "Content-Type", value: "application/json" });
```

---

## Structure

```
static-deploy-service/
├── docker-compose.yml          # dev (with MinIO)
├── docker-compose.prod.yml     # prod (without MinIO)
├── .env.example
├── IAM_POLICY.json             # minimum IAM policy for the AWS key
├── test-publish.sh             # curl client for manual testing
├── api/
│   ├── main.py                 # FastAPI: /publish, /jobs/{id}, /health
│   └── requirements.txt
└── worker/
    ├── jobs.py                 # pipeline orchestration
    ├── postprocess.py          # HTML cleanup (removes CMS artifacts, rewrites URLs)
    ├── optimize.py             # CSS/JS/HTML bundle + minification
    ├── deploy.py               # S3 upload and CloudFront invalidation
    ├── scrape.sh               # wget wrapper
    └── requirements.txt
```
