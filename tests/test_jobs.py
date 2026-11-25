"""Tests for jobs.py — url_to_prefix, CDN asset download, and deploy_page options."""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

from jobs import deploy_page, download_dynamic_cdn_assets, url_to_prefix


# ---------------------------------------------------------------------------
# url_to_prefix
# ---------------------------------------------------------------------------

def test_url_to_prefix_root_url_returns_empty_string():
    """Root URL produces an empty prefix."""
    assert url_to_prefix("https://example.com/") == ""


def test_url_to_prefix_path_returns_stripped_path():
    """URL with a path returns the path stripped of leading and trailing slashes."""
    assert url_to_prefix("https://example.com/blog/my-post/") == "blog/my-post"


def test_url_to_prefix_no_trailing_slash():
    """URL without trailing slash still returns the correct path segment."""
    assert url_to_prefix("https://example.com/page") == "page"


def test_url_to_prefix_nested_path():
    """Nested path is returned without host or slashes."""
    assert url_to_prefix("https://lp.mysite.com/campaigns/promo/") == "campaigns/promo"


def test_url_to_prefix_subdomain_ignored():
    """Subdomain is ignored; root path returns empty string."""
    assert url_to_prefix("https://staging.mysite.com/") == ""


# ---------------------------------------------------------------------------
# download_dynamic_cdn_assets
# ---------------------------------------------------------------------------

def test_download_cdn_assets_no_cdn_returns_zero(tmp_path):
    """Empty CDN list returns zero without downloading anything."""
    assert download_dynamic_cdn_assets(tmp_path, []) == 0


def test_download_cdn_assets_finds_url_in_html_and_downloads(tmp_path, monkeypatch):
    """A CDN URL found in an HTML file is downloaded to the correct local path."""
    html = tmp_path / "index.html"
    html.write_text("var font = 'https://cdn.example.com/assets/font.woff2';")

    def mock_run(cmd, **_kwargs):
        """Fake subprocess.run that writes the downloaded file to the -O target."""
        out_path = Path(cmd[cmd.index("-O") + 1])
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_bytes(b"fake-data")
        result = MagicMock()
        result.returncode = 0
        return result

    monkeypatch.setattr("jobs.subprocess.run", mock_run)

    count = download_dynamic_cdn_assets(tmp_path, ["cdn.example.com"])

    assert count == 1
    assert (tmp_path / "assets" / "font.woff2").exists()


def test_download_cdn_assets_skips_already_downloaded(tmp_path, monkeypatch):
    """A CDN URL whose local file already exists is not downloaded again."""
    html = tmp_path / "index.html"
    html.write_text("var url = 'https://cdn.example.com/assets/img.png';")

    (tmp_path / "assets").mkdir()
    (tmp_path / "assets" / "img.png").write_bytes(b"existing")

    mock_run = MagicMock()
    monkeypatch.setattr("jobs.subprocess.run", mock_run)

    count = download_dynamic_cdn_assets(tmp_path, ["cdn.example.com"])

    assert count == 0
    mock_run.assert_not_called()


def test_download_cdn_assets_deduplicates_urls(tmp_path, monkeypatch):
    """The same CDN URL referenced twice is only downloaded once."""
    html = tmp_path / "index.html"
    html.write_text(
        "var a = 'https://cdn.example.com/img.png';\n"
        "var b = 'https://cdn.example.com/img.png';\n"
    )

    call_count = []

    def mock_run(cmd, **_kwargs):
        """Fake subprocess.run that records the call and writes a stub file."""
        call_count.append(1)
        out_path = Path(cmd[cmd.index("-O") + 1])
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_bytes(b"data")
        result = MagicMock()
        result.returncode = 0
        return result

    monkeypatch.setattr("jobs.subprocess.run", mock_run)

    count = download_dynamic_cdn_assets(tmp_path, ["cdn.example.com"])

    assert count == 1
    assert len(call_count) == 1


def test_download_cdn_assets_ignores_unrelated_cdn(tmp_path, monkeypatch):
    """URLs from a host not in the CDN list are ignored."""
    html = tmp_path / "index.html"
    html.write_text("var url = 'https://other.cdn.com/assets/img.png';")

    mock_run = MagicMock()
    monkeypatch.setattr("jobs.subprocess.run", mock_run)

    count = download_dynamic_cdn_assets(tmp_path, ["cdn.example.com"])

    assert count == 0
    mock_run.assert_not_called()


# ---------------------------------------------------------------------------
# deploy_page — options flags forwarded to optimize.py subprocess
# ---------------------------------------------------------------------------

def _make_mock_run(returncode=0):
    result = MagicMock()
    result.returncode = returncode
    result.stdout = ""
    result.stderr = ""
    return result


