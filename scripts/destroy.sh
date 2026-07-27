#!/usr/bin/env bash

set -euo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"

need docker
need jq
[[ $# -eq 2 && "$2" == "--yes" ]] || {
  echo "Usage: scripts/destroy.sh NAME --yes" >&2
  echo "This permanently removes the sandbox's Pi state and workspace volumes." >&2
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
PI_VOLUME="$(jq -r '.pi_volume' "$STATE_FILE")"
WORKSPACE_VOLUME="$(jq -r '.workspace_volume' "$STATE_FILE")"
docker volume rm "$PI_VOLUME" "$WORKSPACE_VOLUME"
rm -f -- "$STATE_FILE"
echo "Sandbox '$NAME' and both persistent volumes were permanently removed."
