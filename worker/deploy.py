"""
deploy.py — uploads to S3 with correct Content-Type / Cache-Control
and optionally invalidates CloudFront. Compatible with real S3 and MinIO.
"""

import mimetypes
import os
import pathlib
import time

import boto3
from botocore.config import Config

# Extra MIME types not well covered by the mimetypes module
EXTRA_TYPES = {
    ".webp": "image/webp",
    ".woff": "font/woff",
    ".woff2": "font/woff2",
    ".svg": "image/svg+xml",
    ".mjs": "application/javascript",
    ".avif": "image/avif",
}


def _s3_client():
    """S3 client — uses a custom endpoint if set (MinIO in dev)."""
    endpoint = os.environ.get("S3_ENDPOINT_URL", "").strip() or None
    use_path_style = os.environ.get("S3_USE_PATH_STYLE", "").lower() == "true"

    config_kwargs = {"retries": {"max_attempts": 3}}
    if use_path_style:
        config_kwargs["s3"] = {"addressing_style": "path"}

    return boto3.client(
        "s3",
        endpoint_url=endpoint,
        config=Config(**config_kwargs),
    )


def cache_control_for(path: pathlib.Path) -> str:
    ext = path.suffix.lower()
    if ext in (".html", ".htm"):
        return "public, max-age=60, s-maxage=300"
    if ext in (
        ".css", ".js", ".mjs", ".woff", ".woff2", ".png", ".jpg",
        ".jpeg", ".webp", ".avif", ".svg", ".gif", ".mp4", ".webm",
        ".ico",
    ):
        return "public, max-age=31536000, immutable"
    return "public, max-age=3600"


def content_type_for(path: pathlib.Path) -> str:
    ext = path.suffix.lower()
    if ext in EXTRA_TYPES:
        return EXTRA_TYPES[ext]
    ctype, _ = mimetypes.guess_type(str(path))
    return ctype or "application/octet-stream"


def sync_to_s3(local_dir: pathlib.Path, bucket: str, prefix: str) -> int:
    s3 = _s3_client()
    count = 0

    for path in local_dir.rglob("*"):
        if not path.is_file():
            continue
        rel = path.relative_to(local_dir).as_posix()
        key = f"{prefix}/{rel}" if prefix else rel

        s3.upload_file(
            Filename=str(path),
            Bucket=bucket,
            Key=key,
            ExtraArgs={
                "ContentType": content_type_for(path),
                "CacheControl": cache_control_for(path),
            },
        )
        count += 1
        print(f"[deploy] s3://{bucket}/{key}")

    return count


def invalidate_cloudfront(distribution_id: str, paths: list[str]) -> str | None:
    if not distribution_id:
        print("[deploy] CLOUDFRONT_DISTRIBUTION_ID not set — skipping invalidation")
        return None

    cf = boto3.client("cloudfront")
    resp = cf.create_invalidation(
        DistributionId=distribution_id,
        InvalidationBatch={
            "Paths": {"Quantity": len(paths), "Items": paths},
            "CallerReference": str(int(time.time() * 1000)),
        },
    )
    inv_id = resp["Invalidation"]["Id"]
    print(f"[deploy] CloudFront invalidation: {inv_id} ({paths})")
    return inv_id
