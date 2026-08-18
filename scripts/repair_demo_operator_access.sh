#!/usr/bin/env bash
# Reset only the configured demo operator to its Kubernetes Secret password,
# then verify authentication and authorization. No credential is printed.

set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
config_path="${DEMO_ACCESS_CONFIG:-$repo_root/.local/k8s/demo-access.env}"

if [[ $# -ne 0 ]]; then
    echo "ERROR: configure this helper with $config_path, not command-line arguments." >&2
    exit 2
fi

if [[ -f "$config_path" ]]; then
    # This is a trusted, operator-owned shell environment file under .local/.
    # shellcheck disable=SC1090
    source "$config_path"
fi

: "${IDENTITY_NAMESPACE:=identity}"
: "${KEYCLOAK_BOOTSTRAP_RESOURCE:=keycloak-bootstrap-user}"
: "${KEYCLOAK_ADMIN_CREDENTIAL_RESOURCE:=keycloak-admin-credentials}"
: "${KEYCLOAK_POD_SELECTOR:=app.kubernetes.io/name=keycloak,app.kubernetes.io/instance=keycloak}"
: "${KEYCLOAK_REALM:=tenantchat}"
: "${TENANTCHAT_OPERATOR_GROUP:=platform_admin}"
: "${KEYCLOAK_INTERNAL_URL:=http://127.0.0.1:8080}"

if [[ "${DEMO_ALLOW_OPERATOR_PASSWORD_RESET:-}" != "true" ]]; then
    echo "ERROR: this command resets the configured demo operator password." >&2
    echo "Re-run with DEMO_ALLOW_OPERATOR_PASSWORD_RESET=true after confirming the target cluster." >&2
    exit 2
fi

command -v kubectl >/dev/null 2>&1 || {
    echo "ERROR: kubectl is required." >&2
    exit 1
}

secret_value() {
    local secret_name="$1"
    local key="$2"
    kubectl -n "$IDENTITY_NAMESPACE" get secret "$secret_name" \
        -o go-template --template="{{ index .data \"$key\" | base64decode }}"
}

operator_username="$(secret_value "$KEYCLOAK_BOOTSTRAP_RESOURCE" username)"
operator_password="$(secret_value "$KEYCLOAK_BOOTSTRAP_RESOURCE" password)"
admin_username="$(secret_value "$KEYCLOAK_ADMIN_CREDENTIAL_RESOURCE" username)"
admin_password="$(secret_value "$KEYCLOAK_ADMIN_CREDENTIAL_RESOURCE" password)"
keycloak_pod="$(kubectl -n "$IDENTITY_NAMESPACE" get pod \
    -l "$KEYCLOAK_POD_SELECTOR" \
    -o jsonpath='{.items[0].metadata.name}')"

if [[ -z "$keycloak_pod" ]]; then
    echo "ERROR: no Keycloak pod found in namespace $IDENTITY_NAMESPACE." >&2
    exit 1
fi

kubectl -n "$IDENTITY_NAMESPACE" exec "$keycloak_pod" -- env \
    DEMO_OPERATOR_USERNAME="$operator_username" \
    DEMO_OPERATOR_PASSWORD="$operator_password" \
    DEMO_KEYCLOAK_ADMIN_USERNAME="$admin_username" \
    DEMO_KEYCLOAK_ADMIN_PASSWORD="$admin_password" \
    DEMO_KEYCLOAK_REALM="$KEYCLOAK_REALM" \
    DEMO_OPERATOR_GROUP="$TENANTCHAT_OPERATOR_GROUP" \
    DEMO_KEYCLOAK_INTERNAL_URL="$KEYCLOAK_INTERNAL_URL" \
    sh -c '
        set -eu
        kcadm=/opt/keycloak/bin/kcadm.sh
        admin_config=/tmp/kcadm-demo-repair-admin.json
        operator_config=/tmp/kcadm-demo-repair-operator.json
        trap "rm -f $admin_config $operator_config" EXIT

        "$kcadm" config credentials --config "$admin_config" \
            --server "$DEMO_KEYCLOAK_INTERNAL_URL" --realm master \
            --user "$DEMO_KEYCLOAK_ADMIN_USERNAME" \
            --password "$DEMO_KEYCLOAK_ADMIN_PASSWORD" >/dev/null 2>&1

        user_id="$("$kcadm" get users -r "$DEMO_KEYCLOAK_REALM" \
            -q "username=$DEMO_OPERATOR_USERNAME" --fields id \
            --format csv --noquotes --config "$admin_config")"
        test -n "$user_id"

        "$kcadm" get "users/$user_id/groups" -r "$DEMO_KEYCLOAK_REALM" --fields name \
            --format csv --noquotes --config "$admin_config" \
            | grep -Fxq "$DEMO_OPERATOR_GROUP"

        "$kcadm" set-password -r "$DEMO_KEYCLOAK_REALM" --userid "$user_id" \
            --new-password "$DEMO_OPERATOR_PASSWORD" \
            --config "$admin_config" >/dev/null

        "$kcadm" config credentials --config "$operator_config" \
            --server "$DEMO_KEYCLOAK_INTERNAL_URL" --realm "$DEMO_KEYCLOAK_REALM" \
            --user "$DEMO_OPERATOR_USERNAME" \
            --password "$DEMO_OPERATOR_PASSWORD" >/dev/null 2>&1
    '

echo "PASS: the Secret-backed demo operator authenticates with the required group."
