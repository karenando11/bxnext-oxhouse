"""Raw file storage: keeps every download unchanged, named by its content hash.

Phase 1 stores on local disk. An object store (R2 or B2) replaces ``LocalRawStorage`` behind the
same two methods; ``SourceCall.storage_uri`` records where a file went.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Protocol


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
