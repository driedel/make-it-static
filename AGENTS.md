# AGENTS.md — Make it Staticify

This file is written for AI coding agents. It assumes no prior knowledge of the project. The human-facing documentation lives in `README.md`.

## Developer commands

> **Rule**: all tests and security scans must run inside Docker containers. Never run the test suite or install project dependencies directly on the host machine.

```bash
# First-time setup
docker network create make-it-staticify-network   # REQUIRED — compose won't create it
cp .env.example .env

# Start dev (API + Worker + Redis + MinIO + nginx preview)
docker compose up --build

# Start prod (no MinIO)
docker compose -f docker-compose.prod.yml up -d --build

# Run tests inside a temporary container (worker image does not include api/ or tests/)
docker run --rm \
  -v "$(pwd)/api:/app/api" \
  -v "$(pwd)/worker:/app/worker" \
  -v "$(pwd)/tests:/app/tests" \
  -w /app \
  make-it-staticify-worker \
  bash -c "pip install --quiet -r api/requirements.txt -r worker/requirements.txt -r tests/requirements.txt && pytest tests/ -v --cov=api --cov=worker"

# Run security scans inside a temporary container
docker run --rm \
  -v "$(pwd)/api:/app/api" \
  -v "$(pwd)/worker:/app/worker" \
  -w /app \
  make-it-staticify-worker \
  bash -c "pip install --quiet bandit pip-audit && bandit -r api worker && pip-audit -r api/requirements.txt -r worker/requirements.txt"

# Follow worker logs
docker compose logs -f worker
```

## Project overview

**Make it Staticify** is a webhook-driven service that captures a rendered web page, turns it into a static site, optimizes the assets, and uploads the result to S3 (real AWS S3 or MinIO) with an optional CloudFront invalidation.

A client sends a signed `POST /publish` request with a URL and a `post_id`. The API validates the HMAC signature, enqueues a job in Redis, and an RQ worker executes the pipeline asynchronously.

## Technology stack

- **API**: Python 3.12, FastAPI 0.115, Uvicorn, Redis, RQ
- **Worker**: Python 3.12, RQ, boto3, BeautifulSoup 4, rcssmin, rjsmin, minify-html, fonttools, Pillow
- **External tools inside the worker container**: `wget` (website scraping)
- **Queue / cache**: Redis 7
- **Object storage (dev)**: MinIO (S3-compatible)
- **Static preview (dev)**: nginx with an `envsubst`-processed template
- **Container runtime**: Docker / Docker Compose v2
- **CI / CD**: GitHub Actions (pytest, Codacy security scan, optional EC2 deploy)
- **Target Python version**: 3.12 (the Docker images use `python:3.12-slim`)

## Repository layout

```
.
├── api/
│   ├── main.py              # FastAPI application
│   ├── requirements.txt     # API Python dependencies
│   └── Dockerfile           # python:3.12-slim image
├── worker/
│   ├── worker.py            # RQ worker entrypoint
│   ├── jobs.py              # Pipeline orchestration (deploy_page)
│   ├── scrape.sh            # wget wrapper
│   ├── postprocess.py       # HTML cleanup and URL rewriting
│   ├── optimize.py          # CSS/JS bundling + minification, image/font conversion
│   ├── deploy.py            # S3 upload + CloudFront invalidation
│   ├── requirements.txt     # Worker Python dependencies
│   └── Dockerfile           # python:3.12-slim + wget image
├── tests/
│   ├── conftest.py          # pytest path setup and default env vars
│   ├── test_api.py          # FastAPI endpoint tests
│   ├── test_jobs.py         # Pipeline / jobs.py tests
│   ├── test_optimize.py     # optimize.py tests
│   ├── test_postprocess.py  # postprocess.py tests
│   ├── test_integration.py  # Integration tests for S3, CloudFront, conversions
│   └── requirements.txt     # Test dependencies
├── nginx/
│   └── default.conf.template # Local preview nginx config
├── .github/workflows/
│   ├── tests.yml            # pytest on push / PR
│   ├── codacy.yml           # Codacy security scan
│   └── deploy.yml           # Manual EC2 deploy workflow
├── docker-compose.yml             # Dev stack with Redis + API + Worker + MinIO + nginx preview
├── docker-compose.prod.yml        # Production stack with Redis + API + Worker only
├── docker-compose.wordpress.yml   # Example for adding the service to a WordPress project
├── .env.example                   # Environment variable template
├── IAM_POLICY.json          # Minimum AWS IAM policy for production
└── README.md                # Human-facing documentation
```

