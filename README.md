# OxHouse Research

An internal research tool for screening and diligence on small-cap healthcare companies.
Phase 1 builds the screener: a daily universe from Finviz, scores computed in-house, and a
screen with a company page.

Status: **Phase 1, Step 8** done (scores, versioned formula configuration, the 23-table data model with its read views, the golden-day fixture loader, the score run service, and the screener service). Next: Step 9, screener UI.

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
docker-compose.yml    local PostgreSQL, plus the backend container on demand
Makefile              developer commands, run from the repository root
.github/workflows/    CI, one job per component
```

Each component owns its own dependencies, Dockerfile and tests. The root holds only what
spans components: orchestration, CI and documentation.

## Quick start

Requirements: [uv](https://docs.astral.sh/uv/), Docker with Compose, and `make`.

```bash
make bootstrap   # creates backend/.env from backend/.env.example, installs dependencies,
                 # starts PostgreSQL in Docker and applies migrations
make run         # starts the backend on http://localhost:8000
make check       # lint, type check, Django checks, migration check and tests
```

Health check: `curl http://localhost:8000/healthz` returns the database status and the
latest applied migration, or `503` when the database is unreachable.

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

To run the backend itself in a container: `docker compose --profile app up --build`.

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
