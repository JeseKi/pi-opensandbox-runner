#!/usr/bin/env bash

# Prepare the local configuration needed by Docker Compose without exposing
# generated secrets in the terminal or shell history.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/lib.sh"

need jq
need openssl

is_unset() {
  local value="$1"
  [[ -z "$value" || "$value" == "replace-with-a-random-long-secret" ]]
}

env_value() {
  local file="$1"
  local name="$2"
  local line
  line="$(grep -E "^${name}=" "$file" || true)"
  printf '%s' "${line#*=}"
}

set_env_value() {
  local file="$1"
  local name="$2"
  local value="$3"
  local temporary
  temporary="$(mktemp "${file}.XXXXXX")"
  awk -v name="$name" -v value="$value" '
    BEGIN { found = 0 }
    $0 ~ "^" name "=" { print name "=" value; found = 1; next }
    { print }
    END { if (!found) print name "=" value }
  ' "$file" >"$temporary"
  chmod 600 "$temporary"
  mv "$temporary" "$file"
}

random_hex() {
  openssl rand -hex 32
}

random_fernet_key() {
  openssl rand -base64 32 | tr '+/' '-_'
}

for env_file in .litellm.env .manager.env; do
  if [[ ! -e "${PROJECT_DIR}/${env_file}" ]]; then
    cp "${PROJECT_DIR}/${env_file}.example" "${PROJECT_DIR}/${env_file}"
  fi
  chmod 600 "${PROJECT_DIR}/${env_file}"
done

ensure_server_config

LITELLM_ENV_FILE="${PROJECT_DIR}/.litellm.env"
MANAGER_ENV_FILE="${PROJECT_DIR}/.manager.env"
litellm_master_key="$(env_value "$LITELLM_ENV_FILE" LITELLM_MASTER_KEY)"
manager_master_key="$(env_value "$MANAGER_ENV_FILE" LITELLM_MASTER_KEY)"

if is_unset "$litellm_master_key"; then
  if ! is_unset "$manager_master_key"; then
    litellm_master_key="$manager_master_key"
  else
    litellm_master_key="$(random_hex)"
  fi
fi

set_env_value "$LITELLM_ENV_FILE" LITELLM_MASTER_KEY "$litellm_master_key"
set_env_value "$MANAGER_ENV_FILE" LITELLM_MASTER_KEY "$litellm_master_key"
set_env_value "$MANAGER_ENV_FILE" OPENSANDBOX_API_KEY "$(server_key)"

for name in RUNNER_MANAGER_CREDENTIAL_ENCRYPTION_KEY RUNNER_MANAGER_BOOTSTRAP_SERVICE_TOKEN RUNNER_MANAGER_BOOTSTRAP_ADMIN_TOKEN; do
  current="$(env_value "$MANAGER_ENV_FILE" "$name")"
  if is_unset "$current"; then
    if [[ "$name" == "RUNNER_MANAGER_CREDENTIAL_ENCRYPTION_KEY" ]]; then
      current="$(random_fernet_key)"
    else
      current="$(random_hex)"
    fi
    set_env_value "$MANAGER_ENV_FILE" "$name" "$current"
  fi
done

service_token="$(env_value "$MANAGER_ENV_FILE" RUNNER_MANAGER_BOOTSTRAP_SERVICE_TOKEN)"
admin_token="$(env_value "$MANAGER_ENV_FILE" RUNNER_MANAGER_BOOTSTRAP_ADMIN_TOKEN)"
if [[ "$service_token" == "$admin_token" ]]; then
  echo "RUNNER_MANAGER_BOOTSTRAP_SERVICE_TOKEN and RUNNER_MANAGER_BOOTSTRAP_ADMIN_TOKEN must differ." >&2
  exit 1
fi

echo "Local configuration is ready."
echo "Add at least one provider API key to .litellm.env, then run: docker compose up -d --build"
