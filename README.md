# OxHouse Research

An internal research tool for screening and diligence on small-cap healthcare companies.
Phase 1 builds the screener: a daily universe from Finviz, scores computed in-house, and a
screen with a company page.

Status: **Phase 1, Steps 0 to 10** built. Step 9 is backend only (the screener page is the React frontend's job). Step 10 (Finviz ingestion) is tested on synthetic exports only: the first real download, with `FINVIZ_AUTH_TOKEN`, is still to do. Next: Step 11, SEC collectors.

## Repository layout

```
backend/              Django application (API, pipeline, scoring)
  apps/               one Django app per area of responsibility
  config/             project settings, URLs, environment parsing
  scoring_engine/     pure-Python scoring: no Django, database, network or clock
  tests/              pytest suite
  Dockerfile
  pyproject.toml      dependencies and tool configuration (uv)
frontend/             React application (not created yet)
docker-compose.yml    local PostgreSQL and the backend (development image, auto-reload)
Makefile              developer commands, run from the repository root
.github/workflows/    CI, one job per component
```

Each component owns its own dependencies, Dockerfile and tests. The root holds only what
spans components: orchestration, CI and documentation.

## Quick start

### Easiest: everything in Docker

Requirements: Docker with Compose, and `make`. Nothing else (no Python or uv).

```bash
make docker-up      # builds on first use, then starts PostgreSQL and the backend
make docker-user    # create a sign-in for the API (asks for a username and password)
make docker-test    # the full test suite inside the container
make docker-down    # stop (data is kept)
```

The backend is on http://localhost:8000 and reloads when you edit the code. It applies migrations
when it starts. Handy extras:

| Command | What it does |
|---|---|
| `make docker-manage CMD="score_day 2026-10-12"` | Run any `manage.py` command in the container |
| `make docker-logs` | Follow the backend log |
| `make docker-shell` | A shell inside the backend container |
| `make docker-prod` | Build and start the production image on http://localhost:8001 |

The backend container **always uses the database container**, never the database named in
`backend/.env`, and runs with a development secret key, so production credentials in that file are
never used inside Docker. Other values in `backend/.env` (such as `FINVIZ_AUTH_TOKEN`) are passed
through. Raw downloads are kept in a Docker volume. To erase the local data:
`docker compose down -v`.

### Or run Python on your machine

Requirements: [uv](https://docs.astral.sh/uv/), Docker with Compose, and `make`.

```bash
make bootstrap   # creates backend/.env from backend/.env.example, installs dependencies,
                 # starts PostgreSQL in Docker and applies migrations
make run         # starts the backend on http://localhost:8000
make check       # lint, type check, Django checks, migration check and tests
```

`make bootstrap`, `make migrate` and `make run` read `backend/.env`. If that file names a database
that is not on this machine (for example Neon production), they refuse to run and tell you so. To
do it on purpose: `make migrate ALLOW_REMOTE=1`.

Health check: `curl http://localhost:8000/healthz` returns the database status and the
latest applied migration, or `503` when the database is unreachable.

## Local database and testing

**Use PostgreSQL locally, not SQLite.** The data model relies on PostgreSQL features: database views
and SQL functions, a trigger that freezes used formula versions, array and JSONB columns, partial
unique indexes and check constraints. The migrations do not run on SQLite, and tests that passed on
it would say little about production.

### Three databases, kept apart

| Database | Used for | How you get it |
|---|---|---|
| Development | Clicking around, loading fixture data, trying the API | `make docker-up` starts a throwaway PostgreSQL 16 container (or `make bootstrap` if you run Python on your machine) |
| Tests | The pytest suite | Created and dropped by pytest on the same server (`test_oxhouse`); nothing to set up |
| Shared or production (for example Neon) | The real thing | Never used for local work |

### Set up and fill a development database

```bash
make docker-up                      # PostgreSQL and the backend; migrations apply on start
make docker-user                    # create a sign-in for the API
make docker-manage CMD="load_golden tests/fixtures/private/golden_export_2026-10-12.csv --runway tests/fixtures/private/edgar_runway_export_2026-10-14.csv"
make docker-manage CMD="score_day 2026-10-12"
make docker-manage CMD="screen_day 2026-10-12"
```

The two CSV files are licensed data that stay out of the repository: put them in
`backend/tests/fixtures/private/` (git-ignored), which the container sees as `/app/tests/fixtures/private/`.
The development database then holds the golden day as fixture data (`/healthz` reports
`data_source: fixture`), scored and screened, ready for the API.

To start again from nothing: `docker compose down -v`, then `make docker-up`. Loading and migrating
are repeatable, so this is cheap.

### Running the tests

```bash
make docker-test        # inside the container, against the container's database (simplest)
make test               # on your machine, against the PostgreSQL that `make up` started
```

Tests marked `golden` need the private files and are skipped, with a message, when they are absent.

### Staying away from a shared database

- `make bootstrap`, `make migrate` and `make run` refuse to run when `backend/.env` names a database
  that is not on this machine. Override on purpose with `ALLOW_REMOTE=1`.
- The test suite and `load_golden` refuse a remote database as well (`OXHOUSE_ALLOW_REMOTE_TEST_DB=1`
  and `--allow-remote` override them).
- **A `DATABASE_URL` set in your shell wins over `backend/.env`.** If a command might hit the wrong
  database, check `echo $DATABASE_URL` first and unset it. The Docker commands ignore both and always
  use the container database.

If several people need one shared development database (for example while building the frontend), a
Neon branch is a good fit: it is a copy-on-write fork that you can reset. For solo work, Docker is simpler.

## Backend configuration

Settings come from environment variables. The backend refuses to start if a required
variable is missing and names the one that is.

| Variable | Required | Default | Purpose |
|---|---|---|---|
| `DJANGO_SECRET_KEY` | yes | none | Django secret key |
| `DATABASE_URL` | yes | none | `postgres://user:password@host:port/name` |
| `DJANGO_DEBUG` | no | `false` | `true` for local development |
| `DJANGO_ALLOWED_HOSTS` | no | `localhost,127.0.0.1` | Comma-separated host names |

Copy `backend/.env.example` to `backend/.env` for local work. `.env` files are git-ignored.
Never commit secrets.

## Backend apps

| App | Responsibility |
|---|---|
| `platform` | pipeline runs, source calls, data-quality checks, settings, health endpoint |
| `companies` | company identity, ticker history, sponsor aliases |
| `marketdata` | daily Finviz snapshots |
| `sec` | SEC filings, XBRL facts, runway |
| `trials` | ClinicalTrials.gov studies and catalyst signals |
| `scoring` | formula versions, score runs and results |
| `screener` | screen definitions, runs and the funnel log |
| `web` | HTTP views for the screener, company page and pipeline health |

Every app except `web` owns its tables (23 in total, listed in `docs/architecture-and-data-model.md`).
`web` holds the `company_page` read model, a database view the API reads instead of joining base
tables. The metric views `short_volume_metric` and `insider_metric_daily` are views too, backed by
SQL functions that take a market date. Reason codes are a fixed list in
`apps/platform/reasons.py`; the database rejects unknown codes. A formula version that a score run
has used cannot be changed (a database trigger enforces it).

## Scoring engine

`backend/scoring_engine/` computes the scores from plain data and has no infrastructure
dependencies (a test enforces this). It provides all the scores and the Composite, driven by a versioned `FormulaVersion` (every constant plus the parity quirk switches; `score_universe` scores a whole universe under one version):

| Score | Meaning |
|---|---|
| Outlier | how unusual a company is against its peers, in either direction (0 to 10) |
| Divergence | which direction that unusualness points (signed) |
| Recency | how fresh it is: 1.0, 0.75, 0.5 or 0.3 |
| Cash Health | balance-sheet strength, capped by runway and dampened by how far off the next catalyst is (0 to 10) |
| Milestone | how close and how credible the next catalyst is, blended with earnings and momentum (0 to 10) |
| Composite | the ranking score: a weighted blend of the five scores above (0 to 10) |

Peers are companies with the same Finviz industry and market cap bucket (Nano, Micro, Small,
Mid) within the healthcare universe. Pass the **whole** universe to `compute_topology`; peer
statistics depend on every member. `round_half_up` and `display_score` reproduce the display
rounding of the existing system. Constants live in `TopologyConfig`, and its docstring records
which ones the golden data pins down and which come from documentation only.

### Loading the golden day as fixture data

```bash
cd backend
python manage.py load_golden tests/fixtures/private/golden_export_2026-10-12.csv \
    --runway tests/fixtures/private/edgar_runway_export_2026-10-14.csv
```

This loads the companies, tickers, Finviz snapshots, runway rows and catalyst signals for the golden
day, then overlays cash, burn, burn source and notes from the runway export. It is safe to run
again. Every row traces to a `fixture` source call, and `/healthz` reports `data_source: fixture`
while the latest run is a fixture run. It is meant for development: point `DATABASE_URL` at a
local or throwaway database, never at a shared one.

### Scoring a stored day

```bash
python manage.py score_day 2026-10-12
```

This scores every company with a Finviz snapshot on that day under the latest `parity` formula
version and stores the scores with the exact inputs and every component. A later run for the same
day supersedes the earlier one, which is kept. A failed run leaves no partial rows and is recorded
with its error.

### Screening a scored day

```bash
python manage.py screen_day 2026-10-12
```

This stores the default screen for the latest completed score run of that day (every company, with
every filter it failed) and writes the funnel events against the previous day. Ad-hoc screens with
any filters are computed live from the stored scores by `apps.screener.service.adhoc_screen`.

### API

The backend serves JSON under `/api/` for the React frontend: sign-in, the screener table with live
filters, why-excluded, near-the-line, the funnel log, saved screens and a CSV export. See
`docs/api.md`.

### Databases: local, development and production

| Use | Database | Notes |
|---|---|---|
| Tests, loaders, experiments | The Docker Postgres from `make up`, or a Neon `development` branch | `load_golden` and the test suite write real rows |
| The running app and nightly jobs | The Neon `production` branch | Never run tests or `load_golden` here |

The test suite refuses to start when `DATABASE_URL` points at a remote host (set
`OXHOUSE_ALLOW_REMOTE_TEST_DB=1` to override on a throwaway branch), and `load_golden` needs
`--allow-remote`.

**Neon.** Each branch has two addresses. Put them in your own `backend/.env` (git-ignored); never
commit or paste them:

- `DATABASE_URL`: the **pooled** address (host contains `-pooler`), used by the web app. The
  settings detect it and turn off server-side cursors and prepared statements, which a transaction
  pooler cannot support. Keep `?sslmode=require` on the URL; `channel_binding` is passed through.
- `DATABASE_DIRECT_URL`: the **direct** address. Every `python manage.py <command>` except
  `runserver` uses it when set, so migrations and the batch jobs bypass the pooler.

Use a paid Neon tier with point-in-time recovery and scale-to-zero turned off for production.

### Finviz ingestion

```bash
export FINVIZ_AUTH_TOKEN=...   # never commit or paste it; it is never logged
python manage.py finviz_contract_check --save tests/fixtures/private/finviz_raw.csv   # one real download, no database writes
python manage.py ingest_finviz                 # download today's export and load the snapshot
python manage.py ingest_finviz --file path.csv --date 2026-10-12   # load a saved raw file
```

The download is stored unchanged under `RAW_STORAGE_DIR` and recorded in `source_call`. A blank
column, a large row-count change, a changed universe definition and similar problems are recorded
as `data_quality_check` rows. Loading the same file for the same day twice changes nothing.

### Golden fixtures

Regression tests compare against a day of stored scores (1,053 companies). That file holds
licensed Finviz-derived data, so it is **not** committed. Place
`golden_export_2026-10-12.csv` (and `edgar_runway_export_2026-10-14.csv` for the loader tests) in
`backend/tests/fixtures/private/` (git-ignored) or set `OXHOUSE_GOLDEN_EXPORT` and
`OXHOUSE_RUNWAY_EXPORT` to their paths. Without it, the tests marked `golden` are skipped with a
message saying why; with it they must all pass. CI currently skips them until the file is made
available to it through private storage.

## Development commands

All run from the repository root.

| Command | What it does |
|---|---|
| `make test` | Run the backend test suite (needs the database from `make up`) |
| `make lint` | Ruff lint and format check |
| `make format` | Apply Ruff fixes and formatting |
| `make typecheck` | mypy with the Django plugin |
| `make down` | Stop the database container |

The Docker commands (`make docker-up`, `docker-test`, `docker-manage` and the rest) are described in
the quick start.

## Adding the React frontend later

Create `frontend/` beside `backend/`. The pieces that are already in place for it:

- `docker-compose.yml` has a marked place for a `frontend` service.
- CI has one job per component; add a `frontend` job beside `backend`.
- `.gitignore` already covers `node_modules/` and build output.

Decisions to make at that point, none of which need changing in the repository layout:
how the frontend reaches the API (a dev-server proxy avoids CORS locally), how users sign in
(session cookie with CSRF protection versus tokens), and which URL prefix the API uses.

## Continuous integration

`.github/workflows/ci.yml` runs on every pull request and on pushes to `main`. The `backend`
job runs lint, type check, Django checks, a models-versus-migrations check, migrations
against an empty PostgreSQL 16, and the test suite. A pull request cannot merge unless the
job passes once branch protection requiring `backend` is enabled in the repository settings.
