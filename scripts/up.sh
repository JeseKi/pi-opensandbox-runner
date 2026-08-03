#!/usr/bin/env bash

set -euo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"

usage() {
  cat <<'EOF'
Usage: scripts/up.sh NAME [options]

Options:
  --model NAME             LiteLLM model alias (default: coding-default)
  --mcp-env-file PATH      Optional MCP_* credentials injected into sandbox
  --policy SLUG            Runner catalog policy (default: consumer-default)
  --mirror-mode MODE       auto, cn, or global (default: auto)
  --show-token             Print the bridge bearer token (unsafe in CI logs)
  --cpu VALUE              CPU limit (default: 2)
  --memory VALUE           Memory limit (default: 4Gi)
EOF
}

need docker
need curl
need jq
need openssl

[[ $# -ge 1 ]] || {
  usage >&2
  exit 2
}
NAME="$1"
shift
validate_name "$NAME"

MODEL="coding-default"
MCP_ENV_FILE=""
POLICY="consumer-default"
MIRROR_MODE_VALUE="${MIRROR_MODE:-auto}"
SHOW_TOKEN=false
CPU="2"
MEMORY="4Gi"
while (($#)); do
  case "$1" in
    --model) MODEL="$2"; shift 2 ;;
    --mcp-env-file) MCP_ENV_FILE="$2"; shift 2 ;;
    --policy) POLICY="$2"; shift 2 ;;
    --mirror-mode) MIRROR_MODE_VALUE="$2"; shift 2 ;;
    --show-token) SHOW_TOKEN=true; shift ;;
    --cpu) CPU="$2"; shift 2 ;;
    --memory) MEMORY="$2"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown option: $1" >&2; usage >&2; exit 2 ;;
  esac
done

case "$MIRROR_MODE_VALUE" in
  auto|cn|global) ;;
  *) echo "--mirror-mode must be auto, cn, or global." >&2; exit 2 ;;
esac

require_litellm_env
if [[ -n "$MCP_ENV_FILE" && ! -f "$MCP_ENV_FILE" ]]; then
  echo "MCP environment file not found: $MCP_ENV_FILE" >&2
  exit 2
fi
if ! jq -e --arg model "$MODEL" --arg policy "$POLICY" \
  '.policies[] | select(.slug == $policy and (.model_slugs | index($model)))' \
  "$PROJECT_DIR/config/runner-catalog.json" >/dev/null; then
  echo "Unknown LiteLLM model alias: $MODEL" >&2
  exit 2
fi

ensure_server_config
if ! docker image inspect pi-opensandbox-runner-opensandbox:local >/dev/null 2>&1; then
  docker compose --project-directory "$PROJECT_DIR" build opensandbox
fi
if ! docker image inspect pi-runner-egress:local >/dev/null 2>&1; then
  docker build --file "$PROJECT_DIR/Dockerfile.egress" --tag pi-runner-egress:local "$PROJECT_DIR"
fi
docker compose --project-directory "$PROJECT_DIR" up --detach --no-build
wait_for_server
wait_for_litellm

STATE_FILE="$(state_file "$NAME")"
if [[ -f "$STATE_FILE" ]]; then
  EXISTING_ID="$(jq -r '.sandbox_id // empty' "$STATE_FILE")"
  if [[ -n "$EXISTING_ID" ]] \
    && api GET "/v1/sandboxes/${EXISTING_ID}" >/dev/null 2>&1; then
    if [[ -z "$(jq -r '.bridge_proxy_token // empty' "$STATE_FILE")" ]]; then
      echo "Sandbox '$NAME' uses the legacy bridge-token model." >&2
      echo "Run scripts/down.sh $NAME, then rerun this command to migrate without deleting volumes." >&2
      exit 1
    fi
    if ! load_egress_policy_from_state "$STATE_FILE"; then
      echo "Sandbox '$NAME' has unrestricted legacy egress; run scripts/down.sh $NAME, then recreate it with a runner catalog policy." >&2
      exit 1
    fi
    if [[ -z "$RESOLVED_EGRESS_POLICY_SLUG" ]]; then
      echo "Sandbox '$NAME' predates runner-catalog egress state; run scripts/egress-policy.sh $NAME apply --policy $POLICY." >&2
      exit 1
    fi
    if [[ "$RESOLVED_EGRESS_POLICY_SLUG" != "$POLICY" ]]; then
      echo "Sandbox '$NAME' uses policy '$RESOLVED_EGRESS_POLICY_SLUG'; use scripts/egress-policy.sh $NAME apply --policy $POLICY to change it." >&2
      exit 1
    fi
    echo "Sandbox '$NAME' is already running."
    echo "Bridge URL: $(jq -r '.bridge_url' "$STATE_FILE")"
    echo "Credentials: $STATE_FILE (mode 0600)"
    if [[ "$SHOW_TOKEN" == true ]]; then
      echo "Token: $(jq -r '.bridge_proxy_token' "$STATE_FILE")"
    fi
    exit 0
  fi
  STALE_KEY="$(jq -r '.litellm_virtual_key // empty' "$STATE_FILE")"
  if [[ -n "$STALE_KEY" ]]; then
    block_litellm_key "$STALE_KEY" || {
      echo "Could not revoke stale LiteLLM key for '$NAME'; retry down before creating a new sandbox." >&2
      exit 1
    }
    UPDATED="$(mktemp "${RUNTIME_DIR}/${NAME}.XXXXXX")"
    jq 'del(.litellm_virtual_key, .litellm_revocation_pending)' "$STATE_FILE" >"$UPDATED"
    chmod 600 "$UPDATED"
    mv "$UPDATED" "$STATE_FILE"
  fi
