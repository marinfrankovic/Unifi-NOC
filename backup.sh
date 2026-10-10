#!/bin/bash
# UniFi NOC backup: configuration (.env, compose), full database (metrics, rollups, events, alerts,
# alert rules, users, collector state, pinned gateway host keys) and the web TLS certificate.
#
#   ./backup.sh [destination-dir]          default ./backups, keeps the newest $KEEP archives (default 14)
#
# The archive contains secrets (.env) - it is created with mode 600 inside a mode 700 directory.
set -euo pipefail
cd "$(dirname "$0")"
DEST=${1:-./backups}
STACK=$(docker inspect --format '{{index .Config.Labels "com.docker.compose.project"}}' "$(docker compose ps -q db)")
KEEP=${KEEP:-14}
TS=$(date +%Y%m%d-%H%M%S)
NAME="unifi-noc-$TS"
umask 077
mkdir -p "$DEST"
chmod 700 "$DEST"
WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT

echo "[1/4] configuration"
[ ! -f .env ] || cp -a .env "$WORK/env"
cp -a docker-compose.yml "$WORK/docker-compose.yml"
[ ! -f docker-compose.override.yml ] || cp -a docker-compose.override.yml "$WORK/docker-compose.override.yml"
if [ -d certs ]; then cp -a certs "$WORK/controller-certs"; fi

echo "[2/4] database (pg_dump custom format)"
docker compose exec -T db pg_dump -U noc -d noc -Fc --no-owner > "$WORK/noc.dump"
docker compose exec -T db pg_restore -l < "$WORK/noc.dump" > "$WORK/noc.dump.toc"   # proves the dump is readable
echo "      $(grep -c 'TABLE DATA' "$WORK/noc.dump.toc") table data sections, $(du -h "$WORK/noc.dump" | cut -f1)"

echo "[3/4] web certificate"
docker run --rm -v "${STACK}_web-certs":/c:ro -v "$WORK":/o alpine:3 tar -C /c -czf /o/web-certs.tgz .
for volume in noc-state db-secrets; do
	if docker volume inspect "${STACK}_$volume" >/dev/null 2>&1; then
		docker run --rm -v "${STACK}_$volume":/c:ro -v "$WORK":/o alpine:3 tar -C /c -czf "/o/$volume.tgz" .
	fi
done

echo "[4/4] archive"
cat > "$WORK/MANIFEST" <<EOF
created=$(date -Is)
host=$(hostname)
images=$(docker compose config --images 2>/dev/null | sort -u | tr '\n' ' ')
EOF
tar -C "$WORK" -czf "$DEST/$NAME.tar.gz" .
chmod 600 "$DEST/$NAME.tar.gz"
echo "backup written: $DEST/$NAME.tar.gz ($(du -h "$DEST/$NAME.tar.gz" | cut -f1))"

ls -1t "$DEST"/unifi-noc-*.tar.gz 2>/dev/null | tail -n +$((KEEP + 1)) | xargs -r rm -f --
