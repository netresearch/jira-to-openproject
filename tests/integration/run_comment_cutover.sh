#!/usr/bin/env bash
# Copyright (c) 2026 Netresearch DTT GmbH
# SPDX-License-Identifier: MIT
# Start a private, temporary OpenProject 17.9.7 and verify access after Jira shutdown.
set -euo pipefail

network=
fixture_dir=
db=
app=
cleanup() {
    set +e
    if [[ -n "$app" ]]; then docker rm -fv "$app" >/dev/null; fi
    if [[ -n "$db" ]]; then docker rm -fv "$db" >/dev/null; fi
    if [[ -n "$network" ]]; then docker network rm "$network" >/dev/null; fi
    if [[ -n "$fixture_dir" ]]; then rm -rf -- "$fixture_dir"; fi
}
trap cleanup EXIT
fixture_dir=$(mktemp -d)
export UV_PROJECT_ENVIRONMENT="$fixture_dir/venv"
network=$(docker network create --internal "j2o-cutover-$(openssl rand -hex 8)")
# This fixture's pg_dump client ships with the OpenProject image (PostgreSQL 17).
db=$(docker run -d --network "$network" --network-alias postgres \
    --tmpfs /var/lib/postgresql/data -e POSTGRES_USER=openproject -e POSTGRES_DB=openproject \
    -e POSTGRES_HOST_AUTH_METHOD=trust postgres:17-alpine)
for _ in {1..60}; do
    if docker exec "$db" pg_isready -h 127.0.0.1 -U openproject >/dev/null 2>&1; then break; fi
    sleep 1
done
docker exec "$db" pg_isready -h 127.0.0.1 -U openproject
app=$(docker run -d --network "$network" -e DATABASE_URL=postgres://openproject@postgres/openproject \
    -e RAILS_ENV=production -e SECRET_KEY_BASE="$(openssl rand -hex 64)" \
    -e OPENPROJECT_HOST__NAME=localhost -e OPENPROJECT_HTTPS=false -e OPENPROJECT_SEED__LOCALE=en \
    openproject/openproject:17.9.7-slim sleep infinity)
docker exec "$app" bundle exec rails db:migrate db:seed
export J2O_TEST_OPENPROJECT_CONTAINER="$app" J2O_OPENPROJECT_CONTAINER="$app"
export J2O_OPENPROJECT_SERVER=localhost J2O_RUN_INTEGRATION=true J2O_ENABLE_DOCKER=true
uv sync --python 3.14 --frozen --no-build --no-install-project --extra test --extra dev
uv run --no-sync python -m pytest -o addopts='' -q \
    tests/integration/test_group_comment_cutover.py \
    tests/integration/test_protected_comment_cutover.py \
    tests/integration/test_openproject_agile_roundtrip.py \
    tests/integration/test_native_board_layout.py \
    tests/integration/test_membership_file_roundtrip.py