@patch("jobs.shutil.rmtree")
@patch("jobs.invalidate_cloudfront", return_value="INV-123")
@patch("jobs.sync_to_s3", return_value={"uploaded": 5, "skipped": 0})
@patch("jobs._run")
def test_deploy_page_no_options_flags_when_all_true(mock_run, _mock_s3, _mock_cf, _mock_rm):
    """When all options are True, no --no-* flags are added to the optimize command."""
    mock_run.return_value = _make_mock_run()

    deploy_page(
        url="https://example.com/",
        post_id=1,
        options={
            "bundle_css": True,
            "bundle_js": True,
            "compress_images": True,
            "compress_html": True,
            "convert_fonts": True,
        },
    )

    optimize_call = [c for c in mock_run.call_args_list if "optimize.py" in str(c)][0]
    optimize_cmd = optimize_call.args[0]
    assert "--no-bundle-css" not in optimize_cmd
    assert "--no-bundle-js" not in optimize_cmd
    assert "--no-compress-images" not in optimize_cmd
    assert "--no-compress-html" not in optimize_cmd
    assert "--no-convert-fonts" not in optimize_cmd


@patch("jobs.shutil.rmtree")
@patch("jobs.invalidate_cloudfront", return_value=None)
@patch("jobs.sync_to_s3", return_value={"uploaded": 3, "skipped": 0})
@patch("jobs._run")
def test_deploy_page_all_false_options_pass_all_no_flags(mock_run, _mock_s3, _mock_cf, _mock_rm):
    """When all options are False, all --no-* flags appear in the optimize command."""
    mock_run.return_value = _make_mock_run()

    deploy_page(
        url="https://example.com/blog/post/",
        post_id=42,
        options={
            "bundle_css": False,
            "bundle_js": False,
            "compress_images": False,
            "compress_html": False,
            "convert_fonts": False,
            "convert_videos": False,
        },
    )

    optimize_call = [c for c in mock_run.call_args_list if "optimize.py" in str(c)][0]
    optimize_cmd = optimize_call.args[0]
    assert "--no-bundle-css" in optimize_cmd
    assert "--no-bundle-js" in optimize_cmd
    assert "--no-compress-images" in optimize_cmd
    assert "--no-compress-html" in optimize_cmd
    assert "--no-convert-fonts" in optimize_cmd
    assert "--no-convert-videos" in optimize_cmd


@patch("jobs.shutil.rmtree")
@patch("jobs.invalidate_cloudfront", return_value=None)
@patch("jobs.sync_to_s3", return_value={"uploaded": 3, "skipped": 0})
@patch("jobs._run")
def test_deploy_page_no_options_defaults_to_all_enabled(mock_run, _mock_s3, _mock_cf, _mock_rm):
    """Omitting options entirely defaults all flags to enabled (no --no-* flags)."""
    mock_run.return_value = _make_mock_run()

    deploy_page(url="https://example.com/", post_id=99)

    optimize_call = [c for c in mock_run.call_args_list if "optimize.py" in str(c)][0]
    optimize_cmd = optimize_call.args[0]
    assert "--no-bundle-css" not in optimize_cmd
    assert "--no-bundle-js" not in optimize_cmd
    assert "--no-compress-html" not in optimize_cmd


def test_build_opt_cmd_passes_s3_cache_args(monkeypatch, tmp_path):
    """The optimize command carries --cache-bucket/--cache-prefix for the video cache."""
    from jobs import _build_opt_cmd

    monkeypatch.setenv("S3_BUCKET", "my-bucket")
    cmd = _build_opt_cmd(tmp_path, {}, hostname="site.com")

    assert "--cache-bucket" in cmd
    assert cmd[cmd.index("--cache-bucket") + 1] == "my-bucket"
    assert "--cache-prefix" in cmd
    assert cmd[cmd.index("--cache-prefix") + 1] == "site.com"


def test_build_opt_cmd_omits_cache_args_without_bucket(monkeypatch, tmp_path):
    """Without S3_BUCKET in the environment, no cache args are passed."""
    from jobs import _build_opt_cmd

    monkeypatch.delenv("S3_BUCKET", raising=False)
    cmd = _build_opt_cmd(tmp_path, {}, hostname="site.com")

    assert "--cache-bucket" not in cmd
    assert "--cache-prefix" not in cmd


# ---------------------------------------------------------------------------
# Build report — workdir snapshot
# ---------------------------------------------------------------------------

def test_snapshot_workdir_classifies_files_by_kind(tmp_path):
    """The snapshot records relative paths with byte sizes and asset kinds."""
    from jobs import _snapshot_workdir

    (tmp_path / "index.html").write_text("<html></html>")
    sub = tmp_path / "assets"
    sub.mkdir()
    (sub / "app.js").write_text("var x=1;")
    (sub / "logo.svg").write_text("<svg/>")

    snapshot = _snapshot_workdir(tmp_path)

    assert snapshot["index.html"] == {"bytes": len("<html></html>"), "kind": "html"}
    assert snapshot["assets/app.js"] == {"bytes": len("var x=1;"), "kind": "js"}
    assert snapshot["assets/logo.svg"]["kind"] == "image"


