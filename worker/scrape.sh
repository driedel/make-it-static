#!/usr/bin/env bash
# scrape.sh — downloads a complete website (or section) for static deployment
# usage: ./scrape.sh <URL> <OUTPUT_DIR> [EXTRA_CDN]
#   EXTRA_CDN: comma-separated extra domains (e.g. "cdn.example.com,assets.other.com")

set -euo pipefail
export LANG=C.UTF-8 LC_ALL=C.UTF-8

URL="${1:?URL required}"
OUTPUT_DIR="${2:?output directory required}"
EXTRA_CDN="${3:-}"

# Extract the host from the URL using python (already in the container) — handles http/https
HOST=$(python3 -c "from urllib.parse import urlparse; import sys; print(urlparse(sys.argv[1]).hostname)" "$URL")

if [ -z "$HOST" ]; then
  echo "Could not extract host from URL: $URL" >&2
  exit 1
fi

mkdir -p "$OUTPUT_DIR"
cd "$OUTPUT_DIR"

# External asset domains downloaded alongside the page (CDNs for scripts/icons).
# Google Fonts is intentionally excluded: the API returns different CSS per User-Agent
# (each browser gets the right format), and wget would only capture one format. The
# <link href="https://fonts.googleapis.com/css?..."> tags are left as-is in the HTML
# so fonts keep loading from the Google CDN at runtime.
BASE_DOMAINS="ajax.googleapis.com,cdnjs.cloudflare.com,s.w.org"
EXTRA_DOMAINS="${BASE_DOMAINS}${EXTRA_CDN:+,$EXTRA_CDN}"
EXCLUDE_DOMAINS="fonts.googleapis.com,fonts.gstatic.com"

echo "[scrape] downloading $URL to $OUTPUT_DIR (host=$HOST)"
if [ -n "$EXTRA_CDN" ]; then
  echo "[scrape] extra CDNs: $EXTRA_CDN"
fi

# Key flags:
# --recursive --level=0   : unlimited depth — follows all internal links on the domain
# --page-requisites       : CSS, JS, images, fonts for each downloaded page
# --span-hosts --domains  : downloads assets from external CDNs, but only follows HTML on the main host
# --no-host-directories   : no folder named after the host — paths mirror the URL directly
# --restrict-file-names   : replaces ? : * in filenames (required for S3)
# --reject-regex          : blocks CMS admin, feeds, pagination and search (dynamic/irrelevant)
# Note: --convert-links is intentionally omitted. It processes <script> content and mangles
# JS template literals like href="${expr}" into href="/page/path/${expr}". postprocess.py
# handles all link rewriting instead (origin-host stripping, absolutizing, rename cleanup).
wget \
  --recursive \
  --level=0 \
  --page-requisites \
  --adjust-extension \
  --span-hosts \
  --domains="$HOST,$EXTRA_DOMAINS" \
  --exclude-domains="$EXCLUDE_DOMAINS" \
  --no-host-directories \
  --restrict-file-names=windows,nocontrol \
  --local-encoding=UTF-8 \
  --user-agent="Mozilla/5.0 (compatible; MakeItStaticBot/1.0)" \
  --timeout=30 \
  --tries=3 \
  --wait=0.5 \
  --random-wait \
  --execute robots=off \
  --reject-regex '(/wp-admin/|/wp-json/|xmlrpc\.php|/feed/?(\?|$)|/page/[0-9]+/?(\?|$)|\?s=|\?p=|/attachment/)' \
  "$URL" || {
    # wget returns non-zero even for trivial cases (e.g. a single 404 resource).
    # Only fail if no HTML was downloaded at all.
    if [ -z "$(find . -name '*.html' -print -quit)" ]; then
      echo "[scrape] FAILED: no HTML downloaded" >&2
      exit 1
    fi
    echo "[scrape] warning: wget exited with errors but HTML exists — continuing."
}

echo "[scrape] OK"
echo "[scrape] files (first 50):"
find . -type f | head -n 50 || true
