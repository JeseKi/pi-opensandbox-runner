#!/usr/bin/env bash

set -euo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"

usage() {
  cat <<'EOF'
Usage:
  scripts/egress-policy.sh NAME show
  scripts/egress-policy.sh NAME apply [--policy SLUG]

Only a host administrator can run this command. apply replaces the complete
allowlist; it always retains the internal LiteLLM destination.
EOF
}

need curl
need jq
[[ $# -ge 2 ]] || { usage >&2; exit 2; }
NAME="$1"
ACTION="$2"
shift 2
validate_name "$NAME"
STATE_FILE="$(state_file "$NAME")"
[[ -f "$STATE_FILE" ]] || { echo "No state exists for '$NAME'." >&2; exit 1; }
SANDBOX_ID="$(jq -r '.sandbox_id // empty' "$STATE_FILE")"
[[ -n "$SANDBOX_ID" ]] || { echo "Sandbox '$NAME' is not running." >&2; exit 1; }
api GET "/v1/sandboxes/${SANDBOX_ID}" >/dev/null

EGRESS_ENDPOINT_RESPONSE="$(api GET "/v1/sandboxes/${SANDBOX_ID}/endpoints/18080")"
EGRESS_URL="$(endpoint_url "$(jq -er '.endpoint' <<<"$EGRESS_ENDPOINT_RESPONSE")")"
declare -a EGRESS_HEADERS=()
while IFS=$'\t' read -r header_name header_value; do
  [[ -n "$header_name" ]] || continue
  EGRESS_HEADERS+=(--header "${header_name}: ${header_value}")
done < <(jq -r '.headers // {} | to_entries[] | [.key, (.value | tostring)] | @tsv' <<<"$EGRESS_ENDPOINT_RESPONSE")

egress_request() {
  local method="$1"
  local path="$2"
  local body="${3:-}"
  local -a args=(--fail-with-body --silent --show-error --request "$method" "${EGRESS_HEADERS[@]}")
  if [[ -n "$body" ]]; then
    args+=(--header 'Content-Type: application/json' --data "$body")
  fi
  curl "${args[@]}" "${EGRESS_URL}${path}"
}

case "$ACTION" in
  show)
    [[ $# -eq 0 ]] || { usage >&2; exit 2; }
    egress_request GET /policy | jq
    ;;
  apply)
    POLICY="consumer-default"
    while (($#)); do
      case "$1" in
        --policy) POLICY="$2"; shift 2 ;;
        *) echo "Unknown option: $1" >&2; usage >&2; exit 2 ;;
      esac
    done
    resolve_egress_policy "$POLICY"
    egress_request POST /policy "$RESOLVED_EGRESS_POLICY" | jq
    UPDATED="$(mktemp "${RUNTIME_DIR}/${NAME}.XXXXXX")"
    jq --argjson egress_policy "$RESOLVED_EGRESS_STATE" '.egress_policy = $egress_policy' \
      "$STATE_FILE" >"$UPDATED"
    chmod 600 "$UPDATED"
    mv "$UPDATED" "$STATE_FILE"
    ;;
  *)
    echo "Unknown action: $ACTION" >&2
    usage >&2
    exit 2
    ;;
esac
