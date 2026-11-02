"""Artifact store interface. The database stores metadata and a relative storage path;
bytes live in the store (filesystem by default, see
:mod:`agenttape.infrastructure.artifact_store`)."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Protocol


@dataclass(frozen=True)
class StoredObject:
    storage_path: str
    size_bytes: int
    sha256: str


class ArtifactTooLargeError(Exception):
    def __init__(self, size: int, limit: int) -> None:
        super().__init__(f"artifact is {size} bytes; the limit is {limit} bytes")
        self.size = size
        self.limit = limit


class ArtifactStore(Protocol):
    root: Path

    def put_bytes(self, key: str, data: bytes) -> StoredObject:
        """Atomically write ``data`` under relative ``key`` (temp file + rename)."""
        ...

    def put_stream(self, key: str, stream: BinaryIO) -> StoredObject: ...

    def resolve(self, storage_path: str) -> Path:
        """Absolute path for ``storage_path``; refuses traversal outside the store root."""
        ...

    def open(self, storage_path: str) -> BinaryIO: ...

    def iter_bytes(self, storage_path: str, chunk_size: int = 64 * 1024) -> Iterator[bytes]: ...

    def delete(self, storage_path: str) -> bool: ...
