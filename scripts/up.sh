#!/usr/bin/env bash

set -euo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"

usage() {
  cat <<'EOF'
Usage: scripts/up.sh NAME [options]

Options:
  --env-file PATH          Add provider/API environment variables
  --provider NAME          Default Pi provider
  --model NAME             Default Pi model
  --network-policy PATH    OpenSandbox networkPolicy JSON
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

ENV_FILE=""
PROVIDER=""
MODEL=""
NETWORK_POLICY=""
MIRROR_MODE_VALUE="${MIRROR_MODE:-auto}"
SHOW_TOKEN=false
CPU="2"
MEMORY="4Gi"
while (($#)); do
  case "$1" in
    --env-file) ENV_FILE="$2"; shift 2 ;;
    --provider) PROVIDER="$2"; shift 2 ;;
    --model) MODEL="$2"; shift 2 ;;
    --network-policy) NETWORK_POLICY="$2"; shift 2 ;;
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

if [[ -n "$ENV_FILE" && ! -f "$ENV_FILE" ]]; then
  echo "Environment file not found: $ENV_FILE" >&2
  exit 2
fi
if [[ -n "$NETWORK_POLICY" && ! -f "$NETWORK_POLICY" ]]; then
  echo "Network policy not found: $NETWORK_POLICY" >&2
  exit 2
fi
if [[ -n "$PROVIDER" || -n "$MODEL" ]] && [[ -z "$PROVIDER" || -z "$MODEL" ]]; then
  echo "--provider and --model must be supplied together." >&2
  exit 2
fi

ensure_server_config
docker compose --project-directory "$PROJECT_DIR" up --detach --build
wait_for_server
docker build \
  --build-arg "MIRROR_MODE=${MIRROR_MODE_VALUE}" \
  --tag "$BRIDGE_IMAGE" \
  "$PROJECT_DIR"

STATE_FILE="$(state_file "$NAME")"
if [[ -f "$STATE_FILE" ]]; then
  EXISTING_ID="$(jq -r '.sandbox_id // empty' "$STATE_FILE")"
  if [[ -n "$EXISTING_ID" ]] \
    && api GET "/v1/sandboxes/${EXISTING_ID}" >/dev/null 2>&1; then
    echo "Sandbox '$NAME' is already running."
    echo "Bridge URL: $(jq -r '.bridge_url' "$STATE_FILE")"
    echo "Credentials: $STATE_FILE (mode 0600)"
    if [[ "$SHOW_TOKEN" == true ]]; then
      echo "Token: $(jq -r '.bridge_token' "$STATE_FILE")"
    fi
    exit 0
  fi
fi

USER_ENV='{}'
if [[ -n "$ENV_FILE" ]]; then
  while IFS= read -r line || [[ -n "$line" ]]; do
    [[ -z "$line" || "$line" =~ ^[[:space:]]*# ]] && continue
    if [[ "$line" != *=* ]]; then
      echo "Invalid environment line: $line" >&2
      exit 2
    fi
    key="${line%%=*}"
    value="${line#*=}"
    if [[ ! "$key" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]]; then
      echo "Invalid environment key: $key" >&2
      exit 2
    fi
    USER_ENV="$(jq --arg key "$key" --arg value "$value" \
      '. + {($key): $value}' <<<"$USER_ENV")"
  done <"$ENV_FILE"
fi

BRIDGE_TOKEN="$(openssl rand -hex 32)"
INTERNAL_ENV="$(jq -n \
  --arg token "$BRIDGE_TOKEN" \
  --arg provider "$PROVIDER" \
  --arg model "$MODEL" \
  '{
    BRIDGE_API_TOKEN: $token,
    HOME: "/root",
    PI_CODING_AGENT_SESSION_DIR: "/root/.pi/agent/sessions",
    BRIDGE_STATE_ROOT: "/root/.pi/bridge",
    PI_WORKSPACE_ROOT: "/root/workspace"
  }
  + (if $provider == "" then {} else {
      PI_DEFAULT_PROVIDER: $provider,
      PI_DEFAULT_MODEL: $model
    } end)')"
ENV_JSON="$(jq -n --argjson user "$USER_ENV" --argjson internal "$INTERNAL_ENV" \
  '$user + $internal')"

NETWORK_JSON='null'
if [[ -n "$NETWORK_POLICY" ]]; then
  NETWORK_JSON="$(jq -c . "$NETWORK_POLICY")"
fi

PAYLOAD="$(jq -n \
  --arg image "$BRIDGE_IMAGE" \
  --arg name "$NAME" \
  --arg cpu "$CPU" \
  --arg memory "$MEMORY" \
  --arg pi_volume "pi-runner-${NAME}-pi" \
  --arg workspace_volume "pi-runner-${NAME}-workspace" \
  --argjson env "$ENV_JSON" \
  --argjson network "$NETWORK_JSON" \
  '{
    image: {uri: $image},
    entrypoint: ["python", "-m", "pi_opensandbox_runner"],
    timeout: null,
    resourceLimits: {cpu: $cpu, memory: $memory},
    env: $env,
    metadata: {name: $name, component: "pi-opensandbox-runner"},
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
  } + (if $network == null then {} else {networkPolicy: $network} end)')"

CREATE_RESPONSE="$(api POST /v1/sandboxes \
  --header "Content-Type: application/json" \
  --data "$PAYLOAD")"
SANDBOX_ID="$(jq -er '.id' <<<"$CREATE_RESPONSE")"
SANDBOX_RECORDED=false
cleanup_unrecorded_sandbox() {
  if [[ "$SANDBOX_RECORDED" == false && -n "${SANDBOX_ID:-}" ]]; then
    api DELETE "/v1/sandboxes/${SANDBOX_ID}" >/dev/null 2>&1 || true
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

jq -n \
  --arg name "$NAME" \
  --arg sandbox_id "$SANDBOX_ID" \
  --arg bridge_url "$BRIDGE_URL" \
  --arg bridge_token "$BRIDGE_TOKEN" \
  --arg pi_volume "pi-runner-${NAME}-pi" \
  --arg workspace_volume "pi-runner-${NAME}-workspace" \
  '{
    name: $name,
    sandbox_id: $sandbox_id,
    bridge_url: $bridge_url,
    bridge_token: $bridge_token,
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
  echo "Token: $BRIDGE_TOKEN"
fi
