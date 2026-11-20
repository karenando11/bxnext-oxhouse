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


# ------------------------------------------------------------- managed database (Neon)

NEON = "postgresql://owner:pw@ep-cool-name-pooler.c-11.us-east-1.aws.neon.tech/neondb"
NEON_DIRECT = "postgresql://owner:pw@ep-cool-name.c-11.us-east-1.aws.neon.tech/neondb"


def test_url_options_are_passed_to_the_driver() -> None:
    database = parse_database_url(f"{NEON_DIRECT}?sslmode=require&channel_binding=require")
    assert database["OPTIONS"] == {"sslmode": "require", "channel_binding": "require"}
    assert "DISABLE_SERVER_SIDE_CURSORS" not in database


def test_a_plain_local_url_has_no_options() -> None:
    assert "OPTIONS" not in parse_database_url("postgres://u:p@localhost:5432/db")


def test_a_pooled_host_disables_server_side_cursors_and_prepared_statements() -> None:
    database = parse_database_url(f"{NEON}?sslmode=require")
    assert database["DISABLE_SERVER_SIDE_CURSORS"] is True
    assert database["OPTIONS"] == {"sslmode": "require", "prepare_threshold": None}


def test_the_pgbouncer_flag_also_marks_a_pooled_connection() -> None:
    database = parse_database_url("postgres://u:p@db.example/db?pgbouncer=true&sslmode=require")
    assert database["DISABLE_SERVER_SIDE_CURSORS"] is True
    assert "pgbouncer" not in database["OPTIONS"]


def test_an_unsupported_url_option_is_rejected_by_name() -> None:
    with pytest.raises(ConfigError, match="unsupported option: sslmod"):
        parse_database_url("postgres://u:p@db.example/db?sslmod=require")


def test_errors_name_the_variable_the_url_came_from() -> None:
    with pytest.raises(ConfigError, match="DATABASE_DIRECT_URL must start"):
        parse_database_url("mysql://u:p@h/db", "DATABASE_DIRECT_URL")


def test_the_direct_url_is_used_by_management_commands_only() -> None:
    env = {**VALID_ENV, "DATABASE_URL": NEON, "DATABASE_DIRECT_URL": NEON_DIRECT}
    assert read_config(env).database["HOST"].endswith("neon.tech")
    assert "-pooler" in read_config(env).database["HOST"]
    assert "-pooler" not in read_config(env, management_command="migrate").database["HOST"]
    assert "-pooler" in read_config(env, management_command="runserver").database["HOST"]


def test_without_a_direct_url_everything_uses_the_main_one() -> None:
    env = {**VALID_ENV, "DATABASE_URL": NEON}
    assert "-pooler" in read_config(env, management_command="migrate").database["HOST"]
    assert (
        "-pooler"
        in read_config({**env, "DATABASE_DIRECT_URL": "  "}, management_command="migrate").database[
            "HOST"
        ]
    )


def test_a_bad_direct_url_fails_only_when_it_is_used() -> None:
    env = {**VALID_ENV, "DATABASE_DIRECT_URL": "not a url"}
    assert read_config(env).database["NAME"] == "oxhouse"
    with pytest.raises(ConfigError, match="DATABASE_DIRECT_URL"):
        read_config(env, management_command="migrate")
