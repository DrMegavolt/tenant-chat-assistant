#!/usr/bin/env bash
set -euo pipefail

services=(postgres elasticsearch embedding api worker web)
running="$(docker compose --profile app ps --status running --services)"
for service in "${services[@]}"; do
  if ! grep -Fxq "$service" <<<"$running"; then
    echo "compose smoke: $service is not running" >&2
    docker compose --profile app ps >&2
    exit 1
  fi
done

migration_exit="$(docker compose --profile app ps -a --format '{{.ExitCode}}' migrate)"
if [[ "$migration_exit" != "0" ]]; then
  echo "compose smoke: migration exit code is ${migration_exit:-missing}" >&2
  exit 1
fi

docker compose --profile app exec -T api \
  python -c "import urllib.request as r; r.urlopen('http://127.0.0.1:8004/readyz', timeout=10)" \
  >/dev/null
docker compose --profile app exec -T web \
  wget -q -O /dev/null http://127.0.0.1:8080/healthz
docker compose --profile app exec -T web \
  wget -q -O /dev/null http://127.0.0.1:8080/api/tenants

echo "compose smoke passed: migrations, dependency readiness, and visitor gateway"
