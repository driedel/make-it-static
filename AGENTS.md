# AGENTS.md — Make it Static

## Developer commands

```bash
# First-time setup
docker network create make-it-static-network   # REQUIRED — compose won't create it
cp .env.example .env

# Start dev (API + Worker + Redis + MinIO + nginx preview)
docker compose up --build

# Start prod (no MinIO)
docker compose -f docker-compose.prod.yml up -d --build

# Run tests
pip install -r api/requirements.txt -r worker/requirements.txt -r tests/requirements.txt
pytest tests/ -v --cov=api --cov=worker

# Follow worker logs
docker compose logs -f worker
```

## Architecture

```
Client → POST /publish (HMAC-SHA256) → FastAPI (api/) → Redis Queue "deploys" → RQ Worker (worker/)
                                                                          ↓
                                                              scrape → postprocess → optimize → S3 → CloudFront
```

- **api/main.py** — FastAPI app. Endpoints: `POST /publish`, `GET /jobs/{id}`, `DELETE /jobs/{id}`, `GET /health`. Port 8123 (dev) / 8000 (prod, localhost only).
- **worker/jobs.py** — Pipeline orchestration. Entry function: `deploy_page()`.
- **worker/worker.py** — RQ worker entrypoint. Listens on queue `deploys`. Job timeout: 600s.
- **Redis** — Job queue. URL from `REDIS_URL` env var.

## Testing

- Install all three requirements files: `api/`, `worker/`, `tests/`.
- `conftest.py` adds `api/` and `worker/` to `sys.path` and sets test env vars (`HMAC_SECRET=testsecret`).
- No external services needed for unit tests (Redis is mocked via fixtures).
- CI: `pytest tests/ -v --cov=api --cov=worker --cov-report=term-missing --cov-report=xml`

## Key gotchas

- **External network**: `make-it-static-network` must exist before `docker compose up`. Compose declares it as `external: true` and will fail if missing.
- **HMAC auth**: All `/publish` requests require `X-Signature` (hex HMAC-SHA256 of raw body) and `X-Timestamp` headers. Replay window: 300s (configurable via `HMAC_MAX_SKEW`).
- **CORS**: Open (`*`) in dev. Restrict in production.
- **`.env` is gitignored**. Always copy from `.env.example` first.
- **Dev uses MinIO** at `http://minio:9000` with path-style URLs. Prod: leave `S3_ENDPOINT_URL` empty, set `S3_USE_PATH_STYLE=false`.
- **Deploy workflow** (`.github/workflows/deploy.yml`) is manual-only (`workflow_dispatch`). Tests must pass before deploy.

## CI workflows

| Workflow | Trigger | What |
|----------|---------|------|
| `tests.yml` | push/PR to `main` | pytest + coverage |
| `codacy.yml` | push/PR to `main` + weekly cron | Codacy security scan |
| `deploy.yml` | manual dispatch | Run tests, rsync to EC2, restart via `docker-compose.prod.yml` |
