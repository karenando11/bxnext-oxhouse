"""Guards that keep destructive or fixture-writing work away from a shared database."""

from __future__ import annotations

import os

from config.envconfig import is_local_host

ALLOW_REMOTE_TESTS_VAR = "OXHOUSE_ALLOW_REMOTE_TEST_DB"
# Extra host names that are known to be a local database (for example the Compose service "db").
LOCAL_HOSTS_VAR = "OXHOUSE_LOCAL_DB_HOSTS"


def extra_local_hosts() -> set[str]:
    return {h.strip() for h in os.environ.get(LOCAL_HOSTS_VAR, "").split(",") if h.strip()}


class RemoteDatabaseRefused(RuntimeError):
    pass


def ensure_local_database(host: str, what: str, *, allowed: bool = False) -> None:
    """Raise unless the database is on this machine (or the caller said it is allowed)."""
    if allowed or is_local_host(host) or host in extra_local_hosts():
        return
    raise RemoteDatabaseRefused(
        f"Refusing to {what} on the remote database host {host!r}. "
        "Point DATABASE_URL at a local or throwaway database, or confirm you mean it."
    )
