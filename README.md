# OxHouse Research

An internal research tool for screening and diligence on small-cap healthcare companies.
Phase 1 builds the screener: a daily universe from Finviz, scores computed in-house, and a
screen with a company page.

Status: **Phase 1, Step 0** (project skeleton and CI). No business logic yet.

## Repository layout

```
backend/              Django application (API, pipeline, scoring)
  apps/               one Django app per area of responsibility
  config/             project settings, URLs, environment parsing
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

Only `platform` has code so far; the other apps are empty modules that later steps fill in.

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
