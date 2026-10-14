# OxHouse Research

An internal research tool for screening and diligence on small-cap healthcare companies.
This repository holds the Django application. Phase 1 builds the screener: a daily universe
from Finviz, scores computed in-house, and a screen with a company page.

Status: **Phase 1, Step 0** (project skeleton and CI). No business logic yet.

## Quick start

Requirements: [uv](https://docs.astral.sh/uv/), Docker with Compose, and `make`.

```bash
make bootstrap   # creates .env from .env.example, installs dependencies,
                 # starts PostgreSQL in Docker and applies migrations
make run         # starts the development server on http://localhost:8000
make check       # lint, type check, Django checks, migration check and tests
```

Health check: `curl http://localhost:8000/healthz` returns the database status and the
latest applied migration, or `503` when the database is unreachable.

## Configuration

Settings come from environment variables. The application refuses to start if a required
variable is missing and names the one that is.

| Variable | Required | Default | Purpose |
|---|---|---|---|
| `DJANGO_SECRET_KEY` | yes | none | Django secret key |
| `DATABASE_URL` | yes | none | `postgres://user:password@host:port/name` |
| `DJANGO_DEBUG` | no | `false` | `true` for local development |
| `DJANGO_ALLOWED_HOSTS` | no | `localhost,127.0.0.1` | Comma-separated host names |

Copy `.env.example` to `.env` for local work. `.env` is git-ignored. Never commit secrets.

## Layout

```
config/       project settings, URLs, environment parsing
apps/         one Django app per area of responsibility
  platform/   pipeline runs, source calls, data-quality checks, settings, health endpoint
  companies/  company identity, ticker history, sponsor aliases
  marketdata/ daily Finviz snapshots
  sec/        SEC filings, XBRL facts, runway
  trials/     ClinicalTrials.gov studies and catalyst signals
  scoring/    formula versions, score runs and results
  screener/   screen definitions, runs and the funnel log
  web/        pages: screener, company page, pipeline health
tests/        pytest suite
```

Only `apps/platform` has code so far; the other apps are empty modules that later steps fill in.

## Development

| Command | What it does |
|---|---|
| `make test` | Run the test suite (needs the database from `make up`) |
| `make lint` | Ruff lint and format check |
| `make format` | Apply Ruff fixes and formatting |
| `make typecheck` | mypy with the Django plugin |
| `make down` | Stop the database container |

To run the application itself in a container: `docker compose --profile app up --build`.

## Continuous integration

`.github/workflows/ci.yml` runs on every pull request and on pushes to `main`: lint, type
check, Django checks, a models-versus-migrations check, migrations against an empty
PostgreSQL 16, and the test suite. A pull request cannot merge unless these pass once branch
protection requiring the `test` job is enabled in the repository settings.
