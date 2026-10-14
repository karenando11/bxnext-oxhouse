.PHONY: bootstrap up down migrate run test lint format typecheck check

# Defaults used by tooling that imports Django settings (mypy plugin, tests).
# A real .env or exported variables always take precedence.
export DJANGO_SECRET_KEY ?= local-development-secret-key-not-for-production
export DATABASE_URL ?= postgres://oxhouse:oxhouse@localhost:5432/oxhouse

## One command to get a working development environment.
bootstrap:
	@test -f .env || cp .env.example .env
	uv sync
	docker compose up -d --wait db
	uv run --env-file .env python manage.py migrate --noinput
	@echo "Ready. Run 'make run' to start the server and 'make check' to run all checks."

up:
	docker compose up -d --wait db

down:
	docker compose down

migrate:
	uv run --env-file .env python manage.py migrate --noinput

run:
	uv run --env-file .env python manage.py runserver

test:
	uv run pytest

lint:
	uv run ruff check .
	uv run ruff format --check .

format:
	uv run ruff check --fix .
	uv run ruff format .

typecheck:
	uv run mypy .

## Everything CI runs, in the same order.
check: lint typecheck
	uv run python manage.py check
	uv run python manage.py makemigrations --check --dry-run
	uv run pytest
