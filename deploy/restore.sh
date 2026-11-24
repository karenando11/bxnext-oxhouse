#!/usr/bin/env bash
# Restore a backup on a clean host. See deploy/README.md for the whole procedure.
#   deploy/restore.sh oxhouse-20261124T020000Z.dump      # downloads it from the bucket
#   deploy/restore.sh /path/to/file.dump                 # or uses a local file
# Refuses to run when the database already holds tables, unless RESTORE_FORCE=1.
set -euo pipefail
cd "$(dirname "$0")"
set -a; . ./.env; set +a
SRC="${1:?give a dump name from the bucket or a path}"

S3=(aws s3)
[ -n "${AWS_ENDPOINT_URL:-}" ] && S3=(aws --endpoint-url "$AWS_ENDPOINT_URL" s3)
if [ -f "$SRC" ]; then FILE="$SRC"; else
  FILE="$(mktemp -d)/$SRC"; "${S3[@]}" cp "${BACKUP_S3_URI%/}/$SRC" "$FILE" --only-show-errors
fi

C=(docker compose -f docker-compose.prod.yml)
"${C[@]}" up -d db
until "${C[@]}" exec -T db pg_isready -U "${POSTGRES_USER:-oxhouse}" >/dev/null 2>&1; do sleep 2; done

TABLES="$("${C[@]}" exec -T db psql -U "${POSTGRES_USER:-oxhouse}" -d "${POSTGRES_DB:-oxhouse}" -Atc \
  "select count(*) from information_schema.tables where table_schema='public'")"
if [ "$TABLES" != "0" ] && [ "${RESTORE_FORCE:-0}" != "1" ]; then
  echo "The database already has $TABLES tables. Set RESTORE_FORCE=1 to overwrite." >&2; exit 1
fi

"${C[@]}" exec -T db pg_restore -U "${POSTGRES_USER:-oxhouse}" -d "${POSTGRES_DB:-oxhouse}" \
  --clean --if-exists --no-owner < "$FILE"
"${C[@]}" up -d
"${C[@]}" run --rm app python manage.py migrate --noinput
echo "restored from $SRC"
