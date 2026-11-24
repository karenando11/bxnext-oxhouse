#!/usr/bin/env bash
# Nightly database dump to object storage, kept for BACKUP_KEEP_DAYS (default 30).
#   deploy/backup.sh            run from the deploy directory on the server
# Needs the aws CLI (any S3-compatible bucket) and the settings in .env.
set -euo pipefail
cd "$(dirname "$0")"
set -a; . ./.env; set +a

: "${BACKUP_S3_URI:?set BACKUP_S3_URI in .env}"
KEEP_DAYS="${BACKUP_KEEP_DAYS:-30}"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
FILE="oxhouse-${STAMP}.dump"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

S3=(aws s3)
[ -n "${AWS_ENDPOINT_URL:-}" ] && S3=(aws --endpoint-url "$AWS_ENDPOINT_URL" s3)

# Custom format: compressed, and pg_restore can restore all or part of it.
docker compose -f docker-compose.prod.yml exec -T db \
  pg_dump -U "${POSTGRES_USER:-oxhouse}" -d "${POSTGRES_DB:-oxhouse}" -Fc > "$TMP/$FILE"
test -s "$TMP/$FILE"
"${S3[@]}" cp "$TMP/$FILE" "${BACKUP_S3_URI%/}/$FILE" --only-show-errors
echo "uploaded $FILE ($(wc -c < "$TMP/$FILE") bytes)"

# Remove dumps older than the retention period. (A bucket lifecycle rule does the same; this is
# the second line of defence.)
CUTOFF="$(date -u -d "-${KEEP_DAYS} days" +%Y%m%d 2>/dev/null || date -u -v-"${KEEP_DAYS}"d +%Y%m%d)"
"${S3[@]}" ls "${BACKUP_S3_URI%/}/" | awk '{print $4}' | while read -r name; do
  day="${name#oxhouse-}"; day="${day%%T*}"
  case "$day" in [0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9]) ;; *) continue ;; esac
  if [ "$day" -lt "$CUTOFF" ]; then
    "${S3[@]}" rm "${BACKUP_S3_URI%/}/$name" --only-show-errors && echo "removed $name"
  fi
done
