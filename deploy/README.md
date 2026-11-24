# Deploying OxHouse (Step 19)

One small server runs everything: Caddy (HTTPS and the network allow-list), the Django API, and
PostgreSQL. The daily batch, the nightly backup and the disk check are systemd timers on the host.
The host, domain and bucket are the engineer's choice; nothing here depends on a provider.

## Files

| File | Purpose |
|---|---|
| `docker-compose.prod.yml` | The stack. Only Caddy publishes ports (80 and 443); the database is not reachable from outside |
| `Caddyfile` | HTTPS for `SITE_DOMAIN`; addresses outside `ALLOWED_NETWORKS` get 403 |
| `env.production.example` | Every setting; copy to `deploy/.env` on the server |
| `deploy.sh` | Build, migrate, restart (run by the deploy workflow over SSH) |
| `backup.sh`, `restore.sh` | Nightly dump to the bucket, kept 30 days; restore on a clean host |
| `systemd/*.timer`, `*.service` | Batch at 06:00 US Eastern Monday to Friday, backup at 02:00, disk check hourly |
| `../.github/workflows/deploy.yml` | Deploys `main` after CI passes |

## Set up a clean server

1. Install Docker (with the compose plugin), git and the aws CLI. Set the clock to UTC (the timers
   name their own time zone).
2. `git clone <repo> /srv/oxhouse`, then `cp deploy/env.production.example deploy/.env` and fill
   it in (secret key, database password, domain, allowed networks, SEC contact, Finviz token, mail,
   Sentry, bucket). Keep `deploy/.env` readable by the deploy user only. It is git-ignored.
3. Point the domain's DNS at the server. Open ports 80 and 443.
4. `deploy/deploy.sh`. Then create the first user:
   `docker compose -f deploy/docker-compose.prod.yml run --rm app python manage.py createsuperuser`.
5. Install the timers:
   `sudo cp deploy/systemd/* /etc/systemd/system/ && sudo systemctl daemon-reload`
   `sudo systemctl enable --now oxhouse-batch.timer oxhouse-backup.timer oxhouse-disk.timer`
   Check with `systemctl list-timers 'oxhouse-*'`.
6. In the repository settings add the deploy secrets named in `deploy.yml`.

## What runs when

* **Batch:** `oxhouse-batch.timer`, 06:00 US Eastern, Monday to Friday. The times and the market-holiday
  list are settings (`schedule.batch_start_et`, `market_holidays`); the timer itself fires at 06:00.
* **Backup:** `oxhouse-backup.timer`, 02:00 US Eastern, a custom-format `pg_dump` uploaded to
  `BACKUP_S3_URI`. Dumps older than `BACKUP_KEEP_DAYS` (30) are deleted. Also set a lifecycle rule
  on the bucket.
* **Disk:** `oxhouse-disk.timer`, hourly. Above 80 percent one alert is emailed; it clears below.
* **Errors:** with `SENTRY_DSN` set, unhandled errors in the API and the batch go to the tracker.

## Restore drill (on a clean host)

1. Do steps 1 to 3 above on the new host, using the same `deploy/.env`.
2. List the dumps: `aws s3 ls $BACKUP_S3_URI/`.
3. `deploy/restore.sh oxhouse-<stamp>.dump`. It starts the database, restores into it (refusing if
   the database already has tables, unless `RESTORE_FORCE=1`), runs migrations and starts the stack.
4. Check: sign in, open `/api/health` (last batch and run history are there) and
   `/api/company/<ticker>`; compare the row counts with the old host:
   `docker compose -f deploy/docker-compose.prod.yml exec db psql -U oxhouse -c "select count(*) from companies_company"`.
5. Write the date, the dump used, the time it took and the result in the drill log below.

| Date | Dump | Duration | Result | By |
|---|---|---|---|---|
| (none yet) | | | | |

## Access protection

Two layers: the network allow-list in Caddy (`ALLOWED_NETWORKS`; anything else gets 403 and never
reaches the application) and the API's own sign-in (401 without a session). To check from a
disallowed network: `curl -i https://<domain>/healthz` must answer 403.
