.PHONY: bootstrap up down migrate run test lint format typecheck check local-db-guard \
	docker-up docker-down docker-logs docker-shell docker-test docker-manage docker-user docker-prod

BACKEND := backend

# Defaults used by tooling that imports Django settings (mypy plugin, tests).
# A real backend/.env or exported variables always take precedence.
export DJANGO_SECRET_KEY ?= local-development-secret-key-not-for-production
export DATABASE_URL ?= postgres://oxhouse:oxhouse@localhost:5432/oxhouse

## Stops local commands that read backend/.env when it names a shared database (such as the Neon
## production branch). To use one on purpose: make migrate ALLOW_REMOTE=1
local-db-guard:
	@sh scripts/check-local-db.sh $(BACKEND)/.env

## One command to get a working development environment.
bootstrap: local-db-guard
	@test -f $(BACKEND)/.env || cp $(BACKEND)/.env.example $(BACKEND)/.env
	cd $(BACKEND) && uv sync
	docker compose up -d --wait db
	cd $(BACKEND) && uv run --env-file .env python manage.py migrate --noinput
	@echo "Ready. Run 'make run' to start the backend and 'make check' to run all checks."

up:
	docker compose up -d --wait db

down:
	docker compose down

migrate: local-db-guard
	cd $(BACKEND) && uv run --env-file .env python manage.py migrate --noinput

run: local-db-guard
	cd $(BACKEND) && uv run --env-file .env python manage.py runserver

test:
	cd $(BACKEND) && uv run pytest

lint:
	cd $(BACKEND) && uv run ruff check .
	cd $(BACKEND) && uv run ruff format --check .

format:
	cd $(BACKEND) && uv run ruff check --fix .
	cd $(BACKEND) && uv run ruff format .

typecheck:
	cd $(BACKEND) && uv run mypy .

## Everything the backend CI job runs, in the same order.
check: lint typecheck
	cd $(BACKEND) && uv run python manage.py check
	cd $(BACKEND) && uv run python manage.py makemigrations --check --dry-run
	cd $(BACKEND) && uv run pytest

## ---------------------------------------------------------------------------------------------
## Everything in Docker (no uv or Python needed on the host). The backend container always uses
## the database container, never the database named in backend/.env.

## Start the database and the backend (builds on first use): http://localhost:8000
docker-up:
	docker compose up -d --build --wait
	@echo "Backend: http://localhost:8000/healthz   Logs: make docker-logs   Stop: make docker-down"

## Stop the containers. Data is kept; `docker compose down -v` erases it.
docker-down:
	docker compose down

docker-logs:
	docker compose logs -f backend

docker-shell:
	docker compose exec backend sh

## The full test suite inside the container.
docker-test:
	docker compose run --rm backend pytest

## Run a manage.py command: make docker-manage CMD="score_day 2026-10-12"
docker-manage:
	docker compose exec backend python manage.py $(CMD)

## Create a sign-in for the API.
docker-user:
	docker compose exec backend python manage.py createsuperuser

## Build and start the production image on http://localhost:8001
docker-prod:
	docker compose --profile prod up -d --build --wait backend-prod
