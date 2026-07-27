#!/usr/bin/env bash

set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RUNTIME_DIR="${PROJECT_DIR}/.runtime"
SERVER_URL="${OPENSANDBOX_SERVER_URL:-http://127.0.0.1:8080}"
BRIDGE_IMAGE="${PI_RUNNER_IMAGE:-pi-opensandbox-runner:local}"

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
    echo 'network_mode = "bridge"'
    echo 'host_ip = "127.0.0.1"'
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