fi

resolve_egress_policy "$POLICY"

# Rebuilding a shared Docker tag discards the image metadata that OpenSandbox
# needs to inspect already-running sandboxes. Build only for first use; image
# updates are an explicit deployment operation, after affected sandboxes stop.
if ! docker image inspect "$BRIDGE_IMAGE" >/dev/null 2>&1; then
  docker build \
    --build-arg "MIRROR_MODE=${MIRROR_MODE_VALUE}" \
    --tag "$BRIDGE_IMAGE" \
    "$PROJECT_DIR"
fi

KEY_ALIAS="pi-runner-${NAME}-$(openssl rand -hex 4)"
KEY_REQUEST="$(jq -n --arg model "$MODEL" --arg key_alias "$KEY_ALIAS" --arg name "$NAME" \
  '{models: [$model], key_alias: $key_alias,
    metadata: {sandbox_name: $name}}')"
KEY_RESPONSE="$(litellm_admin POST /key/generate "$KEY_REQUEST")"
LITELLM_KEY="$(jq -er '.key // .token' <<<"$KEY_RESPONSE")"

MCP_ENV='{}'
if [[ -n "$MCP_ENV_FILE" ]]; then
  while IFS= read -r line || [[ -n "$line" ]]; do
    [[ -z "$line" || "$line" =~ ^[[:space:]]*# ]] && continue
    [[ "$line" == *=* ]] || { echo "Invalid MCP environment line: $line" >&2; exit 2; }
    key="${line%%=*}"
    value="${line#*=}"
    [[ "$key" =~ ^MCP_([A-Za-z0-9_]+)$ ]] || {
      echo "Only MCP_* variables may be injected from --mcp-env-file." >&2
      exit 2
    }
    MCP_ENV="$(jq --arg key "$key" --arg value "$value" '. + {($key): $value}' <<<"$MCP_ENV")"
  done <"$MCP_ENV_FILE"
fi

BRIDGE_PROXY_TOKEN="$(openssl rand -hex 32)"
BRIDGE_PROXY_TOKEN_HASH="h$(printf '%s' "$BRIDGE_PROXY_TOKEN" \
  | openssl dgst -sha256 -binary | base64 | tr '+/' '-_' | tr -d '=\n')"
INTERNAL_ENV="$(jq -n \
  --arg model "$MODEL" \
  --arg litellm_key "$LITELLM_KEY" \
  '{
    LITELLM_VIRTUAL_KEY: $litellm_key,
    HOME: "/root",
    PI_CODING_AGENT_SESSION_DIR: "/root/.pi/agent/sessions",
    BRIDGE_STATE_ROOT: "/root/.pi/bridge",
    PI_WORKSPACE_ROOT: "/root/workspace"
  }
  + {PI_DEFAULT_MODEL: $model}')"
ENV_JSON="$(jq -n --argjson mcp "$MCP_ENV" --argjson internal "$INTERNAL_ENV" '$mcp + $internal')"

PAYLOAD="$(jq -n \
  --arg image "$BRIDGE_IMAGE" \
  --arg name "$NAME" \
  --arg bridge_proxy_token_hash "$BRIDGE_PROXY_TOKEN_HASH" \
  --arg cpu "$CPU" \
  --arg memory "$MEMORY" \
  --arg pi_volume "pi-runner-${NAME}-pi" \
  --arg workspace_volume "pi-runner-${NAME}-workspace" \
  --argjson env "$ENV_JSON" \
  --argjson network_policy "$RESOLVED_EGRESS_POLICY" \
  '{
    image: {uri: $image},
    entrypoint: ["/usr/local/bin/pi-runner-entrypoint"],
    timeout: null,
    resourceLimits: {cpu: $cpu, memory: $memory},
    env: $env,
    networkPolicy: $network_policy,
    metadata: {
      name: $name,
      component: "pi-opensandbox-runner",
      "pi-runner.bridge-proxy-token-sha256": $bridge_proxy_token_hash
    },
    volumes: [
      {
        name: "pi-state",
        pvc: {
          claimName: $pi_volume,
          createIfNotExists: true,
          deleteOnSandboxTermination: false
        },
        mountPath: "/root/.pi"
      },
      {
        name: "workspace",
        pvc: {
          claimName: $workspace_volume,
          createIfNotExists: true,
          deleteOnSandboxTermination: false
        },
        mountPath: "/root/workspace"
      }
    ]
  }')"

