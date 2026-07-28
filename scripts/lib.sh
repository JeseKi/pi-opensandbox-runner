#!/usr/bin/env bash

set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RUNTIME_DIR="${PROJECT_DIR}/.runtime"
SERVER_URL="${OPENSANDBOX_SERVER_URL:-http://127.0.0.1:8080}"
BRIDGE_IMAGE="${PI_RUNNER_IMAGE:-pi-opensandbox-runner:local}"
LITELLM_ENV_FILE="${PROJECT_DIR}/.litellm.env"

need() {
  command -v "$1" >/dev/null 2>&1 || {
    echo "Missing required command: $1" >&2
    exit 1
  }
}

validate_name() {
  local name="$1"
  if [[ ! "$name" =~ ^[a-z0-9]([-a-z0-9]*[a-z0-9])?$ ]] || ((${#name} > 40)); then
    echo "NAME must be a lowercase DNS label of at most 40 characters." >&2
    exit 2
  fi
}

state_file() {
  printf '%s/%s.json\n' "$RUNTIME_DIR" "$1"
}

server_key() {
  jq -r '.server_api_key' "${RUNTIME_DIR}/server.json"
}

api() {
  local method="$1"
  local path="$2"
  shift 2
  curl --fail-with-body --silent --show-error \
    --request "$method" \
    --header "OPEN-SANDBOX-API-KEY: $(server_key)" \
    "$@" \
    "${SERVER_URL}${path}"
}

ensure_server_config() {
  mkdir -p "$RUNTIME_DIR"
  chmod 700 "$RUNTIME_DIR"
  if [[ ! -f "${RUNTIME_DIR}/server.json" ]]; then
    local key
    key="$(openssl rand -hex 32)"
    jq -n --arg key "$key" '{server_api_key: $key}' \
      >"${RUNTIME_DIR}/server.json"
    chmod 600 "${RUNTIME_DIR}/server.json"
  fi
  local key
  key="$(server_key)"
  {
    echo '[server]'
    echo 'host = "0.0.0.0"'
    echo 'port = 8080'
    echo 'max_sandbox_timeout_seconds = 86400'
    printf 'api_key = "%s"\n' "$key"
    echo
    echo '[log]'
    echo 'level = "INFO"'
    echo
    echo '[runtime]'
    echo 'type = "docker"'
    echo 'execd_image = "opensandbox/execd:v1.0.21"'
    echo
    echo '[storage]'
    echo 'allowed_host_paths = []'
    echo 'volume_default_size = "1Gi"'
    echo
    echo '[store]'
    echo 'type = "sqlite"'
    echo 'path = "/root/.opensandbox/opensandbox.db"'
    echo
    echo '[docker]'
    # Sandboxes share this private Docker network with the OpenSandbox server.
    # The server proxy can therefore reach the bridge directly by its
    # container IP; port 8765 is never published on the host.
    echo 'network_mode = "pi-runner-internal"'
    echo 'port_range_min = 40000'
    echo 'port_range_max = 60000'
    echo 'drop_capabilities = ["AUDIT_WRITE", "MKNOD", "NET_ADMIN", "NET_RAW", "SYS_ADMIN", "SYS_MODULE", "SYS_PTRACE", "SYS_TIME", "SYS_TTY_CONFIG"]'
    echo 'no_new_privileges = true'
    echo 'pids_limit = 4096'
    echo
    echo '[ingress]'
    echo 'mode = "direct"'
    echo
    echo '[egress]'
    echo 'image = "opensandbox/egress:v1.1.4"'
    echo 'mode = "dns"'
  } >"${RUNTIME_DIR}/opensandbox.toml"
  chmod 600 "${RUNTIME_DIR}/opensandbox.toml"
}

wait_for_server() {
  local attempt
  for attempt in {1..60}; do
    if curl --fail --silent "${SERVER_URL}/health" >/dev/null; then
      return
    fi
    sleep 1
  done
  echo "OpenSandbox did not become healthy at ${SERVER_URL}." >&2
  exit 1
}

endpoint_url() {
  local endpoint="$1"
  if [[ "$endpoint" =~ ^https?:// ]]; then
    printf '%s\n' "$endpoint"
  else
    printf 'http://%s\n' "$endpoint"
  fi
}

require_litellm_env() {
  [[ -f "$LITELLM_ENV_FILE" ]] || {
    echo "Missing $LITELLM_ENV_FILE. Copy .litellm.env.example and configure it." >&2
    exit 2
  }
  local mode
  mode="$(stat -c '%a' "$LITELLM_ENV_FILE")"
  if (( (8#$mode & 077) != 0 )); then
    echo "$LITELLM_ENV_FILE must not be readable by group or other users (chmod 600)." >&2
    exit 2
  fi
}

wait_for_litellm() {
  local attempt
  for attempt in {1..60}; do
    if docker compose --project-directory "$PROJECT_DIR" exec -T litellm \
      python -c 'import urllib.request; urllib.request.urlopen("http://127.0.0.1:4000/health/liveliness", timeout=2)' \
      >/dev/null 2>&1; then
      return
    fi
    sleep 1
  done
  echo "LiteLLM did not become healthy." >&2
  exit 1
}

litellm_admin() {
  local method="$1"
  local path="$2"
  local body="$3"
  docker compose --project-directory "$PROJECT_DIR" exec -T litellm \
    python -c '
import os, sys, urllib.error, urllib.request
method, body, path = sys.argv[1:]
request = urllib.request.Request(
    "http://127.0.0.1:4000" + path,
    data=body.encode(),
    method=method,
    headers={
        "Authorization": "Bearer " + os.environ["LITELLM_MASTER_KEY"],
        "Content-Type": "application/json",
    },
)
try:
    with urllib.request.urlopen(request, timeout=15) as response:
        print(response.read().decode(), end="")
except urllib.error.HTTPError as error:
    detail = error.read().decode(errors="replace")
    raise SystemExit(f"LiteLLM admin request failed ({error.code}): {detail}")
' "$method" "$body" "$path"
}

block_litellm_key() {
  local key="$1"
  [[ -n "$key" ]] || return 0
  litellm_admin POST /key/block "$(jq -n --arg key "$key" '{key: $key}')" >/dev/null
}