## Architecture

```
Client (CMS, CI/CD, curl, etc.)
    │  POST /publish  (HMAC-SHA256 signed)
    ▼
FastAPI (api/main.py)
    │  enqueue
    ▼
Redis queue "deploys"
    │
    ▼
RQ Worker (worker/worker.py)
    │
    ├── scrape.sh          wget captures HTML + static assets
    ├── jobs.py            dynamic CDN / webpack / Elementor asset downloads
    ├── postprocess.py     filename cleanup, URL rewriting, CMS artifact removal
    ├── optimize.py        CSS/JS bundling + minification, image/font compression
    ├── deploy.py          S3 upload with Content-Type / Cache-Control
    └── deploy.py          CloudFront invalidation
```

Key routing / naming conventions:

- The worker derives the hostname from the incoming `url` payload, **not** from an environment variable.
- S3 keys are prefixed with the hostname: `s3://{bucket}/{hostname}/path/to/index.html`.
- The CloudFront distribution for a site should use Origin Path `/{hostname}`.
- The local nginx preview uses `ORIGIN_HOST` from `.env` only to route requests to `/{bucket}/{ORIGIN_HOST}/...`.

## Environment variables

Copy `.env.example` to `.env` and adjust:

| Variable | Purpose |
|----------|---------|
| `HMAC_SECRET` | Shared secret for signing webhook requests. Generate in production with `openssl rand -hex 32`. |
| `HMAC_MAX_SKEW` | Replay tolerance in seconds (default 300). |
| `REDIS_URL` | Redis connection URL, e.g. `redis://redis:6379/0`. |
| `AWS_ACCESS_KEY_ID` | AWS key; also used as MinIO root user in dev. |
| `AWS_SECRET_ACCESS_KEY` | AWS secret; also used as MinIO root password in dev. |
| `AWS_REGION` | AWS region (e.g. `us-east-1`). |
| `S3_BUCKET` | Target S3 bucket name. |
| `S3_ENDPOINT_URL` | Custom S3 endpoint. Leave empty + set `S3_USE_PATH_STYLE=false` for real AWS. |
| `S3_USE_PATH_STYLE` | `true` for MinIO, `false` for AWS virtual-hosted style. |
| `CLOUDFRONT_DISTRIBUTION_ID` | Global fallback distribution for invalidation. Optional per-request override via payload. |
| `CORS_ORIGINS` | Comma-separated list of allowed CORS origins. Defaults to `*` in dev. Restrict in production. |
| `ORIGIN_HOST` | **Local preview only** — hostname used by the dev nginx rewrite. |
| `SCRAPE_INTERNAL_HOSTS` | Comma-separated hosts that appear in scraped HTML and should be rewritten to relative paths. Dev-only. |

## Build and run commands

### Local development

1. Copy environment file:
   ```bash
   cp .env.example .env
   ```

2. Create the external Docker network (required by both compose files):
   ```bash
   docker network create make-it-staticify-network
   ```

3. Start the dev stack:
   ```bash
   docker compose up --build
   ```

4. Verify the API:
   ```bash
   curl http://localhost:8123/health
   ```

Dev services:
- API: http://localhost:8123
- Local static preview: http://localhost:8080
- MinIO console: http://localhost:9001 (login `minioadmin` / `minioadmin` unless changed)
- MinIO S3 API: http://localhost:9000

### Production

Use `docker-compose.prod.yml` (no MinIO, API bound to localhost, TLS handled by Caddy/nginx/ALB):

