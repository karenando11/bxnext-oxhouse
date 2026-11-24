#!/usr/bin/env bash
# Deploy the current checkout: build the image, migrate, restart. Run on the server (the deploy
# workflow calls it over SSH):  deploy/deploy.sh
set -euo pipefail
cd "$(dirname "$0")"
test -f .env || { echo "deploy/.env is missing (see env.production.example)" >&2; exit 1; }
C=(docker compose -f docker-compose.prod.yml)
"${C[@]}" build app
"${C[@]}" up -d db
"${C[@]}" run --rm app python manage.py migrate --noinput
"${C[@]}" up -d
"${C[@]}" ps
