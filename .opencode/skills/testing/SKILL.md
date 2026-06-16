---
name: testing
description: Use when running tests, writing new tests, debugging test failures, or working with pytest fixtures and coverage in the make-it-static project.
---

# Testing — make-it-static

## Running tests

```bash
# Install ALL three requirements files (not optional)
pip install -r api/requirements.txt -r worker/requirements.txt -r tests/requirements.txt

# Run all tests
pytest tests/ -v

# Run with coverage (matching CI)
pytest tests/ -v --cov=api --cov=worker --cov-report=term-missing --cov-report=xml

# Run a single test file
pytest tests/test_api.py -v

# Run a single test
pytest tests/test_api.py::test_publish_minimal_payload_returns_job_id -v
```

## Test structure

| File | What it tests |
|------|---------------|
| `tests/test_api.py` | FastAPI endpoints: HMAC auth, validation, job lifecycle |
| `tests/test_jobs.py` | Worker pipeline: `url_to_prefix`, CDN download, option flags |
| `tests/test_postprocess.py` | HTML cleanup: URL rewriting, query-string normalization, regex patterns |
| `tests/test_optimize.py` | Asset optimization: CSS/JS bundling, minification, directory optimization |

## Key conventions

- **No external services** in unit tests. Redis is mocked via `unittest.mock` patches. S3 and CloudFront are mocked.
- **`conftest.py`** adds `api/` and `worker/` to `sys.path` and sets `HMAC_SECRET=testsecret`, `REDIS_URL`, `S3_BUCKET`.
- **`test_api.py` patches Redis/Queue BEFORE importing `main`**: 
  ```python
  with patch("redis.Redis.from_url"), patch("rq.Queue"):
      import main
  ```
- Tests use `tmp_path` (pytest built-in) for filesystem fixtures.
- `_sign()` helper in `test_api.py` generates valid HMAC signatures for test payloads.

## Writing new tests

- Use descriptive names: `test_<what>_returns_<expected>`.
- Group related tests with `##` comment sections.
- Mock subprocess calls with `monkeypatch` or `@patch` — never shell out in tests.
- For `postprocess.py` tests: use `tmp_path` to create HTML/CSS files, run the function, assert file contents.
- For `optimize.py` tests: verify bundle files are created (or not), check minification results.

## Coverage

- CI runs with `--cov=api --cov=worker --cov-report=term-missing`.
- `term-missing` shows uncovered lines — use this when adding features to ensure new code is tested.
- Coverage artifact is uploaded as `coverage.xml` in CI.