```bash
docker compose -f docker-compose.prod.yml up -d
```

Production `.env` requirements:
- `HMAC_SECRET` must be strong and private.
- `S3_ENDPOINT_URL` should be empty.
- `S3_USE_PATH_STYLE=false`.
- `CLOUDFRONT_DISTRIBUTION_ID` set or passed per-request.
- `SCRAPE_INTERNAL_HOSTS` should be empty or removed.

### Publishing images to Docker Hub

A separate workflow (`.github/workflows/dockerhub.yml`) builds and pushes the API and Worker images to Docker Hub:

- `daniloriedel/make-it-staticify-api`
- `daniloriedel/make-it-staticify-worker`

**Triggers:**
- Manual (`workflow_dispatch`) — publishes `latest`.
- Git tags matching `v*.*.*` — publishes `latest`, `vX.Y.Z`, and `vX.Y`.

**Required repository secrets:**
- `DOCKERHUB_USERNAME`
- `DOCKERHUB_TOKEN`

The images are multi-arch (`linux/amd64`, `linux/arm64`) and are built from the existing `api/Dockerfile` and `worker/Dockerfile` — the project architecture is preserved.

Use [`docker-compose.wordpress.yml`](docker-compose.wordpress.yml) as a starting point for integrating the published images into another Docker Compose project (for example, alongside WordPress). The WordPress plugin should POST to `http://make-it-staticify-api:8000/publish` inside the Docker network.

## Testing

> **Rule**: all tests must run inside Docker. Do not install dependencies or run `pytest` on the host machine.

The project uses **pytest**. Inside a temporary worker container, run:

```bash
docker run --rm \
  -v "$(pwd)/api:/app/api" \
  -v "$(pwd)/worker:/app/worker" \
  -v "$(pwd)/tests:/app/tests" \
  -w /app \
  make-it-staticify-worker \
  bash -c "pip install --quiet -r api/requirements.txt -r worker/requirements.txt -r tests/requirements.txt && pytest tests/ -v --cov=api --cov=worker --cov-report=term-missing --cov-report=xml"
```

The tests mock Redis/RQ and do **not** require a running Redis or Docker stack for the unit tests. They cover:
- HMAC authentication and payload validation (`test_api.py`)
- Job lifecycle endpoints (`test_api.py`)
- URL validation and SSRF mitigation (`test_api.py`)
- URL-to-prefix conversion and dynamic asset downloading (`test_jobs.py`)
- Optimization option flags (`test_jobs.py`, `test_optimize.py`)
- CSS/JS bundling and minification (`test_optimize.py`)
- Filename normalization, URL rewriting, and HTML absolutization (`test_postprocess.py`)
- S3 upload, CloudFront invalidation, font/image conversion, and subprocess behavior (`test_integration.py`)

The worker container uses **Python 3.12**, which is the target version for this project. Running tests inside Docker avoids host interpreter issues.

## Code style and conventions

- Follow the existing Python style in each module.
- Use type hints where they already appear (e.g. `list[str]`, `pathlib.Path`).
- Keep functions focused and document non-obvious behavior in docstrings (the existing code does this extensively).
- Log pipeline progress with `print(..., flush=True)` inside worker scripts; these lines become container logs.
- Worker scripts are executed as subprocesses from `jobs.py`; keep CLI interfaces stable (`postprocess.py <dir> <host> [<cdn>...]`, `optimize.py <dir> [--no-* flags]`).
- Prefer `pathlib.Path` for filesystem operations.
- When writing regexes for asset discovery, include comments explaining the matched pattern, as done for webpack chunks and Elementor assets.

## Commit conventions

Use the conventional commit format:

```
type(scope): description
```

Examples:

```
feat(courses): add language filter
fix(i18n): correct page_content fallback for zh-CN
chore(deps): update pgx to v5.7.2
```

Common types: `feat`, `fix`, `chore`, `refactor`, `docs`, `test`.

