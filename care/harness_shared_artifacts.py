"""Shared, bounded JSON artifacts for the native host and MCP subprocess.

This store is deliberately transport-neutral.  A native MAESTRO host and a
separate stdio MCP process can open the same explicit ``root`` and
``session_id`` and exchange only an opaque artifact id plus a pinned SHA-256
digest.  Public receipts contain shape metadata, never JSON values.

Security boundary
-----------------
Artifacts are plaintext while retained in SQLite.  This module does **not**
claim encryption at rest and does not invent application-level cryptography.
Instead it creates/chmods every owned directory to ``0700`` and the database
and SQLite sidecars to ``0600``, keeps SQLite temporary data in memory, enables
``secure_delete``, expires data, and bounds both row count and byte usage.  The
configured root must therefore be a private local state directory on a trusted
filesystem.  Platform keystore-backed envelope encryption can be added later
without changing the receipt contract.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
import hashlib
import json
import math
import os
from pathlib import Path
import re
import secrets
import sqlite3
import stat
import threading
import time
from typing import Any

from pydantic import BaseModel, ConfigDict


AT_REST_PROTECTION = "permissions-only-plaintext"
_SCHEMA_VERSION = 1
_MAX_JSON_DEPTH = 128
_SESSION_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}")
_ARTIFACT_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}")
_SHA256_RE = re.compile(r"[0-9a-f]{64}")


class SharedArtifactError(RuntimeError):
    """Base error for shared artifact operations."""


class SharedArtifactValidationError(SharedArtifactError):
    """A caller supplied an invalid id, payload, digest, or bound."""


class SharedArtifactUnavailableError(SharedArtifactError):
    """An artifact is absent, expired, changed, or fails integrity checks."""


class SharedArtifactStorageError(SharedArtifactError):
    """The private local SQLite store could not complete an operation."""


class SharedJsonArtifactReceipt(BaseModel):
    """Value-free reference that is safe to return through an agent tool."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    session_id: str
    artifact_id: str
    content_sha256: str
    size_bytes: int
    leaf_pointers: tuple[str, ...]
    expires_at_unix: float


class SharedArtifactCleanupReceipt(BaseModel):
    """Value-free summary of one session-scoped cleanup operation."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    session_id: str
    removed_entries: int
    removed_bytes: int
    remaining_entries: int
    remaining_bytes: int


class SharedArtifactStats(BaseModel):
    """Value-free bounded-store statistics for one session."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    session_id: str
    entry_count: int
    total_bytes: int
    next_expiration_unix: float | None


def _positive_integer(name: str, value: Any) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise SharedArtifactValidationError(f"{name} must be a positive integer")
    return value


def _positive_finite(name: str, value: Any) -> float:
    if (
        not isinstance(value, (int, float))
        or isinstance(value, bool)
        or not math.isfinite(float(value))
        or float(value) <= 0
    ):
        raise SharedArtifactValidationError(f"{name} must be positive and finite")
    return float(value)


def _session_id(value: Any) -> str:
    if not isinstance(value, str) or _SESSION_ID_RE.fullmatch(value) is None:
        raise SharedArtifactValidationError(
            "session_id must contain 1-128 letters, digits, underscores, or hyphens"
        )
    return value


def _artifact_id(value: Any) -> str:
    if not isinstance(value, str) or _ARTIFACT_ID_RE.fullmatch(value) is None:
        raise SharedArtifactValidationError("artifact_id has an invalid format")
    return value


def _content_digest(value: Any) -> str:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise SharedArtifactValidationError(
            "content_sha256 must be a lowercase SHA-256 digest"
        )
    return value


