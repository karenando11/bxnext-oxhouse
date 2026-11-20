"""Environment configuration parsing.

Pure Python with no Django imports, so it is trivial to unit test. Django's
settings module calls ``read_config`` once at start-up. Missing or malformed
values fail fast with a message that names the offending variable.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

REQUIRED_VARIABLES: tuple[str, ...] = ("DJANGO_SECRET_KEY", "DATABASE_URL")

DEFAULT_ALLOWED_HOSTS = "localhost,127.0.0.1"
_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off", ""}


class ConfigError(Exception):
    """Raised when the environment is missing or contains an invalid value."""


@dataclass(frozen=True)
class Config:
    secret_key: str
    debug: bool
    allowed_hosts: list[str]
    database: dict[str, Any]


def _require(env: Mapping[str, str], name: str) -> str:
    value = env.get(name, "").strip()
    if not value:
        raise ConfigError(f"Missing required environment variable {name}. See .env.example.")
    return value


def parse_bool(name: str, raw: str) -> bool:
    value = raw.strip().lower()
    if value in _TRUE:
        return True
    if value in _FALSE:
        return False
    raise ConfigError(f"Environment variable {name} must be true or false, got {raw!r}.")


# Connection options a database URL may carry (for example Neon's ?sslmode=require). Anything else
# is rejected so a typo cannot be silently ignored.
ALLOWED_URL_OPTIONS = frozenset(
    {"sslmode", "sslrootcert", "channel_binding", "connect_timeout", "application_name"}
)
LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})
# `manage.py` commands that serve web traffic and so stay on the pooled connection.
WEB_COMMANDS = frozenset({"runserver"})


def is_pooled(host: str, params: Mapping[str, str]) -> bool:
    """True for a connection pooler in transaction mode (Neon's ``-pooler`` host, PgBouncer)."""
    return "-pooler" in host or params.get("pgbouncer", "").lower() == "true"


def is_local_host(host: str) -> bool:
    return host in LOCAL_HOSTS


def parse_database_url(url: str, variable: str = "DATABASE_URL") -> dict[str, Any]:
    parsed = urlparse(url)
    if parsed.scheme not in {"postgres", "postgresql"}:
        raise ConfigError(f"{variable} must start with postgres:// or postgresql://.")
    name = parsed.path.lstrip("/")
    if not parsed.hostname or not name:
        raise ConfigError(f"{variable} must include a host and a database name.")
    params = {k: v[-1] for k, v in parse_qs(parsed.query, keep_blank_values=True).items()}
    pooled = is_pooled(parsed.hostname, params)
    params.pop("pgbouncer", None)
    unknown = sorted(set(params) - ALLOWED_URL_OPTIONS)
    if unknown:
        raise ConfigError(f"{variable} has an unsupported option: {unknown[0]}.")
    options: dict[str, Any] = dict(params)
    config: dict[str, Any] = {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": name,
        "USER": unquote(parsed.username or ""),
        "PASSWORD": unquote(parsed.password or ""),
        "HOST": parsed.hostname,
        "PORT": str(parsed.port or 5432),
        "CONN_MAX_AGE": 60,
        "CONN_HEALTH_CHECKS": True,
    }
    if pooled:
        # A transaction pooler hands each statement to any server connection, so server-side
        # cursors and prepared statements cannot be relied on.
        config["DISABLE_SERVER_SIDE_CURSORS"] = True
        options["prepare_threshold"] = None
    if options:
        config["OPTIONS"] = options
    return config


def select_database_url(
    env: Mapping[str, str], *, management_command: str | None = None
) -> tuple[str, str]:
    """The URL to use and the variable it came from.

    The app serves requests on ``DATABASE_URL`` (the pooled address, for a managed database).
    Migrations and batch jobs run as ``manage.py`` commands and use ``DATABASE_DIRECT_URL`` when
    it is set, because a pooler can interfere with session features. ``runserver`` stays pooled.
    """
    direct = env.get("DATABASE_DIRECT_URL", "").strip()
    if direct and management_command and management_command not in WEB_COMMANDS:
        return direct, "DATABASE_DIRECT_URL"
    return _require(env, "DATABASE_URL"), "DATABASE_URL"


def read_config(env: Mapping[str, str], *, management_command: str | None = None) -> Config:
    secret_key = _require(env, "DJANGO_SECRET_KEY")
    _require(env, "DATABASE_URL")
    url, variable = select_database_url(env, management_command=management_command)
    database = parse_database_url(url, variable)
    debug = parse_bool("DJANGO_DEBUG", env.get("DJANGO_DEBUG", "false"))
    hosts_raw = env.get("DJANGO_ALLOWED_HOSTS", DEFAULT_ALLOWED_HOSTS)
    allowed_hosts = [host.strip() for host in hosts_raw.split(",") if host.strip()]
    return Config(
        secret_key=secret_key,
        debug=debug,
        allowed_hosts=allowed_hosts,
        database=database,
    )