**Important:** write all commit messages in English and do not include any AI
signature or attribution (e.g., no "Generated by ...", "Signed-off-by AI",
"Co-authored-by Assistant", model names, or similar markers).

## Security considerations

- **`HMAC_SECRET` is the only authentication mechanism.** Keep it secret and rotate it periodically.
- The API uses CORS origins from `CORS_ORIGINS` (defaults to `*` for development). Restrict to your client domain in production.
- The `/publish` endpoint validates:
  - Presence of `X-Signature` and `X-Timestamp` headers
  - HMAC-SHA256 signature over the raw request body
  - Timestamp within `HMAC_MAX_SKEW` to prevent replays
  - URL scheme is `http` or `https`
  - URL hostname is not an internal/loopback/link-local/reserved IP address
- `scrape.sh` rejects WordPress admin, REST, feeds, pagination, search, and attachment URLs via `--reject-regex`.
- Google Fonts domains are excluded from scraping because the CSS is User-Agent-specific; those links remain external and load from Google at runtime.
- AWS credentials in dev are reused as MinIO credentials. Change them before exposing MinIO.
- The production deploy workflow uses `environment: production` and manual trigger by default.
- Never commit security scan reports (e.g. `bandit-report.txt`, `pip-audit-*.json`) or private audit documents (e.g. `SECURITY_AUDIT.md`). Run scans inside Docker and keep reports local/private.

## Deployment

A sample GitHub Actions workflow is at `.github/workflows/deploy.yml`. It is **disabled by default** (manual `workflow_dispatch` trigger). Required repository secrets:

- `EC2_HOST`
- `EC2_USER`
- `EC2_SSH_KEY`
- `ENV_FILE`

The workflow runs tests, rsyncs the repository to `/opt/make-it-staticify/` on the EC2 instance, and restarts the production Docker Compose stack.

## Key gotchas

- **External network**: `make-it-staticify-network` must exist before `docker compose up`. Compose declares it as `external: true` and will fail if missing.
- **HMAC auth**: All `/publish` requests require `X-Signature` (hex HMAC-SHA256 of raw body) and `X-Timestamp` headers. Replay window: 300s (configurable via `HMAC_MAX_SKEW`).
- **CORS**: Open (`*`) in dev. Restrict in production via `CORS_ORIGINS`.
- **`.env` is gitignored**. Always copy from `.env.example` first.
- **Dev uses MinIO** at `http://minio:9000` with path-style URLs. Prod: leave `S3_ENDPOINT_URL` empty, set `S3_USE_PATH_STYLE=false`.
- **Deploy workflow** (`.github/workflows/deploy.yml`) is manual-only (`workflow_dispatch`). Tests must pass before deploy.

## CI workflows

| Workflow | Trigger | What |
|----------|---------|------|
| `tests.yml` | push/PR to `main` | pytest + coverage |
| `codacy.yml` | push/PR to `main` + weekly cron | Codacy security scan |
| `dockerhub.yml` | manual dispatch + tags `v*.*.*` | Build and push API + Worker images to Docker Hub |
| `deploy.yml` | manual dispatch | Run tests, rsync to EC2, restart via `docker-compose.prod.yml` |

## Important agent notes

- `README.md` references a `test-publish.sh` script for manual testing. That script does **not** currently exist in the repository.
- The worker derives the site hostname from the `url` field of each request; do not assume `ORIGIN_HOST` is used by worker logic.
- `jobs.py` downloads extra assets in three specialized passes after `wget` and around postprocessing:
  1. Dynamic CDN assets injected via JavaScript strings (`download_dynamic_cdn_assets`)
  2. Webpack lazy-loaded chunks (`download_webpack_chunks`)
  3. Elementor AssetsLoader runtime assets (`download_elementor_dynamic_assets`)
- Postprocessing intentionally does **not** use `wget --convert-links` because that mangled JavaScript template literals; `postprocess.py` handles all URL rewriting instead.
- The project has no `pyproject.toml`, `setup.py`, `setup.cfg`, or `package.json`. Dependency management is done through per-service `requirements.txt` files only.
