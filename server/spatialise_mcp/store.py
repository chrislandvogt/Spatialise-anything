"""Where sign-in state lives: registered clients, pending sign-ins, issued
tokens, and each user's ArcGIS grant.

Two stores share one interface. ``MemoryStore`` forgets everything on
restart, which means every user signs in again. ``SqliteStore`` survives a
restart; every value is encrypted with AES-256-GCM under a key derived from
SPATIALISE_SECRET, so the file alone gives up no token.

The server's own tokens are never stored at all - only their SHA-256 - so a
copy of the store cannot be replayed against the server.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
import time
from typing import Any, Protocol

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF


def digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class Store(Protocol):
    def get(self, kind: str, key: str) -> dict[str, Any] | None: ...
    def put(self, kind: str, key: str, value: dict[str, Any], expires_at: float | None = None) -> None: ...
    def delete(self, kind: str, key: str) -> None: ...
    def pop(self, kind: str, key: str) -> dict[str, Any] | None: ...
    def count(self, kind: str) -> int: ...


class MemoryStore:
    def __init__(self) -> None:
        self._data: dict[tuple[str, str], tuple[dict[str, Any], float | None]] = {}
        self._lock = threading.Lock()

    def get(self, kind: str, key: str) -> dict[str, Any] | None:
        with self._lock:
            item = self._data.get((kind, key))
            if item is None:
                return None
            value, exp = item
            if exp is not None and exp < time.time():
                del self._data[(kind, key)]
                return None
            return dict(value)

    def put(self, kind: str, key: str, value: dict[str, Any], expires_at: float | None = None) -> None:
        with self._lock:
            self._data[(kind, key)] = (dict(value), expires_at)
            if len(self._data) % 256 == 0:
                now = time.time()
                for k in [k for k, (_, e) in self._data.items() if e is not None and e < now]:
                    del self._data[k]

    def delete(self, kind: str, key: str) -> None:
        with self._lock:
            self._data.pop((kind, key), None)

    def pop(self, kind: str, key: str) -> dict[str, Any] | None:
        value = self.get(kind, key)
        self.delete(kind, key)
        return value

    def count(self, kind: str) -> int:
        now = time.time()
        with self._lock:
            return sum(1 for (k, _), (_, e) in self._data.items() if k == kind and (e is None or e >= now))


class SqliteStore:
    def __init__(self, path: str, secret: str) -> None:
        if len(secret) < 32:
            raise ValueError("the store needs SPATIALISE_SECRET of at least 32 characters")
        key = HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=b"spatialise-mcp store v1").derive(
            secret.encode())
        self._aes = AESGCM(key)
        self._lock = threading.Lock()
        self._db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("CREATE TABLE IF NOT EXISTS kv (kind TEXT NOT NULL, key TEXT NOT NULL, value BLOB NOT NULL, "
                         "expires_at REAL, PRIMARY KEY (kind, key))")
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
        self._purge()

    def _purge(self) -> None:
        with self._lock:
            self._db.execute("DELETE FROM kv WHERE expires_at IS NOT NULL AND expires_at < ?", (time.time(),))

    def _seal(self, kind: str, key: str, value: dict[str, Any]) -> bytes:
        nonce = os.urandom(12)
        aad = f"{kind}\x00{key}".encode()  # a row cannot be moved to another key
        return nonce + self._aes.encrypt(nonce, json.dumps(value, separators=(",", ":")).encode(), aad)

    def _open(self, kind: str, key: str, blob: bytes) -> dict[str, Any] | None:
        try:
            return json.loads(self._aes.decrypt(blob[:12], blob[12:], f"{kind}\x00{key}".encode()))
        except Exception:  # wrong key or tampered row: treat as absent
            return None

    def get(self, kind: str, key: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._db.execute("SELECT value, expires_at FROM kv WHERE kind = ? AND key = ?", (kind, key)).fetchone()
        if row is None:
            return None
        if row[1] is not None and row[1] < time.time():
            self.delete(kind, key)
            return None
        return self._open(kind, key, row[0])

    def put(self, kind: str, key: str, value: dict[str, Any], expires_at: float | None = None) -> None:
        blob = self._seal(kind, key, value)
        with self._lock:
            self._db.execute("INSERT OR REPLACE INTO kv (kind, key, value, expires_at) VALUES (?, ?, ?, ?)",
                             (kind, key, blob, expires_at))

    def delete(self, kind: str, key: str) -> None:
        with self._lock:
            self._db.execute("DELETE FROM kv WHERE kind = ? AND key = ?", (kind, key))

    def pop(self, kind: str, key: str) -> dict[str, Any] | None:
        value = self.get(kind, key)
        self.delete(kind, key)
        return value

    def count(self, kind: str) -> int:
        with self._lock:
            return self._db.execute("SELECT COUNT(*) FROM kv WHERE kind = ? AND (expires_at IS NULL OR expires_at >= ?)",
                                    (kind, time.time())).fetchone()[0]


def open_store(path: str, secret: str) -> Store:
    return SqliteStore(path, secret) if path else MemoryStore()
