#!/usr/bin/env bash
# Snapshot of the OpenProject target before a production migration run, and a restore drill (#491).
#
#   scripts/openproject-snapshot.sh take  <parent directory>   # dump, attachment archive, counts, checksums
#   scripts/openproject-snapshot.sh drill <snapshot directory> # restore into a scratch database and compare
#   scripts/openproject-snapshot.sh verify <snapshot directory> # compare the live target with the snapshot
#
# Run it on the Docker host of the OpenProject target, while no migration runs.
# docs/operator-rollback.md describes the restore of the live target.
set -euo pipefail

usage() {
  cat >&2 <<'EOF'
Usage: scripts/openproject-snapshot.sh take   <parent directory>
       scripts/openproject-snapshot.sh drill  <snapshot directory>
       scripts/openproject-snapshot.sh verify <snapshot directory>

Environment:
  J2O_SNAPSHOT_DB_CONTAINER   PostgreSQL container of the target (required)
  J2O_SNAPSHOT_APP_CONTAINER  OpenProject container (default: $J2O_OPENPROJECT_CONTAINER)
  J2O_SNAPSHOT_DB_NAME        database name (default: openproject)
EOF
  exit 2
}

[[ $# -eq 2 ]] || usage
db="${J2O_SNAPSHOT_DB_CONTAINER:?set J2O_SNAPSHOT_DB_CONTAINER to the PostgreSQL container}"
app="${J2O_SNAPSHOT_APP_CONTAINER:-${J2O_OPENPROJECT_CONTAINER:?set J2O_SNAPSHOT_APP_CONTAINER or J2O_OPENPROJECT_CONTAINER}}"
db_name="${J2O_SNAPSHOT_DB_NAME:-openproject}"
db_user="$(docker exec "$db" printenv POSTGRES_USER)"

# Row counts of the tables a migration writes; the drill compares them after the restore.
counts() {
  local database="$1"
  docker exec "$db" psql -U "$db_user" -d "$database" -v ON_ERROR_STOP=1 -AtF $'\t' -c "
    SELECT 'projects', count(*) FROM projects UNION ALL
    SELECT 'work_packages', count(*) FROM work_packages UNION ALL
    SELECT 'journals', count(*) FROM journals UNION ALL
    SELECT 'custom_values', count(*) FROM custom_values UNION ALL
    SELECT 'attachments', count(*) FROM attachments UNION ALL
    SELECT 'time_entries', count(*) FROM time_entries UNION ALL
    SELECT 'relations', count(*) FROM relations UNION ALL
    SELECT 'versions', count(*) FROM versions UNION ALL
    SELECT 'members', count(*) FROM members UNION ALL
    SELECT 'users', count(*) FROM users"
}

take() {
  local parent="$1" out
  umask 077
  out="$parent/openproject-$(date -u +%Y%m%dT%H%M%SZ)"
  mkdir -p "$parent"
  mkdir "$out"
  local storage
  storage="$(docker exec "$app" printenv OPENPROJECT_ATTACHMENTS__STORAGE__PATH || echo /var/openproject/assets/files)"
  echo "+ pg_dump $db_name from $db" >&2
  docker exec "$db" pg_dump -U "$db_user" -Fc -d "$db_name" > "$out/openproject.dump.partial"
  mv "$out/openproject.dump.partial" "$out/openproject.dump"
  echo "+ tar $storage from $app" >&2
  docker exec "$app" tar -C "$(dirname "$storage")" -cf - "$(basename "$storage")" > "$out/attachments.tar.partial"
  mv "$out/attachments.tar.partial" "$out/attachments.tar"
  counts "$db_name" > "$out/counts.tsv"
  docker exec "$app" find "$storage" -type f | awk 'END {print NR}' > "$out/attachment-files.txt"
  printf '%s\n' "$storage" > "$out/attachment-path.txt"
  (cd "$out" && sha256sum openproject.dump attachments.tar counts.tsv attachment-files.txt attachment-path.txt > SHA256SUMS)
  echo "$out"
}

drill() {
  local dir="$1" scratch
  (cd "$dir" && sha256sum --check --quiet SHA256SUMS)
  scratch="j2o_restore_drill_$(date -u +%s)"
  echo "+ createdb $scratch on $db" >&2
  docker exec "$db" createdb -U "$db_user" "$scratch"
  # shellcheck disable=SC2064 # expand now: the trap must drop this scratch database
  trap "docker exec '$db' dropdb -U '$db_user' --if-exists '$scratch' >/dev/null" EXIT
  docker exec -i "$db" pg_restore -U "$db_user" -d "$scratch" --no-owner --exit-on-error < "$dir/openproject.dump"
  if ! diff <(counts "$scratch") "$dir/counts.tsv"; then
    echo "drill FAILED: the restored row counts differ from the snapshot" >&2
    exit 1
  fi
  local files
  files="$(tar -tf "$dir/attachments.tar" | awk '!/\/$/ {n++} END {print n + 0}')"
  if [[ "$files" != "$(cat "$dir/attachment-files.txt")" ]]; then
    echo "drill FAILED: the archive holds $files files, the snapshot counted $(cat "$dir/attachment-files.txt")" >&2
    exit 1
  fi
  echo "drill OK: $(wc -l < "$dir/counts.tsv") table counts match, $files attachment files in the archive"
}

# After a restore of the live target: its counts and attachment files must equal the snapshot's.
verify() {
  local dir="$1" storage files
  (cd "$dir" && sha256sum --check --quiet SHA256SUMS)
  if ! diff <(counts "$db_name") "$dir/counts.tsv"; then
    echo "verify FAILED: the live row counts differ from the snapshot" >&2
    exit 1
  fi
  storage="$(cat "$dir/attachment-path.txt")"
  files="$(docker exec "$app" find "$storage" -type f | awk 'END {print NR}')"
  if [[ "$files" != "$(cat "$dir/attachment-files.txt")" ]]; then
    echo "verify FAILED: $storage holds $files files, the snapshot counted $(cat "$dir/attachment-files.txt")" >&2
    exit 1
  fi
  echo "verify OK: $(wc -l < "$dir/counts.tsv") table counts and $files attachment files match the snapshot"
}

case "$1" in
  take) take "$2" ;;
  drill) drill "$2" ;;
  verify) verify "$2" ;;
  *) usage ;;
esac
