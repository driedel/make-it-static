from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pytest

from jobs import download_dynamic_cdn_assets, url_to_prefix


# ---------------------------------------------------------------------------
# url_to_prefix
# ---------------------------------------------------------------------------

def test_url_to_prefix_root_url_returns_empty_string():
    assert url_to_prefix("https://example.com/") == ""


def test_url_to_prefix_path_returns_stripped_path():
    assert url_to_prefix("https://example.com/blog/my-post/") == "blog/my-post"


def test_url_to_prefix_no_trailing_slash():
    assert url_to_prefix("https://example.com/page") == "page"


def test_url_to_prefix_nested_path():
    assert url_to_prefix("https://lp.mysite.com/campaigns/promo/") == "campaigns/promo"


def test_url_to_prefix_subdomain_ignored():
    assert url_to_prefix("https://staging.mysite.com/") == ""


# ---------------------------------------------------------------------------
# download_dynamic_cdn_assets
# ---------------------------------------------------------------------------

def test_download_cdn_assets_no_cdn_returns_zero(tmp_path):
    assert download_dynamic_cdn_assets(tmp_path, []) == 0


def test_download_cdn_assets_finds_url_in_html_and_downloads(tmp_path, monkeypatch):
    html = tmp_path / "index.html"
    html.write_text("var font = 'https://cdn.example.com/assets/font.woff2';")

    def mock_run(cmd, **kwargs):
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
    html = tmp_path / "index.html"
    html.write_text("var url = 'https://cdn.example.com/assets/img.png';")

    # Pre-create the file so the download is skipped
    (tmp_path / "assets").mkdir()
    (tmp_path / "assets" / "img.png").write_bytes(b"existing")

    mock_run = MagicMock()
    monkeypatch.setattr("jobs.subprocess.run", mock_run)

    count = download_dynamic_cdn_assets(tmp_path, ["cdn.example.com"])

    assert count == 0
    mock_run.assert_not_called()


def test_download_cdn_assets_deduplicates_urls(tmp_path, monkeypatch):
    html = tmp_path / "index.html"
    # Same URL referenced twice
    html.write_text(
        "var a = 'https://cdn.example.com/img.png';\n"
        "var b = 'https://cdn.example.com/img.png';\n"
    )

    call_count = []

    def mock_run(cmd, **kwargs):
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
@patch("jobs.sync_to_s3", return_value=5)
@patch("jobs._run")
def test_deploy_page_no_options_flags_when_all_true(mock_run, mock_s3, mock_cf, mock_rm):
    mock_run.return_value = _make_mock_run()
    from jobs import deploy_page

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
@patch("jobs.sync_to_s3", return_value=3)
@patch("jobs._run")
def test_deploy_page_all_false_options_pass_all_no_flags(mock_run, mock_s3, mock_cf, mock_rm):
    mock_run.return_value = _make_mock_run()
    from jobs import deploy_page

    deploy_page(
        url="https://example.com/blog/post/",
        post_id=42,
        options={
            "bundle_css": False,
            "bundle_js": False,
            "compress_images": False,
            "compress_html": False,
            "convert_fonts": False,
        },
    )

    optimize_call = [c for c in mock_run.call_args_list if "optimize.py" in str(c)][0]
    optimize_cmd = optimize_call.args[0]
    assert "--no-bundle-css" in optimize_cmd
    assert "--no-bundle-js" in optimize_cmd
    assert "--no-compress-images" in optimize_cmd
    assert "--no-compress-html" in optimize_cmd
    assert "--no-convert-fonts" in optimize_cmd


@patch("jobs.shutil.rmtree")
@patch("jobs.invalidate_cloudfront", return_value=None)
@patch("jobs.sync_to_s3", return_value=3)
@patch("jobs._run")
def test_deploy_page_no_options_defaults_to_all_enabled(mock_run, mock_s3, mock_cf, mock_rm):
    mock_run.return_value = _make_mock_run()
    from jobs import deploy_page

    deploy_page(url="https://example.com/", post_id=99)

    optimize_call = [c for c in mock_run.call_args_list if "optimize.py" in str(c)][0]
    optimize_cmd = optimize_call.args[0]
    assert "--no-bundle-css" not in optimize_cmd
    assert "--no-bundle-js" not in optimize_cmd
    assert "--no-compress-html" not in optimize_cmd
