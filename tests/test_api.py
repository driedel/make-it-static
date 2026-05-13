"""Tests for the FastAPI publish/jobs API — authentication, validation, and job lifecycle."""

import hashlib
import hmac
import json
import time
from unittest.mock import MagicMock, patch

# Patch Redis/Queue before main.py executes its module-level connections
with patch("redis.Redis.from_url", return_value=MagicMock()), \
     patch("rq.Queue", return_value=MagicMock()):
    import main

from fastapi.testclient import TestClient

SECRET = b"testsecret"
client = TestClient(main.app)


def _sign(payload: dict, ts_offset: int = 0, corrupt: bool = False):
    body = json.dumps(payload, separators=(",", ":")).encode()
    timestamp = str(int(time.time()) + ts_offset)
    sig = hmac.new(SECRET, body, hashlib.sha256).hexdigest()
    if corrupt:
        sig = "0" * len(sig)
    return body, {"X-Timestamp": timestamp, "X-Signature": sig, "Content-Type": "application/json"}


# ---------------------------------------------------------------------------
# /health
# ---------------------------------------------------------------------------

def test_health_returns_ok():
    """Health endpoint returns 200 with ok=True."""
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["ok"] is True


# ---------------------------------------------------------------------------
# /publish — authentication
# ---------------------------------------------------------------------------

def test_publish_no_signature_headers_returns_401():
    """Request without signature headers is rejected with 401."""
    resp = client.post("/publish", content=b"{}", headers={"Content-Type": "application/json"})
    assert resp.status_code == 401


def test_publish_bad_signature_returns_401():
    """Request with a corrupted signature is rejected with 401."""
    payload = {"url": "https://example.com/", "post_id": 1, "ts": int(time.time())}
    body, headers = _sign(payload, corrupt=True)
    resp = client.post("/publish", content=body, headers=headers)
    assert resp.status_code == 401


def test_publish_expired_timestamp_returns_401():
    """Request with an expired timestamp is rejected with 401."""
    payload = {"url": "https://example.com/", "post_id": 1, "ts": int(time.time())}
    body, headers = _sign(payload, ts_offset=-9999)
    resp = client.post("/publish", content=body, headers=headers)
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# /publish — payload validation
# ---------------------------------------------------------------------------

def test_publish_missing_url_returns_400():
    """Payload without 'url' field returns 400 with detail mentioning url."""
    body, headers = _sign({"post_id": 1, "ts": int(time.time())})
    resp = client.post("/publish", content=body, headers=headers)
    assert resp.status_code == 400
    assert "url" in resp.json()["detail"]


def test_publish_missing_post_id_returns_400():
    """Payload without 'post_id' field returns 400."""
    body, headers = _sign({"url": "https://example.com/", "ts": int(time.time())})
    resp = client.post("/publish", content=body, headers=headers)
    assert resp.status_code == 400


def test_publish_extra_cdn_not_list_returns_400():
    """extra_cdn as a plain string (not a list) returns 400."""
    payload = {
        "url": "https://example.com/", "post_id": 1,
        "ts": int(time.time()), "extra_cdn": "notalist",
    }
    body, headers = _sign(payload)
    resp = client.post("/publish", content=body, headers=headers)
    assert resp.status_code == 400
    assert "extra_cdn" in resp.json()["detail"]


def test_publish_extra_cdn_list_of_non_strings_returns_400():
    """extra_cdn containing non-string items returns 400."""
    payload = {
        "url": "https://example.com/", "post_id": 1,
        "ts": int(time.time()), "extra_cdn": [1, 2],
    }
    body, headers = _sign(payload)
    resp = client.post("/publish", content=body, headers=headers)
    assert resp.status_code == 400


def test_publish_options_not_dict_returns_400():
    """options as a string (not a dict) returns 400."""
    payload = {
        "url": "https://example.com/", "post_id": 1,
        "ts": int(time.time()), "options": "invalid",
    }
    body, headers = _sign(payload)
    resp = client.post("/publish", content=body, headers=headers)
    assert resp.status_code == 400
    assert "options" in resp.json()["detail"]


def test_publish_options_as_list_returns_400():
    """options as a list (not a dict) returns 400."""
    payload = {
        "url": "https://example.com/", "post_id": 1,
        "ts": int(time.time()), "options": [False],
    }
    body, headers = _sign(payload)
    resp = client.post("/publish", content=body, headers=headers)
    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# /publish — valid payloads
# ---------------------------------------------------------------------------

def _mock_enqueue():
    mock_job = MagicMock()
    mock_job.id = "test-job-id"
    main.queue.enqueue = MagicMock(return_value=mock_job)
    return mock_job


def test_publish_minimal_payload_returns_job_id():
    """A valid minimal payload returns 200 with job_id and status=queued."""
    _mock_enqueue()
    payload = {"url": "https://example.com/", "post_id": 1, "ts": int(time.time())}
    body, headers = _sign(payload)
    resp = client.post("/publish", content=body, headers=headers)
    assert resp.status_code == 200
    assert resp.json()["job_id"] == "test-job-id"
    assert resp.json()["status"] == "queued"