CREATE_RESPONSE="$(api POST /v1/sandboxes \
  --header "Content-Type: application/json" \
  --data "$PAYLOAD")"
SANDBOX_ID="$(jq -er '.id' <<<"$CREATE_RESPONSE")"
SANDBOX_RECORDED=false
cleanup_unrecorded_sandbox() {
  if [[ "$SANDBOX_RECORDED" == false && -n "${SANDBOX_ID:-}" ]]; then
    api DELETE "/v1/sandboxes/${SANDBOX_ID}" >/dev/null 2>&1 || true
  fi
  if [[ -n "${LITELLM_KEY:-}" ]]; then
    block_litellm_key "$LITELLM_KEY" || true
  fi
}
trap cleanup_unrecorded_sandbox ERR

for attempt in {1..120}; do
  SANDBOX_RESPONSE="$(api GET "/v1/sandboxes/${SANDBOX_ID}")"
  SANDBOX_STATE="$(jq -r '.status.state // .status // empty' <<<"$SANDBOX_RESPONSE")"
  [[ "$SANDBOX_STATE" == "Running" ]] && break
  if [[ "$SANDBOX_STATE" == "Failed" ]]; then
    jq . <<<"$SANDBOX_RESPONSE" >&2
    exit 1
  fi
  sleep 1
done
if [[ "${SANDBOX_STATE:-}" != "Running" ]]; then
  echo "Sandbox failed to reach Running state." >&2
  exit 1
fi

# Use the OpenSandbox server proxy rather than direct ingress.  The server
# itself is published only on 127.0.0.1 by compose.yaml, so this avoids Docker
# publishing a per-sandbox port on 0.0.0.0.
ENDPOINT_RESPONSE="$(api GET "/v1/sandboxes/${SANDBOX_ID}/endpoints/8765?use_server_proxy=true")"
RAW_ENDPOINT="$(jq -er '.endpoint' <<<"$ENDPOINT_RESPONSE")"
BRIDGE_URL="$(endpoint_url "$RAW_ENDPOINT")"

for attempt in {1..60}; do
  if curl --fail --silent "${BRIDGE_URL}/readyz" >/dev/null; then
    break
  fi
  sleep 1
done
if ! curl --fail --silent "${BRIDGE_URL}/readyz" >/dev/null; then
  echo "Bridge did not become ready at ${BRIDGE_URL}." >&2
  exit 1
fi

# The Runner image contains a bootstrap copy of the catalog so Bridge can
# start before this script has a reachable endpoint. Replace it now from the
# host catalog so direct-script sandboxes observe the same source of truth as
# Manager-provisioned sandboxes without requiring an image rebuild.
BRIDGE_MODEL_CONFIG="$(bridge_model_config "$POLICY")"
curl --fail-with-body --silent --show-error \
  --request PUT \
  --header "Authorization: Bearer ${BRIDGE_PROXY_TOKEN}" \
  --header 'Content-Type: application/json' \
  --data "$BRIDGE_MODEL_CONFIG" \
  "${BRIDGE_URL}/v1/models/config" >/dev/null

jq -n \
  --arg name "$NAME" \
  --arg sandbox_id "$SANDBOX_ID" \
  --arg bridge_url "$BRIDGE_URL" \
  --arg bridge_proxy_token "$BRIDGE_PROXY_TOKEN" \
  --arg litellm_key "$LITELLM_KEY" \
  --arg pi_volume "pi-runner-${NAME}-pi" \
  --arg workspace_volume "pi-runner-${NAME}-workspace" \
  --argjson egress_policy "$RESOLVED_EGRESS_STATE" \
  '{
    name: $name,
    sandbox_id: $sandbox_id,
    bridge_url: $bridge_url,
    bridge_proxy_token: $bridge_proxy_token,
    litellm_virtual_key: $litellm_key,
    litellm_revocation_pending: false,
    egress_policy: $egress_policy,
    pi_volume: $pi_volume,
    workspace_volume: $workspace_volume
  }' >"$STATE_FILE"
chmod 600 "$STATE_FILE"
SANDBOX_RECORDED=true
trap - ERR

echo "Sandbox '$NAME' is ready."
echo "Bridge URL: $BRIDGE_URL"
echo "Credentials: $STATE_FILE (mode 0600)"
if [[ "$SHOW_TOKEN" == true ]]; then
  echo "Token: $BRIDGE_PROXY_TOKEN"
fi
