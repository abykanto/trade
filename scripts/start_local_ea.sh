#!/usr/bin/env bash
# Local dev with MQL5 EA execution (no Docker, no mt5linux).
set -euo pipefail

exec "$(dirname "$0")/start_local.sh" --ea
