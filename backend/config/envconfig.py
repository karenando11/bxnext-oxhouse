"""Environment configuration parsing.

Pure Python with no Django imports, so it is trivial to unit test. Django's
settings module calls ``read_config`` once at start-up. Missing or malformed
values fail fast with a message that names the offending variable.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import unquote, urlparse

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


def parse_database_url(url: str) -> dict[str, Any]:
    parsed = urlparse(url)
    if parsed.scheme not in {"postgres", "postgresql"}:
        raise ConfigError("DATABASE_URL must start with postgres:// or postgresql://.")
    name = parsed.path.lstrip("/")
    if not parsed.hostname or not name:
        raise ConfigError("DATABASE_URL must include a host and a database name.")
    return {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": name,
        "USER": unquote(parsed.username or ""),
        "PASSWORD": unquote(parsed.password or ""),
        "HOST": parsed.hostname,
        "PORT": str(parsed.port or 5432),
        "CONN_MAX_AGE": 60,
        "CONN_HEALTH_CHECKS": True,
    }


def read_config(env: Mapping[str, str]) -> Config:
    secret_key = _require(env, "DJANGO_SECRET_KEY")
    database = parse_database_url(_require(env, "DATABASE_URL"))
    debug = parse_bool("DJANGO_DEBUG", env.get("DJANGO_DEBUG", "false"))
    hosts_raw = env.get("DJANGO_ALLOWED_HOSTS", DEFAULT_ALLOWED_HOSTS)
    allowed_hosts = [host.strip() for host in hosts_raw.split(",") if host.strip()]
    return Config(
        secret_key=secret_key,
        debug=debug,
        allowed_hosts=allowed_hosts,
        database=database,
    )
