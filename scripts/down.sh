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
LITELLM_KEY="$(jq -r '.litellm_virtual_key // empty' "$STATE_FILE")"
REVOCATION_PENDING=false
if [[ -n "$LITELLM_KEY" ]]; then
  if ! block_litellm_key "$LITELLM_KEY"; then
    REVOCATION_PENDING=true
    echo "LiteLLM key revocation failed; sandbox will be stopped and cleanup can be retried." >&2
  fi
fi
if [[ -n "$SANDBOX_ID" ]]; then
  api DELETE "/v1/sandboxes/${SANDBOX_ID}" >/dev/null || true
fi
UPDATED="$(mktemp "${RUNTIME_DIR}/${NAME}.XXXXXX")"
if [[ "$REVOCATION_PENDING" == true ]]; then
  jq '.sandbox_id = null | .bridge_url = null | del(.bridge_proxy_token) | .litellm_revocation_pending = true' \
    "$STATE_FILE" >"$UPDATED"
else
  jq '.sandbox_id = null | .bridge_url = null | del(.bridge_proxy_token, .litellm_virtual_key, .litellm_revocation_pending)' \
    "$STATE_FILE" >"$UPDATED"
fi
chmod 600 "$UPDATED"
mv "$UPDATED" "$STATE_FILE"
if [[ "$REVOCATION_PENDING" == true ]]; then
  exit 1
fi
echo "Sandbox '$NAME' stopped. Pi state and workspace volumes were retained."