def _normalise_json(
    value: Any,
    *,
    depth: int = 0,
    active_containers: set[int] | None = None,
) -> Any:
    if depth > _MAX_JSON_DEPTH:
        raise SharedArtifactValidationError("artifact JSON is nested too deeply")
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise SharedArtifactValidationError("artifact must contain finite JSON")
        return value

    active = active_containers if active_containers is not None else set()
    if isinstance(value, Mapping):
        identity = id(value)
        if identity in active:
            raise SharedArtifactValidationError("artifact JSON must not be cyclic")
        active.add(identity)
        try:
            normalised: dict[str, Any] = {}
            for key, child in value.items():
                if not isinstance(key, str):
                    raise SharedArtifactValidationError(
                        "artifact JSON object keys must be strings"
                    )
                normalised[key] = _normalise_json(
                    child,
                    depth=depth + 1,
                    active_containers=active,
                )
            return normalised
        finally:
            active.remove(identity)

    if isinstance(value, list):
        identity = id(value)
        if identity in active:
            raise SharedArtifactValidationError("artifact JSON must not be cyclic")
        active.add(identity)
        try:
            return [
                _normalise_json(
                    child,
                    depth=depth + 1,
                    active_containers=active,
                )
                for child in value
            ]
        finally:
            active.remove(identity)

    raise SharedArtifactValidationError(
        "artifact must contain only JSON objects, arrays, and scalar values"
    )


def _canonical_bytes(payload: Any) -> bytes:
    if not isinstance(payload, Mapping):
        raise SharedArtifactValidationError("artifact root must be a JSON object")
    normalised = _normalise_json(payload)
    assert isinstance(normalised, dict)
    try:
        encoded = json.dumps(
            normalised,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise SharedArtifactValidationError(
            "artifact must be canonical finite UTF-8 JSON"
        ) from exc
    return encoded


def _encode_pointer(segments: Sequence[str]) -> str:
    if not segments:
        return ""
    return "/" + "/".join(
        segment.replace("~", "~0").replace("/", "~1") for segment in segments
    )


def _leaf_pointers(value: Any, base: tuple[str, ...] = ()) -> list[str]:
    if isinstance(value, dict) and value:
        leaves: list[str] = []
        for key, child in value.items():
            leaves.extend(_leaf_pointers(child, (*base, key)))
        return leaves
    # Arrays stay atomic: exposing their length or positions would disclose
    # more of the private value and would imply an adaptation contract.
    return [_encode_pointer(base)]


def _decode_verified_row(row: sqlite3.Row) -> tuple[dict[str, Any], bytes]:
    raw = row["canonical_json"]
    if isinstance(raw, memoryview):
        raw = raw.tobytes()
    if not isinstance(raw, bytes):
        raise SharedArtifactUnavailableError("artifact integrity check failed")
    try:
        decoded = json.loads(
            raw.decode("utf-8"),
            parse_constant=lambda _value: (_ for _ in ()).throw(ValueError()),
        )
        canonical = _canonical_bytes(decoded)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError, SharedArtifactError):
        raise SharedArtifactUnavailableError(
            "artifact integrity check failed"
        ) from None
    if not isinstance(decoded, dict) or canonical != raw:
        raise SharedArtifactUnavailableError("artifact integrity check failed")
    digest = hashlib.sha256(raw).hexdigest()
    if not secrets.compare_digest(digest, str(row["content_sha256"])) or len(
        raw
    ) != int(row["size_bytes"]):
        raise SharedArtifactUnavailableError("artifact integrity check failed")
    return decoded, raw