def test_publish_options_all_false_are_forwarded():
    """All-false options dict is forwarded as-is to the enqueued job."""
    _mock_enqueue()
    payload = {
        "url": "https://example.com/",
        "post_id": 1,
        "ts": int(time.time()),
        "options": {
            "bundle_css": False,
            "bundle_js": False,
            "compress_images": False,
            "compress_html": False,
            "convert_fonts": False,
        },
    }
    body, headers = _sign(payload)
    client.post("/publish", content=body, headers=headers)

    options_arg = main.queue.enqueue.call_args.args[5]
    assert options_arg["bundle_css"] is False
    assert options_arg["bundle_js"] is False
    assert options_arg["compress_images"] is False
    assert options_arg["compress_html"] is False
    assert options_arg["convert_fonts"] is False


def test_publish_omitted_options_default_to_true():
    """When options is omitted, all flags default to True."""
    _mock_enqueue()
    payload = {"url": "https://example.com/", "post_id": 1, "ts": int(time.time())}
    body, headers = _sign(payload)
    client.post("/publish", content=body, headers=headers)

    options_arg = main.queue.enqueue.call_args.args[5]
    assert options_arg["bundle_css"] is True
    assert options_arg["bundle_js"] is True
    assert options_arg["compress_images"] is True
    assert options_arg["compress_html"] is True
    assert options_arg["convert_fonts"] is True


def test_publish_partial_options_remaining_default_to_true():
    """Flags not present in options default to True while specified ones are respected."""
    _mock_enqueue()
    payload = {
        "url": "https://example.com/",
        "post_id": 1,
        "ts": int(time.time()),
        "options": {"bundle_css": False},
    }
    body, headers = _sign(payload)
    client.post("/publish", content=body, headers=headers)

    options_arg = main.queue.enqueue.call_args.args[5]
    assert options_arg["bundle_css"] is False
    assert options_arg["bundle_js"] is True
    assert options_arg["compress_html"] is True


# ---------------------------------------------------------------------------
# GET /jobs/{job_id}
# ---------------------------------------------------------------------------

def test_job_status_not_found_returns_404():
    """Fetching status for a non-existent job returns 404."""
    with patch("main.Job.fetch", side_effect=Exception("not found")):
        resp = client.get("/jobs/nonexistent")
    assert resp.status_code == 404


def test_job_status_returns_fields():
    """A found job returns 200 with id and status fields."""
    mock_job = MagicMock()
    mock_job.id = "abc"
    mock_job.get_status.return_value = "finished"
    mock_job.result = {"ok": True}
    mock_job.exc_info = None
    mock_job.enqueued_at = None
    mock_job.started_at = None
    mock_job.ended_at = None
    with patch("main.Job.fetch", return_value=mock_job):
        resp = client.get("/jobs/abc")
    assert resp.status_code == 200
    data = resp.json()
    assert data["id"] == "abc"
    assert data["status"] == "finished"


# ---------------------------------------------------------------------------
# DELETE /jobs/{job_id}
# ---------------------------------------------------------------------------

def test_cancel_job_not_found_returns_404():
    """Cancelling a non-existent job returns 404."""
    with patch("main.Job.fetch", side_effect=Exception("not found")):
        resp = client.delete("/jobs/nonexistent")
    assert resp.status_code == 404


def test_cancel_queued_job_succeeds():
    """A queued job can be cancelled and returns cancelled=True."""
    mock_job = MagicMock()
    mock_job.get_status.return_value = "queued"
    with patch("main.Job.fetch", return_value=mock_job):
        resp = client.delete("/jobs/some-id")
    assert resp.status_code == 200
    assert resp.json()["cancelled"] is True
    assert resp.json()["previous_status"] == "queued"


def test_cancel_started_job_succeeds():
    """A started (running) job can be cancelled."""
    mock_job = MagicMock()
    mock_job.get_status.return_value = "started"
    with patch("main.Job.fetch", return_value=mock_job):
        resp = client.delete("/jobs/some-id")
    assert resp.status_code == 200
    assert resp.json()["cancelled"] is True


def test_cancel_finished_job_returns_409():
    """Cancelling an already-finished job returns 409."""
    mock_job = MagicMock()
    mock_job.get_status.return_value = "finished"
    with patch("main.Job.fetch", return_value=mock_job):
        resp = client.delete("/jobs/some-id")
    assert resp.status_code == 409


def test_cancel_failed_job_returns_409():
    """Cancelling an already-failed job returns 409."""
    mock_job = MagicMock()
    mock_job.get_status.return_value = "failed"
    with patch("main.Job.fetch", return_value=mock_job):
        resp = client.delete("/jobs/some-id")
    assert resp.status_code == 409
