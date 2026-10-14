"""T0.4 (unit): configuration is read from the environment and fails fast."""

from __future__ import annotations

import pytest

from config.envconfig import (
    REQUIRED_VARIABLES,
    ConfigError,
    parse_bool,
    parse_database_url,
    read_config,
)

VALID_ENV = {
    "DJANGO_SECRET_KEY": "a-long-enough-secret-for-tests",
    "DATABASE_URL": "postgres://user:pa%40ss@db.example:6543/oxhouse",
}


@pytest.mark.parametrize("missing", REQUIRED_VARIABLES)
def test_missing_required_variable_is_named_in_the_error(missing: str) -> None:
    env = {k: v for k, v in VALID_ENV.items() if k != missing}
    with pytest.raises(ConfigError, match=missing):
        read_config(env)


@pytest.mark.parametrize("blank", REQUIRED_VARIABLES)
def test_blank_required_variable_counts_as_missing(blank: str) -> None:
    env = {**VALID_ENV, blank: "   "}
    with pytest.raises(ConfigError, match=blank):
        read_config(env)


def test_defaults_for_optional_variables() -> None:
    config = read_config(VALID_ENV)
    assert config.debug is False
    assert config.allowed_hosts == ["localhost", "127.0.0.1"]


def test_optional_variables_are_parsed() -> None:
    env = {**VALID_ENV, "DJANGO_DEBUG": "TRUE", "DJANGO_ALLOWED_HOSTS": " a.example , b.example ,"}
    config = read_config(env)
    assert config.debug is True
    assert config.allowed_hosts == ["a.example", "b.example"]


def test_database_url_is_decoded() -> None:
    database = parse_database_url(VALID_ENV["DATABASE_URL"])
    assert database["ENGINE"] == "django.db.backends.postgresql"
    assert database["NAME"] == "oxhouse"
    assert database["USER"] == "user"
    assert database["PASSWORD"] == "pa@ss"
    assert database["HOST"] == "db.example"
    assert database["PORT"] == "6543"


def test_database_url_defaults_the_port() -> None:
    assert parse_database_url("postgresql://u:p@localhost/db")["PORT"] == "5432"


@pytest.mark.parametrize(
    "url",
    ["mysql://u:p@localhost/db", "postgres://u:p@localhost", "postgres:///db", "not a url"],
)
def test_invalid_database_urls_are_rejected(url: str) -> None:
    with pytest.raises(ConfigError):
        parse_database_url(url)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("true", True), ("1", True), ("yes", True), ("false", False), ("0", False), ("", False)],
)
def test_parse_bool(raw: str, expected: bool) -> None:
    assert parse_bool("DJANGO_DEBUG", raw) is expected


def test_parse_bool_rejects_garbage_and_names_the_variable() -> None:
    with pytest.raises(ConfigError, match="DJANGO_DEBUG"):
        parse_bool("DJANGO_DEBUG", "maybe")
