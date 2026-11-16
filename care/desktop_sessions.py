"""Persistent, provider-free session state for the native Maestro client.

The browser UI is intentionally not the authority for conversation history.
This module owns a small SQLite store scoped to the current workspace.  It is
safe to construct again after an application restart and exposes the command
semantics needed by the native shell:

* ``new_session`` archives the current session and starts a fresh one;
* ``clear_current`` clears the current transcript without changing its id;
* ``resume_session`` selects an exact id or the most recently updated session;
* every turn append is bounded, redacted, and committed in one transaction.

There are no model, Memory, Platform, or network dependencies here.  The
integration layer remains responsible for deciding when a provider should be
called and merely records the resulting user-visible turns afterwards.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import sqlite3
import stat
import threading
from collections.abc import Callable, Iterable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timezone
from itertools import islice
from pathlib import Path
from typing import Any, Literal, TypeAlias

from care.redaction import redact_sensitive_text
from care.runtime import user_paths
from care.runtime.session_persistence import make_session_id


SessionMode: TypeAlias = Literal["interactive", "production"]
GenerationMode: TypeAlias = Literal["fast", "deep"]
SessionRole: TypeAlias = Literal["user", "assistant", "system", "tool"]

SESSION_MODES = frozenset({"interactive", "production"})
GENERATION_MODES = frozenset({"fast", "deep"})
SESSION_ROLES = frozenset({"user", "assistant", "system", "tool"})

DEFAULT_MAX_SESSIONS = 128
DEFAULT_MAX_TURNS = 1_000
DEFAULT_MAX_TURN_CHARS = 128 * 1024
DEFAULT_MAX_SESSION_CHARS = 4 * 1024 * 1024
DEFAULT_MAX_PROVENANCE_BYTES = 64 * 1024
DEFAULT_LIST_LIMIT = 20
MAX_LIST_LIMIT = 256

_SCHEMA_VERSION = 1
_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_SECRET_KEY_RE = re.compile(
    r"(?:api.?key|authorization|password|passwd|secret|token|credential)",
    re.IGNORECASE,
)


class DesktopSessionError(RuntimeError):
    """Base class for stable native-session failures."""


class DesktopSessionValidationError(DesktopSessionError, ValueError):
    """Raised when a caller supplies an invalid role, mode, id, or bound."""


class DesktopSessionNotFoundError(DesktopSessionError, LookupError):
    """Raised when an exact session id cannot be resumed or inspected."""


class DesktopSessionStorageError(DesktopSessionError):
    """Raised when persisted session state cannot be read or written safely."""


@dataclass(frozen=True)
class DesktopSessionTurn:
    """One immutable, already-redacted transcript turn."""

    sequence: int
    role: SessionRole
    text: str
    created_at: datetime
    session_mode: SessionMode
    generation_mode: GenerationMode
    provenance: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "sequence": self.sequence,
            "role": self.role,
            "text": self.text,
            "created_at": _format_datetime(self.created_at),
            "session_mode": self.session_mode,
            "generation_mode": self.generation_mode,
            "provenance": _copy_json(self.provenance),
        }


@dataclass(frozen=True)
class DesktopSessionSnapshot:
    """Full persisted session snapshot returned to the native server."""

    session_id: str
    created_at: datetime
    updated_at: datetime
    archived_at: datetime | None
    is_current: bool
    session_mode: SessionMode
    generation_mode: GenerationMode
    dropped_turns: int
    turns: tuple[DesktopSessionTurn, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "created_at": _format_datetime(self.created_at),
            "updated_at": _format_datetime(self.updated_at),
            "archived_at": (
                _format_datetime(self.archived_at) if self.archived_at else None
            ),
            "is_current": self.is_current,
            "session_mode": self.session_mode,
            "generation_mode": self.generation_mode,
            "dropped_turns": self.dropped_turns,
            "turn_count": len(self.turns),
            "turns": [turn.to_dict() for turn in self.turns],
        }


@dataclass(frozen=True)
class DesktopSessionSummary:
    """Bounded list/resume projection that does not load transcript bodies."""

    session_id: str
    created_at: datetime
    updated_at: datetime
    archived_at: datetime | None
    is_current: bool
    session_mode: SessionMode
    generation_mode: GenerationMode
    turn_count: int
    dropped_turns: int
    first_user_preview: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "created_at": _format_datetime(self.created_at),
            "updated_at": _format_datetime(self.updated_at),
            "archived_at": (
                _format_datetime(self.archived_at) if self.archived_at else None
            ),
            "is_current": self.is_current,
            "session_mode": self.session_mode,
            "generation_mode": self.generation_mode,
            "turn_count": self.turn_count,
            "dropped_turns": self.dropped_turns,
            "first_user_preview": self.first_user_preview,
        }


class DesktopSessionStore:
    """Workspace-scoped, transactional native conversation store.

    ``state_dir`` follows :mod:`care.runtime.user_paths` by default and is
    injectable for a packaged host or tests.  A digest of ``workspace_root``
    isolates projects without persisting the absolute workspace path.
    """

    def __init__(
        self,
        *,
        workspace_root: str | os.PathLike[str] | None = None,
        state_dir: str | os.PathLike[str] | None = None,
        session_id_factory: Callable[[], str] = make_session_id,
        clock: Callable[[], datetime] | None = None,
        default_session_mode: SessionMode = "interactive",
        default_generation_mode: GenerationMode = "fast",
        max_sessions: int = DEFAULT_MAX_SESSIONS,
        max_turns: int = DEFAULT_MAX_TURNS,
        max_turn_chars: int = DEFAULT_MAX_TURN_CHARS,
        max_session_chars: int = DEFAULT_MAX_SESSION_CHARS,
        max_provenance_bytes: int = DEFAULT_MAX_PROVENANCE_BYTES,
    ) -> None:
        self._workspace_root = Path(workspace_root or Path.cwd()).expanduser().resolve()
        if not self._workspace_root.is_dir():
            raise DesktopSessionValidationError(
                "workspace_root must be an existing directory"
            )
        self._state_dir = (
            Path(state_dir if state_dir is not None else user_paths.CARE_STATE_DIR)
            .expanduser()
            .resolve()
        )
        self._default_session_mode = _session_mode(default_session_mode)
        self._default_generation_mode = _generation_mode(default_generation_mode)
        self._max_sessions = _positive_bound("max_sessions", max_sessions)
        self._max_turns = _positive_bound("max_turns", max_turns)
        self._max_turn_chars = _positive_bound("max_turn_chars", max_turn_chars)
        self._max_session_chars = _positive_bound(
            "max_session_chars", max_session_chars
        )
        if self._max_turn_chars > self._max_session_chars:
            raise DesktopSessionValidationError(
                "max_turn_chars must not exceed max_session_chars"
            )
        self._max_provenance_bytes = _positive_bound(
            "max_provenance_bytes", max_provenance_bytes
        )
        if not callable(session_id_factory):
            raise DesktopSessionValidationError("session_id_factory must be callable")
        self._session_id_factory = session_id_factory
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._lock = threading.RLock()

        workspace_digest = hashlib.sha256(
            os.fsencode(str(self._workspace_root))
        ).hexdigest()
        self._workspace_digest = workspace_digest
        self._storage_dir = self._state_dir / "desktop-sessions" / workspace_digest[:24]
        self._database_path = self._storage_dir / "sessions.sqlite3"
        self._prepare_storage()

    @property
    def database_path(self) -> Path:
        """Inspectable integration seam; contains no workspace path component."""

        return self._database_path

    @property
    def workspace_root(self) -> Path:
        return self._workspace_root

    def current_session(self) -> DesktopSessionSnapshot:
        """Return the current session, creating one when this workspace is new."""

        with self._lock, self._transaction(write=True) as connection:
            row = self._ensure_current_locked(connection)
            return self._snapshot_locked(connection, row)

    def new_session(
        self,
        *,
        session_mode: SessionMode | None = None,
        generation_mode: GenerationMode | None = None,
    ) -> DesktopSessionSnapshot:
        """Archive the current session and atomically start a new empty one."""

        with self._lock, self._transaction(write=True) as connection:
            current = self._current_row(connection)
            selected_session_mode = _session_mode(
                session_mode
                if session_mode is not None
                else (
                    current["session_mode"]
                    if current is not None
                    else self._default_session_mode
                )
            )
            selected_generation_mode = _generation_mode(
                generation_mode
                if generation_mode is not None
                else (
                    current["generation_mode"]
                    if current is not None
                    else self._default_generation_mode
                )
            )
            now = self._now()
            if current is not None:
                connection.execute(
                    """
                    UPDATE desktop_sessions
                    SET is_current = 0, archived_at = ?, updated_at = ?
                    WHERE session_id = ?
                    """,
                    (now, now, current["session_id"]),
                )
            row = self._insert_session_locked(
                connection,
                now=now,
                session_mode=selected_session_mode,
                generation_mode=selected_generation_mode,
            )
            self._prune_sessions_locked(connection)
            return self._snapshot_locked(connection, row)

    def clear_current(self) -> DesktopSessionSnapshot:
        """Clear transcript state while preserving id and both mode settings."""

        with self._lock, self._transaction(write=True) as connection:
            row = self._ensure_current_locked(connection)
            connection.execute(
                "DELETE FROM desktop_session_turns WHERE session_id = ?",
                (row["session_id"],),
            )
            now = self._now()
            connection.execute(
                """
                UPDATE desktop_sessions
                SET updated_at = ?, dropped_turns = 0
                WHERE session_id = ?
                """,
                (now, row["session_id"]),
            )
            updated = self._session_row_locked(connection, row["session_id"])
            return self._snapshot_locked(connection, updated)

    def set_modes(
        self,
        *,
        session_mode: SessionMode | None = None,
        generation_mode: GenerationMode | None = None,
    ) -> DesktopSessionSnapshot:
        """Update conversation semantics independently from generation depth."""

        if session_mode is None and generation_mode is None:
            raise DesktopSessionValidationError("at least one mode must be supplied")
        with self._lock, self._transaction(write=True) as connection:
            row = self._ensure_current_locked(connection)
            selected_session_mode = _session_mode(
                session_mode if session_mode is not None else row["session_mode"]
            )
            selected_generation_mode = _generation_mode(
                generation_mode
                if generation_mode is not None
                else row["generation_mode"]
            )
            connection.execute(
                """
                UPDATE desktop_sessions
                SET session_mode = ?, generation_mode = ?, updated_at = ?
                WHERE session_id = ?
                """,
                (
                    selected_session_mode,
                    selected_generation_mode,
                    self._now(),
                    row["session_id"],
                ),
            )
            updated = self._session_row_locked(connection, row["session_id"])
            return self._snapshot_locked(connection, updated)

    def append_turn(
        self,
        role: SessionRole,
        text: str,
        *,
        session_id: str | None = None,
        provenance: Mapping[str, Any] | None = None,
        supplied_secrets: Iterable[str] = (),
    ) -> DesktopSessionTurn:
        """Redact, bound, and atomically append one transcript turn.

        Once the configured transcript limits are reached, oldest retained
        turns are removed in the same transaction and ``dropped_turns`` is
        incremented.  Sequence numbers remain monotonic across pruning.
        """

        selected_role = _session_role(role)
        if not isinstance(text, str):
            raise DesktopSessionValidationError("turn text must be a string")
        secrets = tuple(
            value for value in supplied_secrets if isinstance(value, str) and value
        )
        safe_text = redact_sensitive_text(
            text,
            secrets,
            max_chars=self._max_turn_chars,
        )
        safe_provenance = _safe_provenance(
            provenance or {},
            workspace_root=self._workspace_root,
            supplied_secrets=secrets,
            max_bytes=self._max_provenance_bytes,
        )
        encoded_provenance = json.dumps(
            safe_provenance,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        selected_session_id = (
            _session_id(session_id) if session_id is not None else None
        )
        with self._lock, self._transaction(write=True) as connection:
            session = (
                self._ensure_current_locked(connection)
                if selected_session_id is None
                else self._session_row_locked(connection, selected_session_id)
            )
            sequence = int(session["next_sequence"])
            created_at = self._now()
            connection.execute(
                """
                INSERT INTO desktop_session_turns (
                    session_id, sequence, role, text, created_at,
                    session_mode, generation_mode, provenance_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    session["session_id"],
                    sequence,
                    selected_role,
                    safe_text,
                    created_at,
                    session["session_mode"],
                    session["generation_mode"],
                    encoded_provenance,
                ),
            )
            connection.execute(
                """
                UPDATE desktop_sessions
                SET next_sequence = ?, updated_at = ?
                WHERE session_id = ?
                """,
                (sequence + 1, created_at, session["session_id"]),
            )
            self._prune_turns_locked(connection, session["session_id"])
        return DesktopSessionTurn(
            sequence=sequence,
            role=selected_role,
            text=safe_text,
            created_at=_parse_datetime(created_at),
            session_mode=_session_mode(session["session_mode"]),
            generation_mode=_generation_mode(session["generation_mode"]),
            provenance=_copy_json(safe_provenance),
        )

    def append_user(self, text: str, **kwargs: Any) -> DesktopSessionTurn:
        return self.append_turn("user", text, **kwargs)

    def append_assistant(self, text: str, **kwargs: Any) -> DesktopSessionTurn:
        return self.append_turn("assistant", text, **kwargs)

    def append_system(self, text: str, **kwargs: Any) -> DesktopSessionTurn:
        return self.append_turn("system", text, **kwargs)

    def append_tool(self, text: str, **kwargs: Any) -> DesktopSessionTurn:
        return self.append_turn("tool", text, **kwargs)

    def list_sessions(
        self, *, limit: int = DEFAULT_LIST_LIMIT
    ) -> list[DesktopSessionSummary]:
        """Return persisted sessions newest-first, including the current one."""

        if (
            not isinstance(limit, int)
            or isinstance(limit, bool)
            or not 1 <= limit <= MAX_LIST_LIMIT
        ):
            raise DesktopSessionValidationError(
                f"limit must be an integer from 1 to {MAX_LIST_LIMIT}"
            )
        with self._lock, self._transaction(write=False) as connection:
            rows = connection.execute(
                """
                SELECT * FROM desktop_sessions
                ORDER BY updated_at DESC, created_at DESC, session_id DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
            return [self._summary_locked(connection, row) for row in rows]

    def get_session(self, session_id: str) -> DesktopSessionSnapshot:
        """Load one exact persisted session without changing the current one."""

        selected = _session_id(session_id)
        with self._lock, self._transaction(write=False) as connection:
            row = self._session_row_locked(connection, selected)
            return self._snapshot_locked(connection, row)

    def resume_session(self, target: str) -> DesktopSessionSnapshot:
        """Make ``latest`` or one exact session id the current session."""

        if not isinstance(target, str) or not target.strip():
            raise DesktopSessionValidationError(
                "resume target must be `latest` or an exact session id"
            )
        raw_target = target.strip()
        with self._lock, self._transaction(write=True) as connection:
            if raw_target.casefold() == "latest":
                row = connection.execute(
                    """
                    SELECT * FROM desktop_sessions
                    ORDER BY updated_at DESC, created_at DESC, session_id DESC
                    LIMIT 1
                    """
                ).fetchone()
                if row is None:
                    raise DesktopSessionNotFoundError("no persisted sessions exist")
            else:
                row = self._session_row_locked(connection, _session_id(raw_target))
            now = self._now()
            connection.execute(
                """
                UPDATE desktop_sessions
                SET is_current = 0,
                    archived_at = CASE
                        WHEN archived_at IS NULL THEN ? ELSE archived_at
                    END
                WHERE is_current = 1 AND session_id <> ?
                """,
                (now, row["session_id"]),
            )
            connection.execute(
                """
                UPDATE desktop_sessions
                SET is_current = 1, archived_at = NULL, updated_at = ?
                WHERE session_id = ?
                """,
                (now, row["session_id"]),
            )
            resumed = self._session_row_locked(connection, row["session_id"])
            return self._snapshot_locked(connection, resumed)

    def transcript(self, session_id: str | None = None) -> list[dict[str, Any]]:
        """Return export-ready rows for the current or one exact session."""

        snapshot = (
            self.current_session()
            if session_id is None
            else self.get_session(session_id)
        )
        return [
            {
                "role": turn.role,
                "text": turn.text,
                "timestamp": _format_datetime(turn.created_at),
                "mode": turn.session_mode,
                "session_mode": turn.session_mode,
                "generation_mode": turn.generation_mode,
                "provenance": _copy_json(turn.provenance),
            }
            for turn in snapshot.turns
        ]

    # ------------------------------------------------------------------
    # Storage internals
    # ------------------------------------------------------------------

    def _prepare_storage(self) -> None:
        try:
            self._storage_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
            if self._storage_dir.is_symlink() or not self._storage_dir.is_dir():
                raise DesktopSessionStorageError(
                    "Desktop session storage directory is unsafe."
                )
            if self._database_path.is_symlink():
                raise DesktopSessionStorageError(
                    "Desktop session database path is unsafe."
                )
            os.chmod(self._storage_dir, stat.S_IRWXU)
        except DesktopSessionStorageError:
            raise
        except OSError as exc:
            raise DesktopSessionStorageError(
                "Desktop session storage could not be prepared."
            ) from exc

        try:
            with self._connect() as connection:
                connection.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS desktop_session_meta (
                        key TEXT PRIMARY KEY,
                        value TEXT NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS desktop_sessions (
                        session_id TEXT PRIMARY KEY,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        archived_at TEXT,
                        is_current INTEGER NOT NULL CHECK (is_current IN (0, 1)),
                        session_mode TEXT NOT NULL
                            CHECK (session_mode IN ('interactive', 'production')),
                        generation_mode TEXT NOT NULL
                            CHECK (generation_mode IN ('fast', 'deep')),
                        next_sequence INTEGER NOT NULL DEFAULT 1,
                        dropped_turns INTEGER NOT NULL DEFAULT 0
                    );
                    CREATE UNIQUE INDEX IF NOT EXISTS one_current_desktop_session
                        ON desktop_sessions(is_current) WHERE is_current = 1;
                    CREATE INDEX IF NOT EXISTS desktop_sessions_updated
                        ON desktop_sessions(updated_at DESC);
                    CREATE TABLE IF NOT EXISTS desktop_session_turns (
                        session_id TEXT NOT NULL,
                        sequence INTEGER NOT NULL,
                        role TEXT NOT NULL
                            CHECK (role IN ('user', 'assistant', 'system', 'tool')),
                        text TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        session_mode TEXT NOT NULL
                            CHECK (session_mode IN ('interactive', 'production')),
                        generation_mode TEXT NOT NULL
                            CHECK (generation_mode IN ('fast', 'deep')),
                        provenance_json TEXT NOT NULL,
                        PRIMARY KEY (session_id, sequence),
                        FOREIGN KEY (session_id) REFERENCES desktop_sessions(session_id)
                            ON DELETE CASCADE
                    );
                    """
                )
                version_row = connection.execute(
                    "SELECT value FROM desktop_session_meta WHERE key = 'schema_version'"
                ).fetchone()
                if (
                    version_row is not None
                    and int(version_row["value"]) != _SCHEMA_VERSION
                ):
                    raise DesktopSessionStorageError(
                        "Desktop session database schema is unsupported."
                    )
                workspace_row = connection.execute(
                    "SELECT value FROM desktop_session_meta WHERE key = 'workspace_digest'"
                ).fetchone()
                if (
                    workspace_row is not None
                    and workspace_row["value"] != self._workspace_digest
                ):
                    raise DesktopSessionStorageError(
                        "Desktop session database belongs to another workspace."
                    )
                connection.execute(
                    "INSERT OR IGNORE INTO desktop_session_meta(key, value) VALUES (?, ?)",
                    ("schema_version", str(_SCHEMA_VERSION)),
                )
                connection.execute(
                    "INSERT OR IGNORE INTO desktop_session_meta(key, value) VALUES (?, ?)",
                    ("workspace_digest", self._workspace_digest),
                )
                connection.commit()
            os.chmod(self._database_path, stat.S_IRUSR | stat.S_IWUSR)
        except DesktopSessionStorageError:
            raise
        except (OSError, sqlite3.Error, TypeError, ValueError) as exc:
            raise DesktopSessionStorageError(
                "Desktop session database could not be initialised."
            ) from exc

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._database_path, timeout=5.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 5000")
        return connection

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
        except sqlite3.Error as exc:
            if connection is not None and write:
                connection.rollback()
            raise DesktopSessionStorageError(
                "Desktop session storage operation failed."
            ) from exc
        except BaseException:
            if connection is not None and write:
                connection.rollback()
            raise
        finally:
            if connection is not None:
                connection.close()

    def _now(self) -> str:
        value = self._clock()
        if not isinstance(value, datetime):
            raise DesktopSessionValidationError("clock must return a datetime")
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return _format_datetime(value.astimezone(timezone.utc))

    def _current_row(self, connection: sqlite3.Connection) -> sqlite3.Row | None:
        return connection.execute(
            "SELECT * FROM desktop_sessions WHERE is_current = 1 LIMIT 1"
        ).fetchone()

    def _ensure_current_locked(self, connection: sqlite3.Connection) -> sqlite3.Row:
        row = self._current_row(connection)
        if row is not None:
            return row
        return self._insert_session_locked(
            connection,
            now=self._now(),
            session_mode=self._default_session_mode,
            generation_mode=self._default_generation_mode,
        )

    def _insert_session_locked(
        self,
        connection: sqlite3.Connection,
        *,
        now: str,
        session_mode: SessionMode,
        generation_mode: GenerationMode,
    ) -> sqlite3.Row:
        selected_id = ""
        for _attempt in range(16):
            selected_id = _session_id(self._session_id_factory())
            exists = connection.execute(
                "SELECT 1 FROM desktop_sessions WHERE session_id = ?",
                (selected_id,),
            ).fetchone()
            if exists is None:
                break
        else:
            raise DesktopSessionStorageError(
                "Could not allocate a unique desktop session id."
            )
        connection.execute(
            """
            INSERT INTO desktop_sessions (
                session_id, created_at, updated_at, archived_at, is_current,
                session_mode, generation_mode, next_sequence, dropped_turns
            ) VALUES (?, ?, ?, NULL, 1, ?, ?, 1, 0)
            """,
            (selected_id, now, now, session_mode, generation_mode),
        )
        return self._session_row_locked(connection, selected_id)

    def _session_row_locked(
        self, connection: sqlite3.Connection, session_id: str
    ) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM desktop_sessions WHERE session_id = ?",
            (session_id,),
        ).fetchone()
        if row is None:
            raise DesktopSessionNotFoundError(
                f"desktop session {session_id!r} was not found"
            )
        return row

    def _snapshot_locked(
        self, connection: sqlite3.Connection, row: sqlite3.Row
    ) -> DesktopSessionSnapshot:
        turn_rows = connection.execute(
            """
            SELECT * FROM desktop_session_turns
            WHERE session_id = ? ORDER BY sequence ASC
            """,
            (row["session_id"],),
        ).fetchall()
        turns = tuple(self._turn_from_row(item) for item in turn_rows)
        return DesktopSessionSnapshot(
            session_id=_session_id(row["session_id"]),
            created_at=_parse_datetime(row["created_at"]),
            updated_at=_parse_datetime(row["updated_at"]),
            archived_at=(
                _parse_datetime(row["archived_at"]) if row["archived_at"] else None
            ),
            is_current=bool(row["is_current"]),
            session_mode=_session_mode(row["session_mode"]),
            generation_mode=_generation_mode(row["generation_mode"]),
            dropped_turns=max(0, int(row["dropped_turns"])),
            turns=turns,
        )

    def _summary_locked(
        self, connection: sqlite3.Connection, row: sqlite3.Row
    ) -> DesktopSessionSummary:
        turn_count = int(
            connection.execute(
                "SELECT COUNT(*) FROM desktop_session_turns WHERE session_id = ?",
                (row["session_id"],),
            ).fetchone()[0]
        )
        preview_row = connection.execute(
            """
            SELECT text FROM desktop_session_turns
            WHERE session_id = ? AND role = 'user'
            ORDER BY sequence ASC LIMIT 1
            """,
            (row["session_id"],),
        ).fetchone()
        preview = str(preview_row["text"] if preview_row is not None else "")
        preview_lines = preview.splitlines()
        preview = (preview_lines[0] if preview_lines else "")[:160]
        return DesktopSessionSummary(
            session_id=_session_id(row["session_id"]),
            created_at=_parse_datetime(row["created_at"]),
            updated_at=_parse_datetime(row["updated_at"]),
            archived_at=(
                _parse_datetime(row["archived_at"]) if row["archived_at"] else None
            ),
            is_current=bool(row["is_current"]),
            session_mode=_session_mode(row["session_mode"]),
            generation_mode=_generation_mode(row["generation_mode"]),
            turn_count=turn_count,
            dropped_turns=max(0, int(row["dropped_turns"])),
            first_user_preview=preview,
        )

    def _turn_from_row(self, row: sqlite3.Row) -> DesktopSessionTurn:
        try:
            raw_provenance = json.loads(row["provenance_json"])
        except (TypeError, json.JSONDecodeError) as exc:
            raise DesktopSessionStorageError(
                "Stored desktop session provenance is invalid."
            ) from exc
        if not isinstance(raw_provenance, dict):
            raise DesktopSessionStorageError(
                "Stored desktop session provenance is invalid."
            )
        return DesktopSessionTurn(
            sequence=max(1, int(row["sequence"])),
            role=_session_role(row["role"]),
            text=str(row["text"]),
            created_at=_parse_datetime(row["created_at"]),
            session_mode=_session_mode(row["session_mode"]),
            generation_mode=_generation_mode(row["generation_mode"]),
            provenance=_copy_json(raw_provenance),
        )

    def _prune_turns_locked(
        self, connection: sqlite3.Connection, session_id: str
    ) -> None:
        rows = connection.execute(
            """
            SELECT sequence, length(text) AS text_chars
            FROM desktop_session_turns
            WHERE session_id = ? ORDER BY sequence ASC
            """,
            (session_id,),
        ).fetchall()
        total_chars = sum(max(0, int(row["text_chars"] or 0)) for row in rows)
        remove: list[int] = []
        index = 0
        while (
            len(rows) - index > self._max_turns or total_chars > self._max_session_chars
        ):
            row = rows[index]
            remove.append(int(row["sequence"]))
            total_chars -= max(0, int(row["text_chars"] or 0))
            index += 1
        if not remove:
            return
        placeholders = ",".join("?" for _item in remove)
        connection.execute(
            f"""
            DELETE FROM desktop_session_turns
            WHERE session_id = ? AND sequence IN ({placeholders})
            """,
            (session_id, *remove),
        )
        connection.execute(
            """
            UPDATE desktop_sessions
            SET dropped_turns = dropped_turns + ?
            WHERE session_id = ?
            """,
            (len(remove), session_id),
        )

    def _prune_sessions_locked(self, connection: sqlite3.Connection) -> None:
        rows = connection.execute(
            """
            SELECT session_id FROM desktop_sessions
            WHERE is_current = 0
            ORDER BY updated_at DESC, created_at DESC, session_id DESC
            """
        ).fetchall()
        # The current session consumes one slot.  Never remove it.
        keep_archived = max(0, self._max_sessions - 1)
        for row in rows[keep_archived:]:
            connection.execute(
                "DELETE FROM desktop_sessions WHERE session_id = ?",
                (row["session_id"],),
            )


