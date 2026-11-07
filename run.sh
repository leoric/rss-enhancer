#!/usr/bin/env bash
# Run RssEnhancer from anywhere. Settings come from config.toml in this folder.
#   ./run.sh                    run forever (web server + every configured source, in parallel)
#   ./run.sh --once             refresh every source once and exit (no web server)
#   ./run.sh --only hydrator    run just one source (hydrator | subscriptions)
set -euo pipefail
cd "$(dirname "$0")"
exec "${PYTHON:-python3}" -m rssenhancer "$@"