class SharedJsonArtifactStore:
    """SQLite-backed artifact store shared by local processes.

    Each instance is bound to one explicit session.  SQLite's
    ``BEGIN IMMEDIATE`` serialises purge, eviction, and insertion across the
    native host and stdio server so configured bounds cannot be raced.

    Payloads are plaintext at rest; see :data:`AT_REST_PROTECTION` and the
    module security boundary above.
    """

    def __init__(
        self,
        *,
        root: str | os.PathLike[str],
        session_id: str,
        ttl_seconds: float = 3600.0,
        max_entries: int = 128,
        max_payload_bytes: int = 8 * 1024 * 1024,
        max_total_bytes: int = 32 * 1024 * 1024,
        busy_timeout_seconds: float = 5.0,
        clock: Callable[[], float] = time.time,
        id_factory: Callable[[], str] | None = None,
    ) -> None:
        candidate_root = Path(root).expanduser()
        if candidate_root.exists() and candidate_root.is_symlink():
            raise SharedArtifactStorageError("artifact root must not be a symlink")
        self._root = candidate_root.resolve()
        self._session_id = _session_id(session_id)
        self._ttl_seconds = _positive_finite("ttl_seconds", ttl_seconds)
        self._max_entries = _positive_integer("max_entries", max_entries)
        self._max_payload_bytes = _positive_integer(
            "max_payload_bytes", max_payload_bytes
        )
        self._max_total_bytes = _positive_integer("max_total_bytes", max_total_bytes)
        if self._max_total_bytes < self._max_payload_bytes:
            raise SharedArtifactValidationError(
                "max_total_bytes must be at least max_payload_bytes"
            )
        self._busy_timeout_seconds = _positive_finite(
            "busy_timeout_seconds", busy_timeout_seconds
        )
        if not callable(clock):
            raise SharedArtifactValidationError("clock must be callable")
        if id_factory is not None and not callable(id_factory):
            raise SharedArtifactValidationError("id_factory must be callable")
        self._clock = clock
        # ``token_urlsafe`` may legally begin with ``-`` or ``_``, while the
        # public artifact-id grammar deliberately requires an alphanumeric
        # first character. Hex keeps opaque entropy and satisfies the grammar
        # deterministically instead of failing at random in production.
        self._id_factory = id_factory or (lambda: secrets.token_hex(24))
        self._lock = threading.RLock()

        session_digest = hashlib.sha256(self._session_id.encode("utf-8")).hexdigest()
        self._session_digest = session_digest
        self._sessions_dir = self._root / "sessions"
        self._session_dir = self._sessions_dir / session_digest[:32]
        self._database_path = self._session_dir / "artifacts.sqlite3"
        self._prepare_storage()

    @property
    def root(self) -> Path:
        return self._root

    @property
    def session_id(self) -> str:
        return self._session_id

    @property
    def database_path(self) -> Path:
        """Return the digest-scoped database path for host coordination."""

        return self._database_path

    @property
    def at_rest_protection(self) -> str:
        """State plainly that active database values are not encrypted."""

        return AT_REST_PROTECTION

    def stage(self, payload: Mapping[str, Any]) -> SharedJsonArtifactReceipt:
        """Atomically retain one immutable object-root JSON payload."""

        canonical = _canonical_bytes(payload)
        size_bytes = len(canonical)
        if size_bytes > self._max_payload_bytes:
            raise SharedArtifactValidationError(
                "artifact exceeds the configured per-payload byte limit"
            )
        digest = hashlib.sha256(canonical).hexdigest()
        decoded = json.loads(canonical)
        pointers = tuple(sorted(_leaf_pointers(decoded)))

        with self._lock, self._transaction(write=True) as connection:
            now = self._now()
            self._purge_expired_locked(connection, now)
            entry_count, total_bytes = self._counts_locked(connection)
            while entry_count >= self._max_entries or (
                total_bytes + size_bytes > self._max_total_bytes
            ):
                oldest = connection.execute(
                    """
                    SELECT sequence, size_bytes FROM shared_json_artifacts
                    ORDER BY created_at ASC, sequence ASC LIMIT 1
                    """
                ).fetchone()
                if oldest is None:
                    break
                connection.execute(
                    "DELETE FROM shared_json_artifacts WHERE sequence = ?",
                    (oldest["sequence"],),
                )
                entry_count -= 1
                total_bytes -= int(oldest["size_bytes"])

            selected_id = self._allocate_id_locked(connection)
            expires_at = now + self._ttl_seconds
            if not math.isfinite(expires_at):
                raise SharedArtifactValidationError(
                    "artifact expiration timestamp is outside the supported range"
                )
            connection.execute(
                """
                INSERT INTO shared_json_artifacts (
                    artifact_id, content_sha256, canonical_json, size_bytes,
                    created_at, expires_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    selected_id,
                    digest,
                    sqlite3.Binary(canonical),
                    size_bytes,
                    now,
                    expires_at,
                ),
            )
        return SharedJsonArtifactReceipt(
            session_id=self._session_id,
            artifact_id=selected_id,
            content_sha256=digest,
            size_bytes=size_bytes,
            leaf_pointers=pointers,
            expires_at_unix=expires_at,
        )

    def inspect(self, artifact_id: str) -> SharedJsonArtifactReceipt:
        """Return a structural receipt without returning stored values."""

        selected_id = _artifact_id(artifact_id)
        with self._lock, self._transaction(write=True) as connection:
            row = self._get_live_row_locked(connection, selected_id)
            decoded, raw = _decode_verified_row(row)
            return SharedJsonArtifactReceipt(
                session_id=self._session_id,
                artifact_id=selected_id,
                content_sha256=hashlib.sha256(raw).hexdigest(),
                size_bytes=len(raw),
                leaf_pointers=tuple(sorted(_leaf_pointers(decoded))),
                expires_at_unix=float(row["expires_at"]),
            )

    def resolve(
        self,
        artifact_id: str,
        *,
        content_sha256: str,
    ) -> dict[str, Any]:
        """Return a detached payload only when its exact digest is pinned."""

        selected_id = _artifact_id(artifact_id)
        selected_digest = _content_digest(content_sha256)
        with self._lock, self._transaction(write=True) as connection:
            row = self._get_live_row_locked(connection, selected_id)
            decoded, raw = _decode_verified_row(row)
            actual_digest = hashlib.sha256(raw).hexdigest()
            if not secrets.compare_digest(actual_digest, selected_digest):
                raise SharedArtifactUnavailableError("artifact content changed")
            # ``decoded`` was freshly loaded and validated, so every resolve is
            # already a detached copy from the SQLite BLOB.
            return decoded

    def discard(self, artifact_id: str) -> bool:
        """Delete one artifact without reading or returning its payload."""

        selected_id = _artifact_id(artifact_id)
        with self._lock, self._transaction(write=True) as connection:
            self._purge_expired_locked(connection, self._now())
            cursor = connection.execute(
                "DELETE FROM shared_json_artifacts WHERE artifact_id = ?",
                (selected_id,),
            )
            return cursor.rowcount > 0

    def cleanup(self) -> SharedArtifactCleanupReceipt:
        """Delete every expired artifact in this session and report counts."""

        with self._lock, self._transaction(write=True) as connection:
            removed_entries, removed_bytes = self._purge_expired_locked(
                connection, self._now()
            )
            remaining_entries, remaining_bytes = self._counts_locked(connection)
            return SharedArtifactCleanupReceipt(
                session_id=self._session_id,
                removed_entries=removed_entries,
                removed_bytes=removed_bytes,
                remaining_entries=remaining_entries,
                remaining_bytes=remaining_bytes,
            )

    def clear(self) -> SharedArtifactCleanupReceipt:
        """Delete all retained artifacts in this bound session."""

        with self._lock, self._transaction(write=True) as connection:
            removed_entries, removed_bytes = self._counts_locked(connection)
            connection.execute("DELETE FROM shared_json_artifacts")
            return SharedArtifactCleanupReceipt(
                session_id=self._session_id,
                removed_entries=removed_entries,
                removed_bytes=removed_bytes,
                remaining_entries=0,
                remaining_bytes=0,
            )

    def stats(self) -> SharedArtifactStats:
        """Return value-free session statistics after expiring stale rows."""

        with self._lock, self._transaction(write=True) as connection:
            self._purge_expired_locked(connection, self._now())
            entry_count, total_bytes = self._counts_locked(connection)
            row = connection.execute(
                "SELECT MIN(expires_at) AS next_expiration FROM shared_json_artifacts"
            ).fetchone()
            next_expiration = row["next_expiration"] if row is not None else None
            return SharedArtifactStats(
                session_id=self._session_id,
                entry_count=entry_count,
                total_bytes=total_bytes,
                next_expiration_unix=(
                    float(next_expiration) if next_expiration is not None else None
                ),
            )

    def _prepare_storage(self) -> None:
        try:
            for directory in (self._root, self._sessions_dir, self._session_dir):
                directory.mkdir(mode=0o700, parents=True, exist_ok=True)
                if directory.is_symlink() or not directory.is_dir():
                    raise SharedArtifactStorageError(
                        "artifact storage directory is unsafe"
                    )
                os.chmod(directory, stat.S_IRWXU)

            if self._database_path.exists():
                if (
                    self._database_path.is_symlink()
                    or not self._database_path.is_file()
                ):
                    raise SharedArtifactStorageError("artifact database path is unsafe")
            else:
                flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
                if hasattr(os, "O_NOFOLLOW"):
                    flags |= os.O_NOFOLLOW
                try:
                    descriptor = os.open(self._database_path, flags, 0o600)
                except FileExistsError:
                    # Another host process won the creation race.  Validate it
                    # below before SQLite opens the path.
                    pass
                else:
                    os.close(descriptor)
            self._harden_storage_files()

            with self._transaction(write=True) as connection:
                # Keep schema creation inside the same BEGIN IMMEDIATE.  The
                # sqlite3 ``executescript`` helper performs an implicit commit
                # and would weaken the cross-process initialisation boundary.
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS shared_artifact_meta (
                        key TEXT PRIMARY KEY,
                        value TEXT NOT NULL
                    )
                    """
                )
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS shared_json_artifacts (
                        sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                        artifact_id TEXT NOT NULL UNIQUE,
                        content_sha256 TEXT NOT NULL,
                        canonical_json BLOB NOT NULL,
                        size_bytes INTEGER NOT NULL CHECK (size_bytes > 0),
                        created_at REAL NOT NULL,
                        expires_at REAL NOT NULL
                    )
                    """
                )
                connection.execute(
                    """
                    CREATE INDEX IF NOT EXISTS shared_artifacts_expiration
                        ON shared_json_artifacts(expires_at, sequence)
                    """
                )
                version = connection.execute(
                    "SELECT value FROM shared_artifact_meta WHERE key = 'schema_version'"
                ).fetchone()
                if version is not None and int(version["value"]) != _SCHEMA_VERSION:
                    raise SharedArtifactStorageError(
                        "artifact database schema is unsupported"
                    )
                session = connection.execute(
                    "SELECT value FROM shared_artifact_meta WHERE key = 'session_digest'"
                ).fetchone()
                if session is not None and not secrets.compare_digest(
                    str(session["value"]), self._session_digest
                ):
                    raise SharedArtifactStorageError(
                        "artifact database belongs to another session"
                    )
                connection.execute(
                    "INSERT OR IGNORE INTO shared_artifact_meta(key, value) VALUES (?, ?)",
                    ("schema_version", str(_SCHEMA_VERSION)),
                )
                connection.execute(
                    "INSERT OR IGNORE INTO shared_artifact_meta(key, value) VALUES (?, ?)",
                    ("session_digest", self._session_digest),
                )
        except SharedArtifactError:
            raise
        except (OSError, sqlite3.Error, TypeError, ValueError) as exc:
            raise SharedArtifactStorageError(
                "shared artifact storage could not be initialised"
            ) from exc

    def _connect(self) -> sqlite3.Connection:
        try:
            connection = sqlite3.connect(
                self._database_path,
                timeout=self._busy_timeout_seconds,
            )
            connection.row_factory = sqlite3.Row
            connection.execute(
                f"PRAGMA busy_timeout = {int(self._busy_timeout_seconds * 1000)}"
            )
            connection.execute("PRAGMA journal_mode = DELETE")
            connection.execute("PRAGMA synchronous = FULL")
            connection.execute("PRAGMA temp_store = MEMORY")
            connection.execute("PRAGMA secure_delete = ON")
            connection.execute("PRAGMA trusted_schema = OFF")
            return connection
        except sqlite3.Error as exc:
            raise SharedArtifactStorageError(
                "shared artifact database could not be opened"
            ) from exc

    @contextmanager
    def _transaction(self, *, write: bool) -> Iterator[sqlite3.Connection]:
        connection: sqlite3.Connection | None = None
        try:
            connection = self._connect()
            if write:
                connection.execute("BEGIN IMMEDIATE")
            yield connection
            if write:
                connection.commit()
        except SharedArtifactError:
            if connection is not None and write:
                connection.rollback()
            raise
        except sqlite3.Error as exc:
            if connection is not None and write:
                connection.rollback()
            raise SharedArtifactStorageError(
                "shared artifact storage operation failed"
            ) from exc
        except BaseException:
            if connection is not None and write:
                connection.rollback()
            raise
        finally:
            if connection is not None:
                connection.close()
            self._harden_storage_files()

    def _harden_storage_files(self) -> None:
        for path in (
            self._database_path,
            self._database_path.with_name(f"{self._database_path.name}-journal"),
            self._database_path.with_name(f"{self._database_path.name}-wal"),
            self._database_path.with_name(f"{self._database_path.name}-shm"),
        ):
            if not path.exists():
                continue
            if path.is_symlink() or not stat.S_ISREG(path.stat().st_mode):
                raise SharedArtifactStorageError("artifact storage file is unsafe")
            os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)

    def _now(self) -> float:
        try:
            value = float(self._clock())
        except (TypeError, ValueError, OverflowError) as exc:
            raise SharedArtifactValidationError(
                "clock must return a finite timestamp"
            ) from exc
        if not math.isfinite(value):
            raise SharedArtifactValidationError("clock must return a finite timestamp")
        return value

    def _allocate_id_locked(self, connection: sqlite3.Connection) -> str:
        for _attempt in range(16):
            selected = _artifact_id(self._id_factory())
            exists = connection.execute(
                "SELECT 1 FROM shared_json_artifacts WHERE artifact_id = ?",
                (selected,),
            ).fetchone()
            if exists is None:
                return selected
        raise SharedArtifactStorageError("could not allocate a unique artifact id")

    def _get_live_row_locked(
        self,
        connection: sqlite3.Connection,
        artifact_id: str,
    ) -> sqlite3.Row:
        self._purge_expired_locked(connection, self._now())
        row = connection.execute(
            "SELECT * FROM shared_json_artifacts WHERE artifact_id = ?",
            (artifact_id,),
        ).fetchone()
        if row is None:
            raise SharedArtifactUnavailableError("artifact is missing or expired")
        return row

    @staticmethod
    def _counts_locked(connection: sqlite3.Connection) -> tuple[int, int]:
        row = connection.execute(
            """
            SELECT COUNT(*) AS entry_count,
                   COALESCE(SUM(size_bytes), 0) AS total_bytes
            FROM shared_json_artifacts
            """
        ).fetchone()
        assert row is not None
        return int(row["entry_count"]), int(row["total_bytes"])

    @staticmethod
    def _purge_expired_locked(
        connection: sqlite3.Connection,
        now: float,
    ) -> tuple[int, int]:
        row = connection.execute(
            """
            SELECT COUNT(*) AS entry_count,
                   COALESCE(SUM(size_bytes), 0) AS total_bytes
            FROM shared_json_artifacts WHERE expires_at <= ?
            """,
            (now,),
        ).fetchone()
        assert row is not None
        entry_count = int(row["entry_count"])
        total_bytes = int(row["total_bytes"])
        if entry_count:
            connection.execute(
                "DELETE FROM shared_json_artifacts WHERE expires_at <= ?",
                (now,),
            )
        return entry_count, total_bytes


__all__ = [
    "AT_REST_PROTECTION",
    "SharedArtifactCleanupReceipt",
    "SharedArtifactError",
    "SharedArtifactStats",
    "SharedArtifactStorageError",
    "SharedArtifactUnavailableError",
    "SharedArtifactValidationError",
    "SharedJsonArtifactReceipt",
    "SharedJsonArtifactStore",
]