# ---------------------------------------------------------------------------
# Build report — optimize report loading
# ---------------------------------------------------------------------------

def test_load_optimize_report_missing_file_returns_empty(tmp_path):
    """A missing report (optimize failed or skipped) yields an empty list."""
    from jobs import _load_optimize_report

    assert _load_optimize_report(tmp_path) == []


def test_load_optimize_report_invalid_json_returns_empty(tmp_path):
    """An invalid report is fail-open: warning + empty list, never an exception."""
    from deploy import BUILD_REPORT
    from jobs import _load_optimize_report

    (tmp_path / BUILD_REPORT).write_text("{not valid json")

    assert _load_optimize_report(tmp_path) == []


def test_load_optimize_report_reads_files_list(tmp_path):
    """A valid report yields its files list."""
    from deploy import BUILD_REPORT
    from jobs import _load_optimize_report

    files = [{"path": "a.png", "kind": "image", "action": "converted",
              "original_bytes": 10, "final_bytes": 5}]
    (tmp_path / BUILD_REPORT).write_text(json.dumps({"files": files}))

    assert _load_optimize_report(tmp_path) == files


# ---------------------------------------------------------------------------
# Build report — summary aggregation
# ---------------------------------------------------------------------------

def _sample_snapshot():
    """Pre-optimize workdir baseline covering every asset bucket."""
    return {
        "index.html": {"bytes": 2000, "kind": "html"},
        "img/photo.jpg": {"bytes": 5000, "kind": "image"},
        "css/a.css": {"bytes": 800, "kind": "css"},
        "css/b.css": {"bytes": 700, "kind": "css"},
        "media/clip.mov": {"bytes": 9000, "kind": "video"},
        "media/promo.mov": {"bytes": 4000, "kind": "video"},
        "robots.txt": {"bytes": 100, "kind": "other"},
    }


def _sample_report_files():
    """Per-file optimize report matching _sample_snapshot()."""
    return [
        {"path": "index.html", "kind": "html", "action": "minified",
         "original_bytes": 2000, "final_bytes": 1500},
        {"path": "img/photo.jpg", "kind": "image", "action": "converted",
         "original_bytes": 5000, "final_bytes": 1200},
        {"path": "bundle.abc123def4567890.css", "kind": "css", "action": "bundled",
         "original_bytes": 1500, "final_bytes": 900,
         "sources": [{"path": "css/a.css", "bytes": 800},
                     {"path": "css/b.css", "bytes": 700}]},
        {"path": "media/clip.mov", "kind": "video", "action": "converted",
         "original_bytes": 9000, "final_bytes": 3000},
        {"path": "media/promo.mov", "kind": "video", "action": "cached",
         "original_bytes": 4000, "final_bytes": 1000},
    ]


def test_build_report_summary_aggregates_snapshot_and_report():
    """Snapshot + optimize report aggregate into the exact result.report schema."""
    from jobs import _build_report_summary

    steps = {"scrape": 1.234, "postprocess": 0.05, "optimize": 3.456, "upload": 2.0}
    summary = _build_report_summary(_sample_snapshot(), _sample_report_files(), steps)

    assert set(summary) == {"pages", "assets", "totals", "steps_seconds", "top_savings"}
    assert summary["pages"] == {"count": 1, "bytes": 2000}

    assets = summary["assets"]
    assert list(assets) == ["images", "css", "js", "fonts", "videos", "html", "other"]
    assert assets["images"] == {"count": 1, "original_bytes": 5000, "final_bytes": 1200}
    assert assets["css"] == {"count": 1, "original_bytes": 1500, "final_bytes": 900}
    assert assets["js"] == {"count": 0, "original_bytes": 0, "final_bytes": 0}
    assert assets["fonts"] == {"count": 0, "original_bytes": 0, "final_bytes": 0}
    assert assets["videos"] == {
        "count": 2, "original_bytes": 13000, "final_bytes": 4000,
        "converted": 1, "cache_hits": 1,
    }
    assert assets["html"] == {"count": 1, "original_bytes": 2000, "final_bytes": 1500}
    assert assets["other"] == {"count": 1, "original_bytes": 100, "final_bytes": 100}

    assert summary["totals"] == {
        "files": 6, "original_bytes": 21600, "final_bytes": 7700, "saved_pct": 64.4,
    }
    assert summary["steps_seconds"] == {
        "scrape": 1.2, "postprocess": 0.1, "optimize": 3.5, "upload": 2.0,
    }

    savings = summary["top_savings"]
    assert [s["path"] for s in savings] == [
        "media/clip.mov", "img/photo.jpg", "media/promo.mov",
        "bundle.abc123def4567890.css", "index.html",
    ]
    assert savings[0] == {
        "path": "media/clip.mov", "original_bytes": 9000, "final_bytes": 3000,
    }


