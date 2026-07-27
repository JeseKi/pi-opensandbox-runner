#!/usr/bin/env bash

set -euo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"

need curl
need jq
[[ $# -eq 1 ]] || {
  echo "Usage: scripts/status.sh NAME" >&2
  exit 2
}
NAME="$1"
validate_name "$NAME"
STATE_FILE="$(state_file "$NAME")"
[[ -f "$STATE_FILE" ]] || {
  echo "No state exists for '$NAME'." >&2
  exit 1
}

SANDBOX_ID="$(jq -r '.sandbox_id // empty' "$STATE_FILE")"
if [[ -z "$SANDBOX_ID" ]]; then
  echo "Sandbox '$NAME' is stopped; its named volumes are retained."
  exit 0
fi
api GET "/v1/sandboxes/${SANDBOX_ID}" | jq .
echo "Bridge URL: $(jq -r '.bridge_url' "$STATE_FILE")"
