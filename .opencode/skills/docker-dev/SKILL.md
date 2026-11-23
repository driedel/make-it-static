---
name: docker-dev
description: Use when setting up the development environment, running Docker Compose, debugging containers, or switching between dev and production configurations.
---

# Docker Development — make-it-static

## First-time setup

```bash
# REQUIRED: create the external network before compose up
docker network create make-it-static-network

# Copy and customize environment
cp .env.example .env
```

## Dev environment (with LocalStack)

```bash
docker compose up --build
```

Services:
| Service | Address | Notes |
|---------|---------|-------|
| API | http://localhost:8123 | FastAPI on port 8123 |
| Site preview | http://localhost:8080 | nginx proxying to LocalStack |
| LocalStack S3 API | http://localhost:4566 | Path-style URLs; bucket auto-created at startup |

### Verify it's working
```bash
curl http://localhost:8123/health
# Expected: {"ok": true, "redis": true}
```

### View worker logs in real time
```bash
docker compose logs -f worker
```

### Browse uploaded files
The dev S3 (LocalStack) has no web console — use `awslocal` inside the container:
```bash
docker compose exec localstack awslocal s3 ls s3://my-static-site --recursive
```

## Production environment (real S3 + CloudFront)

```bash
docker compose -f docker-compose.prod.yml up -d --build
```

Differences from dev:
- **No LocalStack** — uses real AWS S3.
- **No nginx preview** — CloudFront serves the bucket.
- API exposed only on `127.0.0.1:8000` (Caddy/nginx handles TLS externally).

### Required `.env` changes for prod
```bash
HMAC_SECRET=$(openssl rand -hex 32)
S3_ENDPOINT_URL=                    # empty = real AWS
S3_USE_PATH_STYLE=false
AWS_ACCESS_KEY_ID=<your-key>
AWS_SECRET_ACCESS_KEY=<your-secret>
S3_BUCKET=<your-bucket>
CLOUDFRONT_DISTRIBUTION_ID=E1ABC...
ORIGIN_HOST=staging.yourdomain.com
```

## Critical gotchas

- **External network required**: `make-it-static-network` must exist before any `docker compose` command. Compose declares it as `external: true` and fails if missing.
- **Dev credentials**: LocalStack accepts any credentials in dev; `AWS_ACCESS_KEY_ID`/`AWS_SECRET_ACCESS_KEY` are passed to boto3 but ignored by LocalStack.
- **Bucket auto-creation**: `localstack/init-s3.sh` (mounted into the container at `/etc/localstack/init/ready.d/`) creates the bucket and an anonymous-read policy on startup in dev only.
- **Path-style URLs**: Dev uses `S3_USE_PATH_STYLE=true` for LocalStack; real AWS uses virtual-hosted style.
- **`.env` is gitignored**: Never commit secrets.

## Dockerfiles

- `api/Dockerfile` — FastAPI app. Runs `uvicorn main:app --host 0.0.0.0 --port 8000`.
- `worker/Dockerfile` — RQ worker. Installs `wget`, `gcc`, `python3-dev`; removes build deps after pip install. Sets `chmod +x scrape.sh`.

## Restarting services

```bash
# Rebuild and restart everything
docker compose up --build -d

# Restart just the worker
docker compose restart worker

# Scale workers (if needed)
docker compose up -d --scale worker=3
```
