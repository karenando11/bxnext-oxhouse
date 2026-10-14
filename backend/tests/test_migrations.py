"""T0.2 (integration): migrations apply cleanly and models match them."""

from __future__ import annotations

import pytest
from django.core.management import call_command
from django.db import connection
from django.db.migrations.executor import MigrationExecutor


@pytest.mark.django_db
def test_all_migrations_are_applied_to_the_test_database() -> None:
    # pytest-django builds the test database from an empty one by running every migration.
    executor = MigrationExecutor(connection)
    pending = executor.migration_plan(executor.loader.graph.leaf_nodes())
    assert pending == []


@pytest.mark.django_db
def test_models_have_no_pending_migrations() -> None:
    call_command("makemigrations", "--check", "--dry-run")
