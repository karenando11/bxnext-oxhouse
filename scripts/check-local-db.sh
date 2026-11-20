#!/bin/sh
# Refuse to continue when backend/.env points at a database that is not on this machine (for
# example the Neon production branch). Prints no values from the file.
#
# Override on purpose with:  ALLOW_REMOTE=1 make migrate
set -eu

env_file="${1:-backend/.env}"

if [ -n "${ALLOW_REMOTE:-}" ] || [ ! -f "$env_file" ]; then
  exit 0
fi

for var in DATABASE_URL DATABASE_DIRECT_URL; do
  url=$(grep -E "^${var}=" "$env_file" | head -1 | cut -d= -f2- || true)
  case "$url" in
    '' | *@localhost[:/]* | *@127.0.0.1[:/]*) ;;
    *)
      echo "$env_file sets $var to a database that is not on this machine." >&2
      echo "Refusing to continue. For local work use the Docker database (make docker-up)," >&2
      echo "or run this command with ALLOW_REMOTE=1 if you mean it." >&2
      exit 1
      ;;
  esac
done