def _positive_bound(name: str, value: Any) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise DesktopSessionValidationError(f"{name} must be a positive integer")
    return value


def _session_id(value: Any) -> str:
    if not isinstance(value, str) or not _SESSION_ID_RE.fullmatch(value):
        raise DesktopSessionValidationError(
            "session id must contain only letters, digits, underscore, or hyphen"
        )
    return value


def _session_mode(value: Any) -> SessionMode:
    if value not in SESSION_MODES:
        raise DesktopSessionValidationError(
            "session_mode must be interactive or production"
        )
    return value


def _generation_mode(value: Any) -> GenerationMode:
    if value not in GENERATION_MODES:
        raise DesktopSessionValidationError("generation_mode must be fast or deep")
    return value


def _session_role(value: Any) -> SessionRole:
    if value not in SESSION_ROLES:
        raise DesktopSessionValidationError(
            "turn role must be user, assistant, system, or tool"
        )
    return value


def _format_datetime(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_datetime(value: Any) -> datetime:
    if not isinstance(value, str):
        raise DesktopSessionStorageError("Stored desktop session time is invalid.")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise DesktopSessionStorageError(
            "Stored desktop session time is invalid."
        ) from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _safe_provenance(
    value: Mapping[str, Any],
    *,
    workspace_root: Path,
    supplied_secrets: tuple[str, ...],
    max_bytes: int,
) -> dict[str, Any]:
    projected = _project_value(
        value,
        workspace_root=workspace_root,
        supplied_secrets=supplied_secrets,
        depth=0,
    )
    if not isinstance(projected, dict):
        projected = {}
    encoded = json.dumps(
        projected,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    if len(encoded) > max_bytes:
        return {"_truncated": True}
    return projected


def _project_value(
    value: Any,
    *,
    workspace_root: Path,
    supplied_secrets: tuple[str, ...],
    depth: int,
) -> Any:
    if depth > 8:
        return "[truncated]"
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, str):
        return redact_sensitive_text(value, supplied_secrets, max_chars=8_000)
    if isinstance(value, Path):
        candidate = value.expanduser()
        if not candidate.is_absolute():
            candidate = workspace_root / candidate
        candidate = candidate.resolve(strict=False)
        try:
            return candidate.relative_to(workspace_root).as_posix()
        except ValueError:
            return "[external-path]"
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return _format_datetime(value)
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, (bytes, bytearray, memoryview)):
        return f"[bytes:{len(value)}]"
    if isinstance(value, Mapping):
        projected: dict[str, Any] = {}
        bounded_items = list(islice(value.items(), 129))
        for raw_key, item in bounded_items[:128]:
            key = raw_key if isinstance(raw_key, str) else f"<{type(raw_key).__name__}>"
            key = redact_sensitive_text(key, supplied_secrets, max_chars=256)
            if _SECRET_KEY_RE.search(key):
                projected[key] = "[redacted]"
            else:
                projected[key] = _project_value(
                    item,
                    workspace_root=workspace_root,
                    supplied_secrets=supplied_secrets,
                    depth=depth + 1,
                )
        if len(bounded_items) > 128:
            projected["_truncated"] = True
        return projected
    if isinstance(value, (list, tuple)):
        projected_items = [
            _project_value(
                item,
                workspace_root=workspace_root,
                supplied_secrets=supplied_secrets,
                depth=depth + 1,
            )
            for item in value[:128]
        ]
        if len(value) > 128:
            projected_items.append("[truncated]")
        return projected_items
    return f"<{type(value).__name__}>"


def _copy_json(value: Any) -> Any:
    return json.loads(json.dumps(value, ensure_ascii=False))


__all__ = [
    "DEFAULT_LIST_LIMIT",
    "DEFAULT_MAX_PROVENANCE_BYTES",
    "DEFAULT_MAX_SESSIONS",
    "DEFAULT_MAX_SESSION_CHARS",
    "DEFAULT_MAX_TURN_CHARS",
    "DEFAULT_MAX_TURNS",
    "DesktopSessionError",
    "DesktopSessionNotFoundError",
    "DesktopSessionSnapshot",
    "DesktopSessionStorageError",
    "DesktopSessionStore",
    "DesktopSessionSummary",
    "DesktopSessionTurn",
    "DesktopSessionValidationError",
    "GENERATION_MODES",
    "GenerationMode",
    "SESSION_MODES",
    "SESSION_ROLES",
    "SessionMode",
    "SessionRole",
]
