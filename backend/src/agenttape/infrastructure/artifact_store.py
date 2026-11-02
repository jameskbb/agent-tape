"""Filesystem artifact store: atomic writes, SHA-256, size caps, traversal-safe reads."""

from __future__ import annotations

import hashlib
import io
import os
import tempfile
from collections.abc import Iterator
from pathlib import Path
from typing import BinaryIO

from agenttape.artifacts.store import ArtifactTooLargeError, StoredObject
from agenttape.security.paths import safe_join

_CHUNK = 64 * 1024


class FilesystemArtifactStore:
    def __init__(self, root: Path, max_bytes: int) -> None:
        self.root = root
        self.max_bytes = max_bytes
        self.root.mkdir(parents=True, exist_ok=True)

    def put_bytes(self, key: str, data: bytes) -> StoredObject:
        return self.put_stream(key, io.BytesIO(data))

    def put_stream(self, key: str, stream: BinaryIO) -> StoredObject:
        target = safe_join(self.root, key)
        target.parent.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha256()
        size = 0
        fd, tmp_name = tempfile.mkstemp(prefix=".tmp-", dir=target.parent)
        tmp = Path(tmp_name)
        try:
            with os.fdopen(fd, "wb") as out:
                while chunk := stream.read(_CHUNK):
                    size += len(chunk)
                    if size > self.max_bytes:
                        raise ArtifactTooLargeError(size, self.max_bytes)
                    digest.update(chunk)
                    out.write(chunk)
                out.flush()
                os.fsync(out.fileno())
            tmp.replace(target)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
        rel = target.relative_to(self.root.resolve()).as_posix()
        return StoredObject(storage_path=rel, size_bytes=size, sha256=digest.hexdigest())

    def resolve(self, storage_path: str) -> Path:
        return safe_join(self.root, storage_path)

    def open(self, storage_path: str) -> BinaryIO:
        return self.resolve(storage_path).open("rb")

    def iter_bytes(self, storage_path: str, chunk_size: int = _CHUNK) -> Iterator[bytes]:
        with self.open(storage_path) as fh:
            while chunk := fh.read(chunk_size):
                yield chunk

    def delete(self, storage_path: str) -> bool:
        path = self.resolve(storage_path)
        if path.is_file():
            path.unlink()
            return True
        return False
