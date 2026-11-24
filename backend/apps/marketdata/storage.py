"""Raw file storage: keeps every download unchanged, named by its content hash.

Development stores on local disk (``LocalRawStorage``). Production keeps no raw files
(``NullRawStorage``): only the parsed rows and each download's hash and size are stored. The
backend is chosen by the ``RAW_STORAGE_BACKEND`` setting (see ``get_raw_storage``).
``SourceCall.storage_uri`` records where a file went, and is empty when nothing was kept.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Protocol

from django.conf import settings


class RawStorage(Protocol):
    def put(self, key: str, content: bytes) -> str:
        """Store ``content`` under ``key`` and return its URI."""

    def get(self, uri: str) -> bytes: ...


def sha256_hex(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


class LocalRawStorage:
    def __init__(self, root: Path) -> None:
        self.root = root

    def put(self, key: str, content: bytes) -> str:
        path = (self.root / key).resolve()
        if self.root.resolve() not in path.parents:
            raise ValueError(f"Storage key escapes the storage root: {key}")
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():  # identical content is stored once and never rewritten
            path.write_bytes(content)
        return path.as_uri()

    def get(self, uri: str) -> bytes:
        return Path(uri.removeprefix("file://")).read_bytes()


class NullRawStorage:
    """Keeps nothing. ``put`` returns an empty URI, so the source call records no location."""

    def put(self, key: str, content: bytes) -> str:
        return ""

    def get(self, uri: str) -> bytes:
        raise FileNotFoundError("Raw files are not kept in this environment.")


def get_raw_storage() -> RawStorage:
    """The storage the settings ask for: local disk in development, nothing in production."""
    if settings.RAW_STORAGE_BACKEND == "local":
        return LocalRawStorage(settings.RAW_STORAGE_DIR)
    return NullRawStorage()
