---
name: security
description: Use when working with HMAC authentication, environment variables, secrets management, AWS credentials, or any security-sensitive configuration in the make-it-staticify project.
---

# Security — make-it-staticify

## HMAC Authentication

All `POST /publish` requests require:
- `X-Timestamp`: Unix epoch (seconds)
- `X-Signature`: Hex HMAC-SHA256 of the **raw request body**

### Rules
- Replay window: `HMAC_MAX_SKEW` seconds (default: 300). Requests outside this window are rejected with 401.
- Signature is compared with `hmac.compare_digest()` to prevent timing attacks.
- Missing or invalid headers → 401.
- Timestamp must be a valid integer.

### Generating a signature
```python
import hashlib, hmac, json, time

body = json.dumps({"url": "...", "post_id": 1, "ts": int(time.time())}, separators=(",", ":")).encode()
sig = hmac.new(HMAC_SECRET.encode(), body, hashlib.sha256).hexdigest()
```

## Environment variables

### `.env` file (gitignored — never commit)
Copy from `.env.example`:
```bash
cp .env.example .env
```

### Secrets that MUST be changed in production
- `HMAC_SECRET`: Generate with `openssl rand -hex 32`. Shared with all clients.
- `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY`: MinIO root credentials in dev; real AWS in prod.

### Dev vs Prod S3 configuration
| Env | `S3_ENDPOINT_URL` | `S3_USE_PATH_STYLE` | `AWS_ACCESS_KEY_ID` |
|-----|-------------------|---------------------|---------------------|
| Dev | `http://minio:9000` | `true` | `minioadmin` |
| Prod | **empty** | `false` | Real AWS key |

## CORS

Dev: `allow_origins=["*"]` (open). 
**Restrict in production** to your client domain(s).

## CloudFront

- `CLOUDFRONT_DISTRIBUTION_ID` is optional per-request via `cloudfront_distribution_id` in payload.
- Falls back to env var if absent.
- Dev: leave empty to skip invalidation.

## CI / GitHub Actions security

- Deploy workflow (`deploy.yml`) requires 4 secrets: `EC2_HOST`, `EC2_USER`, `EC2_SSH_KEY`, `ENV_FILE`.
- `ENV_FILE` secret contains the full production `.env` file — treat as highly sensitive.
- Workflow is **manual-only** (`workflow_dispatch`) by default.

## IAM Policy

Minimum IAM policy for the AWS key is in `IAM_POLICY.json`:
- `s3:PutObject`, `s3:PutObjectAcl`, `s3:DeleteObject`, `s3:ListBucket`, `s3:GetObject` on the target bucket
- `cloudfront:CreateInvalidation` on the target distribution

## Testing security code

- `test_api.py` has comprehensive HMAC tests: missing headers, bad signature, expired timestamp.
- Use `_sign()` helper to generate valid signatures in tests.
- Corrupt signatures with `corrupt=True` to test rejection.
