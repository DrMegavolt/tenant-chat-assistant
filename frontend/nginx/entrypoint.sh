#!/bin/sh
# Render the site configuration from the two configured upstreams, then hand the
# process to nginx.
#
# Rendering at start rather than at build keeps one immutable image usable
# against a Kubernetes Service, a compose network, and a port-forwarded backend.
set -eu

: "${CHAT_BACKEND_ORIGIN:=http://chat-backend:8000}"
: "${CHAT_ADMIN_ORIGIN:=http://chat-admin:8004}"

# An origin carrying a path or a stray quote would either silently rewrite every
# proxied URI or inject directives into the rendered configuration.
validate_origin() {
    printf '%s' "$2" | grep -Eq '^https?://[A-Za-z0-9._-]+(:[0-9]{1,5})?$' && return 0
    echo "$1 must be scheme://host[:port] with no trailing path, got '$2'" >&2
    exit 78
}

validate_origin CHAT_BACKEND_ORIGIN "$CHAT_BACKEND_ORIGIN"
validate_origin CHAT_ADMIN_ORIGIN "$CHAT_ADMIN_ORIGIN"
export CHAT_BACKEND_ORIGIN CHAT_ADMIN_ORIGIN

# The variable list is explicit: without it envsubst would also expand nginx's
# own `$uri`, `$host`, and `$proxy_add_x_forwarded_for` to empty strings.
envsubst '${CHAT_BACKEND_ORIGIN} ${CHAT_ADMIN_ORIGIN}' \
    </etc/nginx/templates/site.conf.template \
    >/etc/nginx/conf.d/site.conf

nginx -t
exec "$@"
