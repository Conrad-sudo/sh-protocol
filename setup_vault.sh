#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="$SCRIPT_DIR/.env"
VAULT_CONTAINER="vault-dev"
VAULT_ADDR_VAL="http://127.0.0.1:8200"
VAULT_TOKEN_VAL="dev-root-token"

v() {
    docker exec "$VAULT_CONTAINER" sh -c "VAULT_ADDR=$VAULT_ADDR_VAL VAULT_TOKEN=$VAULT_TOKEN_VAL $*"
}

echo "==> Enabling transit secrets engine..."
v "vault secrets enable transit" 2>/dev/null || echo "    (already enabled)"

echo "==> Creating session-keys transit key..."
v "vault write -f transit/keys/session-keys type=aes256-gcm96" 2>/dev/null || echo "    (already exists)"

echo "==> Enabling AppRole auth method..."
v "vault auth enable approle" 2>/dev/null || echo "    (already enabled)"

echo "==> Writing wallet-agent policy..."
printf 'path "transit/encrypt/session-keys" { capabilities = ["update"] }\npath "transit/decrypt/session-keys" { capabilities = ["update"] }\n' \
    | docker exec -i "$VAULT_CONTAINER" sh -c \
        "VAULT_ADDR=$VAULT_ADDR_VAL VAULT_TOKEN=$VAULT_TOKEN_VAL vault policy write wallet-agent -"

echo "==> Creating wallet-agent AppRole role..."
v "vault write auth/approle/role/wallet-agent \
    token_policies=wallet-agent \
    token_ttl=1h \
    token_max_ttl=4h"

echo "==> Reading role-id..."
ROLE_ID=$(v "vault read -field=role_id auth/approle/role/wallet-agent/role-id")

echo "==> Generating secret-id..."
SECRET_ID_JSON=$(v "vault write -format=json -f auth/approle/role/wallet-agent/secret-id")
SECRET_ID=$(echo "$SECRET_ID_JSON" | python3 -c \
    "import sys,json; d=json.load(sys.stdin); print(d['data']['secret_id'])")
SECRET_ID_ACCESSOR=$(echo "$SECRET_ID_JSON" | python3 -c \
    "import sys,json; d=json.load(sys.stdin); print(d['data']['secret_id_accessor'])")

echo ""
echo "  Role ID:            $ROLE_ID"
echo "  Secret ID:          $SECRET_ID"
echo "  Secret ID Accessor: $SECRET_ID_ACCESSOR"
echo ""

set_env() {
    local key="$1" val="$2"
    touch "$ENV_FILE"
    if grep -qE "^${key}[[:space:]]*=" "$ENV_FILE"; then
        sed -i '' "s|^${key}[[:space:]]*=.*|${key}=${val}|" "$ENV_FILE"
    else
        echo "${key}=${val}" >> "$ENV_FILE"
    fi
}

echo "==> Updating $ENV_FILE..."
set_env "VAULT_ROLE_ID"            "$ROLE_ID"
set_env "VAULT_SECRET_ID"          "$SECRET_ID"
set_env "VAULT_SECRET_ID_ACCESSOR" "$SECRET_ID_ACCESSOR"
set_env "VAULT_ADDR"               "$VAULT_ADDR_VAL"

echo "==> Done. Keep the secret_id_accessor ($SECRET_ID_ACCESSOR) — you'll need it to revoke the secret-id later."
