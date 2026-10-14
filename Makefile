.PHONY: bootstrap up down migrate run test lint format typecheck check

BACKEND := backend

# Defaults used by tooling that imports Django settings (mypy plugin, tests).
# A real backend/.env or exported variables always take precedence.
export DJANGO_SECRET_KEY ?= local-development-secret-key-not-for-production
export DATABASE_URL ?= postgres://oxhouse:oxhouse@localhost:5432/oxhouse

## One command to get a working development environment.
bootstrap:
	@test -f $(BACKEND)/.env || cp $(BACKEND)/.env.example $(BACKEND)/.env
	cd $(BACKEND) && uv sync
	docker compose up -d --wait db
	cd $(BACKEND) && uv run --env-file .env python manage.py migrate --noinput
	@echo "Ready. Run 'make run' to start the backend and 'make check' to run all checks."

up:
	docker compose up -d --wait db

down:
	docker compose down

migrate:
	cd $(BACKEND) && uv run --env-file .env python manage.py migrate --noinput

run:
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
