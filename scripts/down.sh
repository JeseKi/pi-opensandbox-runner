#!/usr/bin/env bash

set -euo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"

need curl
need jq
[[ $# -eq 1 ]] || {
  echo "Usage: scripts/down.sh NAME" >&2
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
if [[ -n "$SANDBOX_ID" ]]; then
  api DELETE "/v1/sandboxes/${SANDBOX_ID}" >/dev/null || true
fi
UPDATED="$(mktemp "${RUNTIME_DIR}/${NAME}.XXXXXX")"
jq '.sandbox_id = null | .bridge_url = null' "$STATE_FILE" >"$UPDATED"
chmod 600 "$UPDATED"
mv "$UPDATED" "$STATE_FILE"
echo "Sandbox '$NAME' stopped. Pi state and workspace volumes were retained."
