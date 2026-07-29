#!/usr/bin/env bash

set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RUNTIME_DIR="${PROJECT_DIR}/.runtime"
SERVER_URL="${OPENSANDBOX_SERVER_URL:-http://127.0.0.1:8080}"
BRIDGE_IMAGE="${PI_RUNNER_IMAGE:-pi-opensandbox-runner:local}"
LITELLM_ENV_FILE="${PROJECT_DIR}/.litellm.env"
EGRESS_PROFILES_FILE="${PROJECT_DIR}/config/egress-profiles.json"

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

_trim() {
  local value="$1"
  value="${value#"${value%%[![:space:]]*}"}"
  value="${value%"${value##*[![:space:]]}"}"
  printf '%s\n' "$value"
}

validate_egress_domain() {
  local domain="$1"
  local bare="$domain"
  [[ -n "$domain" && ${#domain} -le 253 ]] || return 1
  [[ "$domain" != *://* && "$domain" != */* && "$domain" != *:* ]] || return 1
  [[ ! "$domain" =~ ^[0-9.]+$ ]] || return 1
  if [[ "$domain" == \*.* ]]; then
    bare="${domain#*.}"
  elif [[ "$domain" == *'*'* ]]; then
    return 1
  fi
  [[ "$bare" == *.* ]] || return 1
  [[ "$bare" != *..* && "$bare" != .* && "$bare" != *. ]] || return 1
  [[ "$bare" =~ ^[A-Za-z0-9]([A-Za-z0-9-]*[A-Za-z0-9])?(\.[A-Za-z0-9]([A-Za-z0-9-]*[A-Za-z0-9])?)+$ ]]
}

_domains_to_json() {
  if (($# == 0)); then
    jq -cn '[]'
    return
  fi
  printf '%s\n' "$@" | LC_ALL=C sort -u | jq -Rsc 'split("\n") | map(select(length > 0))'
}

resolve_egress_policy() {
  local allowlist_file="$1"
  shift
  local -a profiles=("$@")
  # Both services stay on the private Docker network. LiteLLM is the model
  # endpoint; OpenSandbox is the only permitted inbound bridge proxy.
  local -a domains=("litellm" "opensandbox")
  local -a custom_domains=()
  local profile domain raw

  [[ -f "$EGRESS_PROFILES_FILE" ]] || {
    echo "Missing egress profile config: $EGRESS_PROFILES_FILE" >&2
    return 1
  }
  jq -e '.profiles | type == "object"' "$EGRESS_PROFILES_FILE" >/dev/null || {
    echo "Invalid egress profile config: $EGRESS_PROFILES_FILE" >&2
    return 1
  }
  if ((${#profiles[@]} == 0)) && [[ -z "$allowlist_file" ]]; then
    echo "Choose at least one --egress-profile or provide --egress-allowlist." >&2
    return 1
  fi
  for profile in "${profiles[@]}"; do
    jq -e --arg profile "$profile" '.profiles[$profile] | arrays' "$EGRESS_PROFILES_FILE" >/dev/null || {
      echo "Unknown egress profile: $profile" >&2
      return 1
    }
    while IFS= read -r domain; do
      validate_egress_domain "$domain" || {
        echo "Invalid domain '$domain' in egress profile '$profile'." >&2
        return 1
      }
      domains+=("$domain")
    done < <(jq -r --arg profile "$profile" '.profiles[$profile][]' "$EGRESS_PROFILES_FILE")
  done
  if [[ -n "$allowlist_file" ]]; then
    [[ -f "$allowlist_file" ]] || {
      echo "Egress allowlist file not found: $allowlist_file" >&2
      return 1
    }
    while IFS= read -r raw || [[ -n "$raw" ]]; do
      domain="$(_trim "$raw")"
      [[ -z "$domain" || "$domain" == \#* ]] && continue
      validate_egress_domain "$domain" || {
        echo "Invalid domain '$domain' in egress allowlist: $allowlist_file" >&2
        return 1
      }
      custom_domains+=("$domain")
      domains+=("$domain")
    done <"$allowlist_file"
  fi

  RESOLVED_EGRESS_POLICY="$(jq -cn --argjson domains "$(_domains_to_json "${domains[@]}")" \
    '{defaultAction: "deny", egress: [$domains[] | {action: "allow", target: .}]}')"
  RESOLVED_EGRESS_PROFILES="$(_domains_to_json "${profiles[@]}")"
  RESOLVED_EGRESS_CUSTOM_DOMAINS="$(_domains_to_json "${custom_domains[@]}")"
  RESOLVED_EGRESS_STATE="$(jq -cn \
    --argjson profiles "$RESOLVED_EGRESS_PROFILES" \
    --argjson custom_domains "$RESOLVED_EGRESS_CUSTOM_DOMAINS" \
    --argjson policy "$RESOLVED_EGRESS_POLICY" \
    '{version: 1, profiles: $profiles, custom_domains: $custom_domains, policy: $policy}')"
}

load_egress_policy_from_state() {
  local file="$1"
  RESOLVED_EGRESS_STATE="$(jq -ce '
    .egress_policy
    | select(.version == 1)
    | select(.policy.defaultAction == "deny" and (.policy.egress | type == "array" and length > 0))
  ' "$file")" || return 1
  RESOLVED_EGRESS_POLICY="$(jq -c '.policy' <<<"$RESOLVED_EGRESS_STATE")"
  RESOLVED_EGRESS_PROFILES="$(jq -c '.profiles // []' <<<"$RESOLVED_EGRESS_STATE")"
  RESOLVED_EGRESS_CUSTOM_DOMAINS="$(jq -c '.custom_domains // []' <<<"$RESOLVED_EGRESS_STATE")"
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
    # Egress policy management is a host-admin operation. Its sidecar API is
    # bound to Docker's host loopback, so advertise that address rather than
    # the OpenSandbox server container's private IP.
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
    # v1.1.5 fixes Docker DNS interception when the resolver uses loopback.
    echo 'image = "pi-runner-egress:local"'
    echo 'mode = "dns+nft"'
    echo 'disable_ipv6 = true'
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
