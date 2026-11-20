#!/usr/bin/env bash
# Copyright (c) 2026 Netresearch DTT GmbH
# SPDX-License-Identifier: MIT
# Recreate sampled Jira comments in a disposable Jira 9.12.3 and OpenProject 17.9.7 behind a
# private CA, import them with j2o and j2o-comments, then check every test account's view.
# Usage: run_company_comment_acceptance.sh [up|test|down|all]
set -euo pipefail

: "${J2O_COMPANY_COMMENT_FIXTURE:?Set the private sample fixture produced by company_comment_sample.py}"
: "${J2O_COMPANY_COMMENT_STATE:?Set a private state directory outside the checkout}"
: "${J2O_ACCEPT_JIRA_LICENSE:?Set a file with a Jira Software Data Center test license}"
checkout=$(cd "$(dirname "$0")/../.." && pwd -P)
state=$(mkdir -p -m 700 "$J2O_COMPANY_COMMENT_STATE" && cd "$J2O_COMPANY_COMMENT_STATE" && pwd -P)
case "$state/" in "$checkout"/*) echo "State must live outside the checkout" >&2; exit 2 ;; esac
chmod 700 "$state"
umask 077
jira_port=${J2O_ACCEPT_JIRA_PORT:-18443}
op_port=${J2O_ACCEPT_OP_PORT:-18444}
project=$(cat "$state/compose-project" 2>/dev/null || echo "j2o-accept-$(openssl rand -hex 4)")
echo "$project" > "$state/compose-project"
compose=(docker compose -p "$project" -f "$state/compose.yml")

write_stack() {
    mkdir -p "$state/tls"
    if [[ ! -s "$state/tls/ca.pem" ]]; then
        openssl req -x509 -newkey rsa:3072 -nodes -days 2 -subj "/CN=j2o acceptance CA" \
            -addext "basicConstraints=critical,CA:TRUE" -addext "keyUsage=critical,keyCertSign,cRLSign" -keyout "$state/tls/ca.key" -out "$state/tls/ca.pem" 2>/dev/null
        openssl req -newkey rsa:3072 -nodes -subj "/CN=localhost" -keyout "$state/tls/leaf.key" \
            -out "$state/tls/leaf.csr" 2>/dev/null
        printf '%s\n' "subjectAltName=DNS:localhost,IP:127.0.0.1" "extendedKeyUsage=serverAuth" \
            "keyUsage=critical,digitalSignature,keyEncipherment" "authorityKeyIdentifier=keyid" "subjectKeyIdentifier=hash" > "$state/tls/leaf.ext"
        openssl x509 -req -in "$state/tls/leaf.csr" -CA "$state/tls/ca.pem" -CAkey "$state/tls/ca.key" \
            -CAcreateserial -days 2 -extfile "$state/tls/leaf.ext" -out "$state/tls/leaf.pem" 2>/dev/null
        chmod 644 "$state/tls/leaf.pem" "$state/tls/leaf.key" "$state/tls/ca.pem"
    fi
    [[ -s "$state/op-secret" ]] || openssl rand -hex 64 > "$state/op-secret"
    cat > "$state/nginx.conf" <<NGINX
events {}
http {
  client_max_body_size 100m;
  proxy_read_timeout 300s;
  server {
    listen $jira_port ssl;
    ssl_certificate /tls/leaf.pem;
    ssl_certificate_key /tls/leaf.key;
    location / { proxy_pass http://jira:8080; proxy_set_header Host \$http_host; proxy_set_header X-Forwarded-Proto https; }
  }
  server {
    listen $op_port ssl;
    ssl_certificate /tls/leaf.pem;
    ssl_certificate_key /tls/leaf.key;
    location / { proxy_pass http://op:8080; proxy_set_header Host \$http_host; proxy_set_header X-Forwarded-Proto https; }
  }
}
NGINX
    chmod 644 "$state/nginx.conf"
    cat > "$state/compose.yml" <<COMPOSE
services:
  jira:
    image: atlassian/jira-software:9.12.3
    environment:
      ATL_PROXY_NAME: localhost
      ATL_PROXY_PORT: "$jira_port"
      ATL_TOMCAT_SCHEME: https
      ATL_TOMCAT_SECURE: "true"
      JVM_MINIMUM_MEMORY: 1024m
      JVM_MAXIMUM_MEMORY: 2048m
      JVM_SUPPORT_RECOMMENDED_ARGS: -Djira.websudo.is.disabled=true
  opdb:
    image: postgres:17-alpine
    environment:
      POSTGRES_USER: openproject
      POSTGRES_DB: openproject
      POSTGRES_HOST_AUTH_METHOD: trust
  op:
    image: openproject/openproject:17.9.7-slim
    depends_on: [opdb]
    environment:
      DATABASE_URL: postgres://openproject@opdb/openproject
      RAILS_ENV: production
      SECRET_KEY_BASE: $(cat "$state/op-secret")
      OPENPROJECT_HOST__NAME: localhost:$op_port
      OPENPROJECT_HTTPS: "true"
      OPENPROJECT_SEED__LOCALE: en
      OPENPROJECT_DEFAULT__LANGUAGE: en
    command: ["bash", "-c", "until pg_isready -q -h opdb -U openproject; do sleep 1; done; bundle exec rails db:migrate && bundle exec rails db:seed && exec ./docker/prod/web"]
  proxy:
    image: nginx:1.31.9
    depends_on: [jira, op]
    ports: ["127.0.0.1:$jira_port:$jira_port", "127.0.0.1:$op_port:$op_port"]
    volumes: ["$state/nginx.conf:/etc/nginx/nginx.conf:ro", "$state/tls:/tls:ro"]
COMPOSE
}

wait_for() {
    local url=$1 expect=$2
    for _ in {1..180}; do
        if [[ -n "$("${compose[@]}" ps -a -q --status exited)" ]]; then
            "${compose[@]}" logs --tail 40 >&2
            echo "A container of the acceptance stack exited" >&2
            return 1
        fi
        if curl -s --cacert "$state/tls/ca.pem" -o /dev/null -w '%{http_code}' "$url" | grep -qE "$expect"; then return 0; fi
        sleep 5
    done
    echo "Timed out waiting for $url" >&2
    return 1
}

up() {
    write_stack
    "${compose[@]}" up -d
    wait_for "https://localhost:$op_port/api/v3" '^(200|401)$'
    wait_for "https://localhost:$jira_port/status" '^200$'
    [[ -s "$state/jira-admin-password" ]] || openssl rand -hex 16 > "$state/jira-admin-password"
    (cd "$checkout" && uv run --no-sync python - "$state" "https://localhost:$jira_port" "$J2O_ACCEPT_JIRA_LICENSE" <<'PY'
import sys
from pathlib import Path
from tests.integration.company_comment_stack import JiraAdmin
state, url, license_file = Path(sys.argv[1]), sys.argv[2], Path(sys.argv[3])
admin = JiraAdmin(url, str(state / "tls/ca.pem"), "acceptadmin", (state / "jira-admin-password").read_text().strip())
admin.setup(license_file.read_text().strip())
print("Jira setup complete")
PY
    )
}


down() {
    "${compose[@]}" down -v --remove-orphans
    rm -f "$state/compose-project"
}

run_test() {
    export J2O_ACCEPT_STACK="$state/stack.json"
    printf '{"compose_project": "%s", "jira": "https://localhost:%s", "openproject": "https://localhost:%s", "ca": "%s"}\n' \
        "$project" "$jira_port" "$op_port" "$state/tls/ca.pem" > "$state/stack.json"
    export J2O_RUN_INTEGRATION=true J2O_ENABLE_DOCKER=true
    cd "$checkout"
    local status=0
    uv run --no-sync python -m pytest -o addopts='' -p no:cacheprovider -q \
        tests/integration/test_company_comment_acceptance.py || status=$?
    # Print counts only; comment text stays in the private state directory.
    uv run --no-sync python - "$state/acceptance-results.json" <<'PY' || true
import json, sys
data = json.load(open(sys.argv[1]))
print(f"j2o migrate exit code: {data['migrate_exit']}")
print("Components that did not run cleanly:", {k: v for k, v in data["components"].items() if v != "ran"})
print("| Case | Jira authorized | Jira other | OpenProject authorized | OpenProject other | Restricted seen by other (Jira/OP) | Anonymous (Jira/OP) | Archive |")
print("|---|---|---|---|---|---|---|---|")
for case, row in data["cases"].items():
    archive = row["cutover"] if isinstance(row["cutover"], str) else ", ".join(f"{k} {v}" for k, v in row["cutover"].items())
    print(f"| {case} | {len(row['jira']['authorized'])} | {len(row['jira']['other'])} | {len(row['op']['authorized'])} ({row['op']['authorized_how']}) "
          f"| {len(row['op']['other'])} ({row['op']['other_how']}) | {row['restricted']['other']['jira']}/{row['restricted']['other']['op']} "
          f"| HTTP {row['anonymous']['jira']}/{row['anonymous']['op']} | {archive[:90]} |")
PY
    return "$status"
}

case "${1:-all}" in
    up) up ;;
    test) run_test ;;
    down) down ;;
    all) trap down EXIT; up; run_test ;;
    *) echo "Usage: $0 [up|test|down|all]" >&2; exit 2 ;;
esac
