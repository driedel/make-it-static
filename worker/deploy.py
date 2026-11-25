"""
deploy.py — uploads to S3 with correct Content-Type / Cache-Control
and optionally invalidates CloudFront. Compatible with real S3 and MinIO.

Uploads are content-addressed: every object carries its sha256 in S3 metadata
(`sha256`, plus `source-sha256`/`encoder` for converted videos). Before
uploading, the local file's hash is compared with the stored metadata and
unchanged files are skipped — repeat deploys of unchanged sites upload almost
nothing. The metadata (not the ETag) is used because multipart uploads make
the ETag a composite value rather than a plain MD5.
"""
from __future__ import annotations

import hashlib
import json
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
    ".webm": "video/webm",
    ".mp4": "video/mp4",
    ".mov": "video/quicktime",
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
    """Returns the Cache-Control header for a path based on its extension (HTML vs. assets)."""
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
    """Returns the Content-Type for a path, using the EXTRA_TYPES table before mimetypes."""
    ext = path.suffix.lower()
    if ext in EXTRA_TYPES:
        return EXTRA_TYPES[ext]
    ctype, _ = mimetypes.guess_type(str(path))
    return ctype or "application/octet-stream"


# Manifest written by optimize.py at the workdir root when videos are converted:
# maps each .webm's site-relative key to {"source-sha256", "encoder"}. sync_to_s3
# merges these into the object's S3 metadata and never uploads the manifest itself.
VIDEO_CACHE_MANIFEST = ".video-cache-manifest.json"

# Build report written by optimize.py (--report) at the workdir root and consumed
# by jobs.py to build the per-job summary. Like the video cache manifest, it is
# an internal pipeline artifact and is never uploaded to S3.
BUILD_REPORT = ".build-report.json"


def file_sha256(path: pathlib.Path) -> str:
    """Returns the hex sha256 of a file, read in chunks (videos can be large)."""
    digest = hashlib.sha256()
    with open(path, "rb") as fp:
        for chunk in iter(lambda: fp.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def object_metadata(s3, bucket: str, key: str) -> dict | None:
    """
    Returns the S3 object's user metadata, or None when the object does not exist.

    Fail-open: any HEAD error (missing object, credentials, network) returns
    None, so callers upload/convert as if there were no cached object.
    """
    try:
        return s3.head_object(Bucket=bucket, Key=key).get("Metadata", {})
    except Exception:  # fail-open cache lookup
        return None


def sync_to_s3(local_dir: pathlib.Path, bucket: str, prefix: str) -> dict:
    """
    Uploads every file under local_dir to s3://{bucket}/{prefix}/ with correct headers.

    Files whose sha256 already matches the metadata of the existing object are
    skipped. Returns {"uploaded": int, "skipped": int}.
    """
    s3 = _s3_client()
    uploaded = 0
    skipped = 0

    video_meta: dict = {}
    manifest_path = local_dir / VIDEO_CACHE_MANIFEST
    if manifest_path.is_file():
        try:
            video_meta = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            print(f"[deploy] warning: could not read video cache manifest: {exc}")

    for path in local_dir.rglob("*"):
        if not path.is_file() or path.name in (VIDEO_CACHE_MANIFEST, BUILD_REPORT):
            continue
        rel = path.relative_to(local_dir).as_posix()
        key = f"{prefix}/{rel}" if prefix else rel

        local_hash = file_sha256(path)
        remote_meta = object_metadata(s3, bucket, key)
        if remote_meta is not None and remote_meta.get("sha256") == local_hash:
            skipped += 1
            continue

        metadata = {"sha256": local_hash, **video_meta.get(rel, {})}
        s3.upload_file(
            Filename=str(path),
            Bucket=bucket,
            Key=key,
            ExtraArgs={
                "ContentType": content_type_for(path),
                "CacheControl": cache_control_for(path),
                "Metadata": metadata,
            },
        )
        uploaded += 1
        print(f"[deploy] s3://{bucket}/{key}")

    print(f"[deploy] {uploaded} uploaded, {skipped} unchanged (skipped)")
    return {"uploaded": uploaded, "skipped": skipped}


def invalidate_cloudfront(distribution_id: str, paths: list[str]) -> str | None:
    """Creates a CloudFront invalidation for the given paths; None if no distribution is set."""
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
