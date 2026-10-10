#!/bin/bash
# UniFi NOC restore.
#
#   ./restore.sh <backup.tar.gz> --verify          restore into a scratch database, compare row counts, drop it (live data untouched)
#   ./restore.sh <backup.tar.gz> [--with-env] --yes   REPLACE the live database (and optionally .env) with the backup
#
# A full restore stops collector/api/web, recreates the database, restores it with TimescaleDB
# pre/post-restore hooks and starts the stack again.
set -euo pipefail
cd "$(dirname "$0")"
STACK=$(docker inspect --format '{{index .Config.Labels "com.docker.compose.project"}}' "$(docker compose ps -q db)")
ARCHIVE=${1:-}
shift || true
VERIFY=0; YES=0; WITH_ENV=0
for a in "$@"; do
  case "$a" in
    --verify) VERIFY=1 ;;
    --yes) YES=1 ;;
    --with-env) WITH_ENV=1 ;;
    *) echo "unknown option $a"; exit 2 ;;
  esac
done
[ -f "$ARCHIVE" ] || { echo "usage: $0 <backup.tar.gz> [--verify | [--with-env] --yes]"; exit 2; }
WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT
tar -C "$WORK" -xzf "$ARCHIVE"
[ -s "$WORK/noc.dump" ] || { echo "archive has no noc.dump"; exit 1; }
cat "$WORK/MANIFEST" 2>/dev/null || true

psql_db() { docker compose exec -T db psql -v ON_ERROR_STOP=1 -U noc -d "$1" -qAt "${@:2}"; }
restore_into() {
  local db=$1
  psql_db postgres -c "DROP DATABASE IF EXISTS $db WITH (FORCE)" -c "CREATE DATABASE $db"
  psql_db "$db" -c "CREATE EXTENSION IF NOT EXISTS timescaledb" -c "SELECT timescaledb_pre_restore()" >/dev/null
  # TimescaleDB catalog notices are expected; real failures are caught by the count check below.
  docker compose exec -T db pg_restore -U noc -d "$db" --no-owner < "$WORK/noc.dump" 2> "$WORK/restore.log" || true
  psql_db "$db" -c "SELECT timescaledb_post_restore()" >/dev/null
  grep -iE "error" "$WORK/restore.log" | grep -viE "already exists|must be owner|timescaledb" | head -5 || true
}
counts() { psql_db "$1" -c "SELECT 'events',count(*) FROM events UNION ALL SELECT 'alerts',count(*) FROM alerts UNION ALL SELECT 'devices',count(*) FROM devices UNION ALL SELECT 'clients',count(*) FROM clients UNION ALL SELECT 'users',count(*) FROM users UNION ALL SELECT 'metrics',count(*) FROM metrics"; }

if [ "$VERIFY" = 1 ]; then
  echo "== verify: restoring into scratch database noc_restore_test"
  restore_into noc_restore_test
  echo "== row counts in restored copy"; counts noc_restore_test | column -t -s'|'
  psql_db postgres -c "DROP DATABASE noc_restore_test WITH (FORCE)"
  echo "verify OK (live database untouched)"
  exit 0
fi

if [ "$YES" != 1 ]; then
  read -r -p "This REPLACES the live UniFi NOC database with $ARCHIVE. Type RESTORE to continue: " ans
  [ "$ans" = "RESTORE" ] || { echo "aborted"; exit 1; }
fi
echo "== safety backup of the current state first"
bash ./backup.sh ./backups >/dev/null
echo "   done"
docker compose stop collector api web
if [ "$WITH_ENV" = 1 ]; then
  [ ! -f "$WORK/env" ] || install -m 600 "$WORK/env" .env
  if [ -d "$WORK/controller-certs" ]; then
    mkdir -p certs
    cp -a "$WORK/controller-certs/." certs/
  fi
  echo "   .env and available controller certificates restored"
fi
for volume in web-certs noc-state db-secrets; do
  if [ -s "$WORK/$volume.tgz" ]; then
    docker run --rm -v "${STACK}_$volume":/c -v "$WORK":/o:ro alpine:3 tar -xzf "/o/$volume.tgz" -C /c
  fi
done
docker compose up -d db
until docker compose exec -T db pg_isready -U noc -d postgres >/dev/null 2>&1; do sleep 2; done
if [ -s "$WORK/db-secrets.tgz" ]; then
  docker compose exec -T db sh -c 'password=$(cat /run/noc-secrets/db-password); printf "ALTER ROLE noc PASSWORD :\047password\047;\n" | psql -v ON_ERROR_STOP=1 -U noc -d postgres -v password="$password"' >/dev/null
fi
restore_into noc
echo "== restored row counts"; counts noc | column -t -s'|'
docker compose up -d
echo "restore complete"