def test_build_report_summary_top_savings_capped_at_ten():
    """top_savings holds at most 10 entries, sorted by absolute savings desc."""
    from jobs import _build_report_summary

    snapshot = {f"img/{i}.jpg": {"bytes": 1000, "kind": "image"} for i in range(12)}
    report_files = [
        {"path": f"img/{i}.jpg", "kind": "image", "action": "converted",
         "original_bytes": 1000, "final_bytes": 100 + i * 10}
        for i in range(12)
    ]

    summary = _build_report_summary(snapshot, report_files, {})

    savings = summary["top_savings"]
    assert len(savings) == 10
    amounts = [s["original_bytes"] - s["final_bytes"] for s in savings]
    assert amounts == sorted(amounts, reverse=True)


def test_build_report_summary_without_report_marks_everything_unchanged():
    """With no optimize report, all snapshot files count as unchanged."""
    from jobs import _build_report_summary

    snapshot = {
        "index.html": {"bytes": 1000, "kind": "html"},
        "logo.png": {"bytes": 500, "kind": "image"},
    }
    summary = _build_report_summary(snapshot, [], {})

    assert summary["totals"] == {
        "files": 2, "original_bytes": 1500, "final_bytes": 1500, "saved_pct": 0.0,
    }
    assert not summary["top_savings"]
    assert summary["pages"] == {"count": 1, "bytes": 1000}


def test_build_report_summary_empty_snapshot_saved_pct_zero():
    """An empty snapshot yields zeroed buckets and saved_pct 0.0 (no division error)."""
    from jobs import _build_report_summary

    summary = _build_report_summary({}, [], {})

    assert summary["totals"] == {
        "files": 0, "original_bytes": 0, "final_bytes": 0, "saved_pct": 0.0,
    }
    assert all(
        bucket["count"] == 0 for bucket in summary["assets"].values()
    )


# ---------------------------------------------------------------------------
# Build report — deploy_page integration
# ---------------------------------------------------------------------------

@patch("jobs.shutil.rmtree")
@patch("jobs.invalidate_cloudfront", return_value=None)
@patch("jobs.sync_to_s3", return_value={"uploaded": 1, "skipped": 0})
@patch("jobs._run")
def test_deploy_page_result_includes_build_report(mock_run, _mock_s3, _mock_cf, _mock_rm):
    """The result carries a schema-complete report even when optimize wrote none."""
    mock_run.return_value = _make_mock_run()

    result = deploy_page(url="https://example.com/", post_id=7)

    report = result["report"]
    assert set(report) == {"pages", "assets", "totals", "steps_seconds", "top_savings"}
    assert list(report["assets"]) == ["images", "css", "js", "fonts", "videos", "html", "other"]
    assert set(report["steps_seconds"]) == {"scrape", "postprocess", "optimize", "upload"}

    # The optimize subprocess is always asked to write the build report.
    optimize_call = [c for c in mock_run.call_args_list if "optimize.py" in str(c)][0]
    assert "--report" in optimize_call.args[0]


@patch("jobs.shutil.rmtree")
@patch("jobs.invalidate_cloudfront", return_value=None)
@patch("jobs.sync_to_s3", return_value={"uploaded": 2, "skipped": 0})
@patch("jobs._run")
def test_deploy_page_aggregates_optimize_report(mock_run, _mock_s3, _mock_cf, _mock_rm):
    """A report written by the optimize step is aggregated into result.report."""
    def fake_run(cmd, **_kwargs):
        """Simulates scrape (writes workdir files) and optimize (writes report)."""
        if "scrape.sh" in str(cmd[1]):
            workdir = Path(cmd[3])
            (workdir / "index.html").write_text("<html>  <body>hi</body>  </html>")
            (workdir / "photo.jpg").write_bytes(b"x" * 1000)
        if any("optimize.py" in str(part) for part in cmd):
            report_path = Path(cmd[cmd.index("--report") + 1])
            report_path.write_text(json.dumps({"files": [
                {"path": "photo.jpg", "kind": "image", "action": "converted",
                 "original_bytes": 1000, "final_bytes": 300},
            ]}), encoding="utf-8")
        return _make_mock_run()

    mock_run.side_effect = fake_run

    result = deploy_page(url="https://example.com/", post_id=8)

    report = result["report"]
    assert report["assets"]["images"] == {
        "count": 1, "original_bytes": 1000, "final_bytes": 300,
    }
    assert report["pages"]["count"] == 1
    assert report["top_savings"] == [
        {"path": "photo.jpg", "original_bytes": 1000, "final_bytes": 300},
    ]
