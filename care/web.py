"""Native, dependency-free HTTP surface for Maestro's browser UI.

The server intentionally uses :class:`http.server.ThreadingHTTPServer`: the
browser UI remains available while generation/execution run on their own daemon
threads and while one or more Server-Sent Events connections are open.

Security model
--------------

This is a *local* workbench, not an Internet-facing service.  The server binds
only to literal loopback addresses/``localhost`` and rejects non-loopback Host
headers (DNS-rebinding fence).  Mutating requests must be JSON, reject a
cross-origin ``Origin``/``Sec-Fetch-Site``, and every response carries a strict
CSP plus common browser hardening headers.  No configuration or provider key is
included in API snapshots.

Active run objects and their event buffers are bounded, process-local, and
volatile.  Workspace conversation sessions are separately persisted by
``DesktopSessionStore`` in bounded SQLite storage.  Event payloads are
projected through :func:`json_safe` before entering volatile history, which
handles Pydantic/dataclass/runtime objects, cycles, non-finite floats, and
secret-looking dictionary fields without falling back to unsafe JSON encoding.
"""

from __future__ import annotations

import asyncio
import copy
import dataclasses
import hashlib
import hmac
import inspect
import ipaddress
import itertools
import json
import logging
import math
import mimetypes
import os
import secrets
import socket
import sys
import tempfile
import threading
import time
import uuid
import webbrowser
from collections import OrderedDict, deque
from collections.abc import Awaitable, Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager, nullcontext
from datetime import date, datetime, timezone
from enum import Enum
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, TextIO
from urllib.parse import parse_qs, unquote, urlsplit

from care.desktop_commands import CommandBridge
from care.desktop_command_jobs import (
    CommandJobCapacityError,
    CommandJobClosedError,
    CommandJobError,
    CommandJobNotFound,
    CommandJobResultUnavailable,
    DesktopCommandJobManager,
    raise_if_cancelled,
)
from care.desktop_sessions import (
    DesktopSessionError,
    DesktopSessionNotFoundError,
    DesktopSessionStorageError,
    DesktopSessionStore,
    DesktopSessionValidationError,
)
from care.desktop_settings import DesktopSettingsBridge, SettingsLoadError
from care.redaction import redact_sensitive_text


_log = logging.getLogger("care.web")

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8787
MAX_REQUEST_BYTES = 64 * 1024
MAX_COMMAND_REQUEST_BYTES = 4 * 1024 * 1024 + MAX_REQUEST_BYTES
MAX_PROMPT_CHARS = 32_000
MAX_INPUT_CHARS = 32_000
MAX_SESSION_CONTEXT_CHARS = 48_000
MAX_STATIC_BYTES = 16 * 1024 * 1024
MAX_WORKSPACE_IMAGE_BYTES = 16 * 1024 * 1024
MAX_WORKSPACE_TEXT_BYTES = 512 * 1024
MAX_WORKSPACE_PATH_CHARS = 1_024
MAX_JSON_DEPTH = 10
MAX_JSON_ITEMS = 512
MAX_JSON_STRING_CHARS = 128 * 1024
DEFAULT_MAX_HISTORY = 64
DEFAULT_MAX_EVENTS = 1_024
DEFAULT_CANCEL_GRACE_SECONDS = 0.75
MAX_PRODUCTION_STATE_FILES = 2_048
MAX_PRODUCTION_STATE_BYTES = 32 * 1024 * 1024
SSE_HEARTBEAT_SECONDS = 15.0
SESSION_COOKIE_NAME = "maestro_session"

_WORKSPACE_IMAGE_MIME = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".bmp": "image/bmp",
}

TERMINAL_STATUSES = frozenset({"completed", "failed", "cancelled"})

_CSP = (
    "default-src 'self'; "
    "script-src 'self'; "
    "style-src 'self'; "
    "connect-src 'self'; "
    "img-src 'self' data:; "
    "font-src 'self'; "
    "object-src 'none'; "
    "base-uri 'none'; "
    "frame-ancestors 'none'; "
    "form-action 'self'"
)

_SECRET_KEYS = frozenset(
    {
        "api_key",
        "apikey",
        "authorization",
        "password",
        "secret",
        "access_token",
        "refresh_token",
        "client_secret",
    }
)


class WebError(RuntimeError):
    """Base class for errors that have a stable HTTP representation."""

    status = HTTPStatus.BAD_REQUEST
    code = "bad_request"


class RunNotFoundError(WebError):
    status = HTTPStatus.NOT_FOUND
    code = "run_not_found"


class SessionNotFoundError(WebError):
    status = HTTPStatus.NOT_FOUND
    code = "session_not_found"


class SessionStateError(WebError):
    status = HTTPStatus.INTERNAL_SERVER_ERROR
    code = "session_state_error"


class CommandJobNotFoundError(WebError):
    status = HTTPStatus.NOT_FOUND
    code = "command_job_not_found"


class CommandJobConflictError(WebError):
    status = HTTPStatus.CONFLICT
    code = "command_job_conflict"


class CommandJobCapacityWebError(WebError):
    status = HTTPStatus.TOO_MANY_REQUESTS
    code = "command_job_capacity"


class CommandJobUnavailableError(WebError):
    status = HTTPStatus.SERVICE_UNAVAILABLE
    code = "command_jobs_unavailable"


class RunCapacityError(WebError):
    status = HTTPStatus.SERVICE_UNAVAILABLE
    code = "run_capacity"


class RunConflictError(WebError):
    status = HTTPStatus.CONFLICT
    code = "run_conflict"


class RequestValidationError(WebError):
    code = "invalid_request"


class UnsupportedMediaTypeError(WebError):
    status = HTTPStatus.UNSUPPORTED_MEDIA_TYPE
    code = "unsupported_media_type"


class WorkspaceWriteError(WebError):
    status = HTTPStatus.INTERNAL_SERVER_ERROR
    code = "workspace_write_failed"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _is_secret_key(key: Any) -> bool:
    normalised = str(key).strip().lower().replace("-", "_")
    return normalised in _SECRET_KEYS or normalised.endswith("_api_key")


def json_safe(
    value: Any,
    *,
    max_depth: int = MAX_JSON_DEPTH,
    max_items: int = MAX_JSON_ITEMS,
    max_string_chars: int = MAX_JSON_STRING_CHARS,
    redact_strings: bool = False,
) -> Any:
    """Return a bounded value accepted by strict ``json.dumps``.

    Unknown objects become their type name plus ``str(value)`` rather than a
    traversal of arbitrary private attributes.  This both avoids surprising
    serializer crashes and keeps provider clients/configuration out of API
    responses.  Secret-looking mapping fields are always redacted.
    """

    seen: set[int] = set()

    def _convert(item: Any, depth: int) -> Any:
        if item is None or isinstance(item, (bool, int)):
            return item
        if isinstance(item, float):
            return item if math.isfinite(item) else None
        if isinstance(item, str):
            if redact_strings:
                item = redact_sensitive_text(item)
            if len(item) <= max_string_chars:
                return item
            return item[:max_string_chars] + "… [truncated]"
        if isinstance(item, (bytes, bytearray, memoryview)):
            text = bytes(item).decode("utf-8", errors="replace")
            return _convert(text, depth)
        if isinstance(item, (datetime, date)):
            return item.isoformat()
        if isinstance(item, Path):
            return str(item)
        if isinstance(item, Enum):
            return _convert(item.value, depth)
        if isinstance(item, BaseException):
            return {
                "type": type(item).__name__,
                "message": _convert(
                    redact_sensitive_text(item),
                    depth + 1,
                ),
            }
        if depth >= max_depth:
            return f"<{type(item).__name__}: max depth reached>"

        identity = id(item)
        if isinstance(
            item, (Mapping, Sequence, set, frozenset)
        ) or dataclasses.is_dataclass(item):
            if identity in seen:
                return "<circular reference>"
            seen.add(identity)
            try:
                if isinstance(item, Mapping):
                    result: dict[str, Any] = {}
                    for index, (key, nested) in enumerate(item.items()):
                        if index >= max_items:
                            result["__truncated__"] = (
                                f"{len(item) - max_items} additional items"
                                if hasattr(item, "__len__")
                                else "additional items"
                            )
                            break
                        text_key = str(key)
                        result[text_key] = (
                            "[redacted]"
                            if _is_secret_key(text_key)
                            else _convert(nested, depth + 1)
                        )
                    return result
                if dataclasses.is_dataclass(item) and not isinstance(item, type):
                    return {
                        field.name: _convert(getattr(item, field.name), depth + 1)
                        for field in dataclasses.fields(item)[:max_items]
                        if not field.name.startswith("_")
                        and not _is_secret_key(field.name)
                    }
                if isinstance(item, (set, frozenset)):
                    values = sorted(item, key=lambda candidate: repr(candidate))
                else:
                    values = item
                projected = [
                    _convert(nested, depth + 1)
                    for nested in itertools.islice(values, max_items)
                ]
                if len(item) > max_items:
                    projected.append(f"<{len(item) - max_items} additional items>")
                return projected
            finally:
                seen.discard(identity)

        # Pydantic v2/v1 and runtime models commonly expose a deliberate
        # serialisation method.  Prefer that public contract over __dict__.
        model_dump = getattr(item, "model_dump", None)
        if callable(model_dump):
            if identity in seen:
                return "<circular reference>"
            seen.add(identity)
            try:
                try:
                    dumped = model_dump(mode="json")
                except (TypeError, ValueError):
                    dumped = model_dump()
                return _convert(dumped, depth + 1)
            except Exception:  # noqa: BLE001 - safe fallback below
                pass
            finally:
                seen.discard(identity)

        to_dict = getattr(item, "to_dict", None)
        if callable(to_dict):
            if identity in seen:
                return "<circular reference>"
            seen.add(identity)
            try:
                return _convert(to_dict(), depth + 1)
            except Exception:  # noqa: BLE001 - safe fallback below
                pass
            finally:
                seen.discard(identity)

        try:
            text = str(item)
        except Exception:  # noqa: BLE001
            text = ""
        if not text or text.startswith("<") and text.endswith(">"):
            return f"<{type(item).__name__}>"
        return _convert(text, depth + 1)

    return _convert(value, 0)


def _json_bytes(value: Any) -> bytes:
    return json.dumps(
        json_safe(value),
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
    ).encode("utf-8")


def _public_message_fields(message: Any) -> dict[str, Any]:
    fields: dict[str, Any] = {}
    for name in getattr(message, "__dataclass_fields__", {}) or {}:
        if name.startswith("_") or name == "future":
            continue
        try:
            fields[name] = getattr(message, name)
        except Exception:  # noqa: BLE001
            continue
    if fields:
        return fields
    try:
        values = vars(message)
    except TypeError:
        return {}
    return {
        name: value
        for name, value in values.items()
        if not name.startswith("_") and name != "future"
    }


def map_runtime_message(
    message: Any,
    *,
    source: str,
) -> tuple[str, dict[str, Any]]:
    """Map a ``MagePoster``/``CarlStreamer`` message to a web event.

    Mapping by class name keeps ``care.web`` importable when optional runtime
    packages are absent and gives tests a light duck-typed seam.
    """

    name = type(message).__name__

    def get(field: str, default: Any = None) -> Any:
        return getattr(message, field, default)

    if source == "generation":
        mapping: dict[str, tuple[str, dict[str, Any]]] = {
            "StageStarted": (
                "generation.stage.started",
                {"stage": get("stage", "")},
            ),
            "StageCompleted": (
                "generation.stage.completed",
                {"stage": get("stage", ""), "result": get("result")},
            ),
            "StageError": (
                "generation.stage.error",
                {
                    "stage": get("stage", ""),
                    "error": redact_sensitive_text(get("error", "")),
                },
            ),
            "LLMChunk": (
                "generation.chunk",
                {"stage": get("stage", ""), "delta": get("delta", "")},
            ),
            "StageProgress": (
                "generation.stage.progress",
                {"stage": get("stage", ""), "artifact": get("artifact")},
            ),
            "StageRetry": (
                "generation.stage.retry",
                {
                    "stage": get("stage", ""),
                    "attempt": get("attempt", 0),
                    "error": redact_sensitive_text(get("error", "")),
                },
            ),
            "CostEstimate": (
                "generation.cost",
                {"estimate": get("estimate")},
            ),
        }
        if name in mapping:
            event_type, data = mapping[name]
            return event_type, json_safe(data, redact_strings=True)

    if source == "execution":
        mapping = {
            "StepStarted": (
                "execution.step.started",
                {
                    "step_number": get("step_number"),
                    "step_title": get("step_title", ""),
                },
            ),
            "StepCompleted": (
                "execution.step.completed",
                _step_completed_payload(get("result")),
            ),
            "ChainCompleted": (
                "execution.chain.completed",
                {"result": get("result")},
            ),
            "Progress": (
                "execution.progress",
                {"completed": get("completed", 0), "total": get("total", 0)},
            ),
            "LlmChunk": (
                "execution.chunk",
                {
                    "chunk": get("chunk", ""),
                    "step_number": get("step_number"),
                    "stage": get("stage"),
                },
            ),
            "StepEvent": (
                "execution.step.event",
                {
                    "step_number": get("step_number"),
                    "event_type": get("event_type", ""),
                    "payload": get("payload", {}),
                },
            ),
        }
        if name in mapping:
            event_type, data = mapping[name]
            return event_type, json_safe(data, redact_strings=True)

    fallback = "generation.event" if source == "generation" else "execution.event"
    return fallback, {
        "kind": name,
        "payload": json_safe(
            _public_message_fields(message),
            redact_strings=True,
        ),
    }


def _step_completed_payload(result: Any) -> dict[str, Any]:
    def read(name: str, default: Any = None) -> Any:
        if isinstance(result, Mapping):
            return result.get(name, default)
        return getattr(result, name, default)

    return {
        "step_number": read("step_number", read("number")),
        "step_title": read("step_title", read("title", "")),
        "success": read("success"),
        "error": read("error"),
        "result": result,
    }


@dataclasses.dataclass(slots=True)
class _PendingInput:
    request_id: str
    prompt: str
    future: Any
    loop: asyncio.AbstractEventLoop


@dataclasses.dataclass(slots=True)
class _RunRecord:
    id: str
    prompt: str
    generation_prompt: str
    mode: str
    session_mode: str
    session_id: str | None
    execute: bool
    created_at: str
    status: str = "queued"
    started_at: str | None = None
    finished_at: str | None = None
    result: Any = None
    error: str | None = None
    cancel_requested: bool = False
    event_sequence: int = 0
    events: deque[dict[str, Any]] = dataclasses.field(default_factory=deque)
    pending_inputs: OrderedDict[str, _PendingInput] = dataclasses.field(
        default_factory=OrderedDict,
    )
    loop: asyncio.AbstractEventLoop | None = None
    cancel_event: asyncio.Event | None = None
    task: asyncio.Task[Any] | None = None
    thread: threading.Thread | None = None
    cancel_callbacks: list[Callable[[], Any]] = dataclasses.field(
        default_factory=list,
    )


class WebRunContext:
    """The narrow, testable contract supplied to a ``RunManager`` runner."""

    def __init__(self, manager: "RunManager", run_id: str) -> None:
        self._manager = manager
        self.id = run_id

    @property
    def prompt(self) -> str:
        return self._manager._record_value(self.id, "prompt")

    @property
    def generation_prompt(self) -> str:
        """Prompt enriched with bounded prior turns for chain generation only."""

        return self._manager._record_value(self.id, "generation_prompt")

    @property
    def mode(self) -> str:
        return self._manager._record_value(self.id, "mode")

    @property
    def session_mode(self) -> str:
        return self._manager._record_value(self.id, "session_mode")

    @property
    def session_id(self) -> str | None:
        return self._manager._record_value(self.id, "session_id")

    @property
    def execute(self) -> bool:
        return self._manager._record_value(self.id, "execute")

    @property
    def cancel_event(self) -> asyncio.Event:
        event = self._manager._record_value(self.id, "cancel_event")
        if event is None:
            raise RuntimeError("run cancellation event is not initialised")
        return event

    @property
    def cancelled(self) -> bool:
        return self.cancel_event.is_set()

    def emit(self, event_type: str, data: Any = None) -> dict[str, Any]:
        return self._manager.emit(self.id, event_type, data)

    def sink(self, source: str) -> "RuntimeEventSink":
        return RuntimeEventSink(self._manager, self.id, source=source)

    def add_cancel_callback(self, callback: Callable[[], Any]) -> None:
        """Register cooperative cancellation to run before forced Task.cancel."""

        self._manager._add_cancel_callback(self.id, callback)

    async def request_human_input(self, prompt: str) -> str:
        """Create a loop-owned future and expose it through the HTTP API."""

        loop = asyncio.get_running_loop()
        future: asyncio.Future[str] = loop.create_future()
        self._manager._register_human_input(self.id, prompt, future, loop)
        return await future


class RuntimeEventSink:
    """Duck-typed Textual sink used by ``MagePoster`` and ``CarlStreamer``."""

    def __init__(
        self,
        manager: "RunManager",
        run_id: str,
        *,
        source: str,
    ) -> None:
        if source not in {"generation", "execution"}:
            raise ValueError("source must be 'generation' or 'execution'")
        self._manager = manager
        self._run_id = run_id
        self._source = source

    def post_message(self, message: Any) -> bool:
        if (
            self._source == "execution"
            and type(message).__name__ == "HumanInputRequested"
        ):
            future = getattr(message, "future", None)
            prompt = str(getattr(message, "prompt", ""))
            loop = self._manager._record_value(self._run_id, "loop")
            if loop is None or not loop.is_running():
                self._manager.emit(
                    self._run_id,
                    "execution.event",
                    {
                        "kind": "HumanInputRequested",
                        "error": "callback did not run in the owner event loop",
                    },
                )
                return False
            try:
                current_loop = asyncio.get_running_loop()
            except RuntimeError:
                current_loop = None
            if current_loop is loop:
                self._manager._register_human_input(
                    self._run_id,
                    prompt,
                    future,
                    loop,
                )
            else:
                loop.call_soon_threadsafe(
                    self._manager._register_human_input,
                    self._run_id,
                    prompt,
                    future,
                    loop,
                )
            return True

        event_type, data = map_runtime_message(message, source=self._source)
        self._manager.emit(self._run_id, event_type, data)
        return True


Runner = Callable[[WebRunContext], Awaitable[Any] | Any]


class RunManager:
    """Thread-safe owner of bounded, volatile in-memory web runs."""

    def __init__(
        self,
        *,
        runner: Runner | None = None,
        session_store: DesktopSessionStore | None = None,
        max_history: int = DEFAULT_MAX_HISTORY,
        max_events: int = DEFAULT_MAX_EVENTS,
        cancel_grace_seconds: float = DEFAULT_CANCEL_GRACE_SECONDS,
    ) -> None:
        if max_history < 1:
            raise ValueError("max_history must be at least 1")
        if max_events < 1:
            raise ValueError("max_events must be at least 1")
        if (
            isinstance(cancel_grace_seconds, bool)
            or not isinstance(cancel_grace_seconds, (int, float))
            or not math.isfinite(cancel_grace_seconds)
            or cancel_grace_seconds < 0
        ):
            raise ValueError("cancel_grace_seconds must be a finite non-negative number")
        self._runner = runner or default_async_runner
        self._session_store = session_store
        self._max_history = max_history
        self._max_events = max_events
        self._cancel_grace_seconds = float(cancel_grace_seconds)
        self._records: OrderedDict[str, _RunRecord] = OrderedDict()
        self._condition = threading.Condition(threading.RLock())
        self._closed = False

    @property
    def max_history(self) -> int:
        return self._max_history

    @property
    def session_store(self) -> DesktopSessionStore | None:
        return self._session_store

    def attach_session_store(self, session_store: DesktopSessionStore) -> None:
        """Bind persistence before the first run is accepted by this manager."""

        if not isinstance(session_store, DesktopSessionStore):
            raise TypeError("session_store must be a DesktopSessionStore")
        with self._condition:
            if self._records:
                raise RunConflictError(
                    "a session store cannot be attached after runs have started"
                )
            if self._session_store not in {None, session_store}:
                raise RunConflictError("run manager already has a session store")
            self._session_store = session_store

    def submit(
        self,
        prompt: str,
        *,
        mode: str = "fast",
        session_mode: str = "interactive",
        execute: bool = True,
    ) -> dict[str, Any]:
        if not isinstance(prompt, str) or not prompt.strip():
            raise RequestValidationError("prompt must be a non-empty string")
        if len(prompt) > MAX_PROMPT_CHARS:
            raise RequestValidationError(
                f"prompt exceeds {MAX_PROMPT_CHARS} characters",
            )
        prompt = prompt.strip()
        if mode not in {"fast", "deep"}:
            raise RequestValidationError("mode must be 'fast' or 'deep'")
        if session_mode not in {"interactive", "production"}:
            raise RequestValidationError(
                "session_mode must be 'interactive' or 'production'"
            )
        if not isinstance(execute, bool):
            raise RequestValidationError("execute must be a boolean")

        with self._condition:
            if self._closed:
                raise RunConflictError("run manager is shutting down")
            self._make_capacity_locked()
            run_id = uuid.uuid4().hex
            session_id: str | None = None
            generation_prompt = prompt
            if self._session_store is not None:
                try:
                    session = self._session_store.current_session()
                    if session.session_mode != session_mode:
                        session = self._session_store.new_session(
                            session_mode=session_mode,
                            generation_mode=mode,
                        )
                    elif session.generation_mode != mode:
                        session = self._session_store.set_modes(
                            generation_mode=mode,
                        )
                    session_id = session.session_id
                    generation_prompt = _build_session_generation_prompt(
                        session.turns,
                        prompt,
                    )
                    self._session_store.append_user(
                        prompt,
                        session_id=session_id,
                        provenance={"run_id": run_id, "status": "created"},
                    )
                except DesktopSessionError as exc:
                    raise _translate_session_error(exc) from exc
            record = _RunRecord(
                id=run_id,
                prompt=prompt,
                generation_prompt=generation_prompt,
                mode=mode,
                session_mode=session_mode,
                session_id=session_id,
                execute=execute,
                created_at=_utc_now(),
                events=deque(maxlen=self._max_events),
            )
            self._records[run_id] = record
            self._append_event_locked(
                record,
                "run.created",
                {
                    "prompt": prompt,
                    "mode": mode,
                    "session_mode": session_mode,
                    "session_id": session_id,
                    "execute": execute,
                },
            )
            thread = threading.Thread(
                target=self._thread_main,
                args=(run_id,),
                name=f"maestro-web-run-{run_id[:8]}",
                daemon=True,
            )
            record.thread = thread
            snapshot = self._snapshot_locked(record)
            thread.start()
            return snapshot

    # Friendly aliases for callers/tests that prefer explicit verbs.
    create_run = submit

    def list_runs(self) -> list[dict[str, Any]]:
        with self._condition:
            return [
                self._snapshot_locked(record)
                for record in reversed(self._records.values())
            ]

    def get_run(self, run_id: str) -> dict[str, Any]:
        with self._condition:
            return self._snapshot_locked(self._require_locked(run_id))

    def emit(self, run_id: str, event_type: str, data: Any = None) -> dict[str, Any]:
        if not isinstance(event_type, str) or not event_type.strip():
            raise ValueError("event_type must be a non-empty string")
        with self._condition:
            record = self._require_locked(run_id)
            return self._append_event_locked(record, event_type.strip(), data)

    def get_events(self, run_id: str, *, after: int = 0) -> list[dict[str, Any]]:
        with self._condition:
            record = self._require_locked(run_id)
            return [dict(event) for event in record.events if event["seq"] > after]

    def wait_for_events(
        self,
        run_id: str,
        *,
        after: int,
        timeout: float,
    ) -> tuple[list[dict[str, Any]], bool]:
        deadline = time.monotonic() + max(timeout, 0.0)
        with self._condition:
            while True:
                record = self._require_locked(run_id)
                events = [
                    dict(event) for event in record.events if event["seq"] > after
                ]
                terminal = record.status in TERMINAL_STATUSES
                if events or terminal:
                    return events, terminal
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return [], False
                self._condition.wait(remaining)

    def cancel_run(self, run_id: str) -> dict[str, Any]:
        """Idempotently request cancellation on the run's owner event loop."""

        with self._condition:
            record = self._require_locked(run_id)
            if record.status in TERMINAL_STATUSES or record.cancel_requested:
                return self._snapshot_locked(record)
            record.cancel_requested = True
            loop = record.loop
            self._condition.notify_all()
        if loop is not None and loop.is_running():
            loop.call_soon_threadsafe(self._cancel_on_owner_loop, run_id)
        return self.get_run(run_id)

    cancel = cancel_run

    def provide_human_input(
        self,
        run_id: str,
        value: str,
        *,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        if not isinstance(value, str):
            raise RequestValidationError("value must be a string")
        if len(value) > MAX_INPUT_CHARS:
            raise RequestValidationError(
                f"value exceeds {MAX_INPUT_CHARS} characters",
            )
        with self._condition:
            record = self._require_locked(run_id)
            if record.status in TERMINAL_STATUSES:
                raise RunConflictError("run is already terminal")
            if request_id is None:
                if not record.pending_inputs:
                    raise RunConflictError("run is not waiting for human input")
                request_id = next(iter(record.pending_inputs))
            pending = record.pending_inputs.get(request_id)
            if pending is None:
                raise RunConflictError(f"unknown human-input request: {request_id}")
            # Pop before scheduling so two HTTP threads cannot resolve one future.
            record.pending_inputs.pop(request_id, None)
            record.status = "waiting_for_input" if record.pending_inputs else "running"
            self._append_event_locked(
                record,
                "human_input.resolved",
                {"request_id": request_id},
            )
            snapshot = self._snapshot_locked(record)

        def _resolve() -> None:
            future = pending.future
            try:
                if future is not None and not future.done():
                    future.set_result(value)
            except Exception as exc:  # noqa: BLE001
                _log.debug("human input future resolution failed: %s", exc)

        if not pending.loop.is_running():
            raise RunConflictError("run event loop is no longer active")
        pending.loop.call_soon_threadsafe(_resolve)
        return snapshot

    provide_input = provide_human_input

    def wait(self, run_id: str, *, timeout: float = 10.0) -> dict[str, Any]:
        deadline = time.monotonic() + max(timeout, 0.0)
        with self._condition:
            while True:
                record = self._require_locked(run_id)
                if record.status in TERMINAL_STATUSES:
                    return self._snapshot_locked(record)
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return self._snapshot_locked(record)
                self._condition.wait(remaining)

    def stats(self) -> dict[str, int]:
        with self._condition:
            active = sum(
                record.status not in TERMINAL_STATUSES
                for record in self._records.values()
            )
            return {
                "active": active,
                "retained": len(self._records),
                "capacity": self._max_history,
            }

    def shutdown(self, *, wait: bool = False, timeout: float = 2.0) -> None:
        with self._condition:
            self._closed = True
            active_ids = [
                record.id
                for record in self._records.values()
                if record.status not in TERMINAL_STATUSES
            ]
            threads = [
                record.thread for record in self._records.values() if record.thread
            ]
        for run_id in active_ids:
            self.cancel_run(run_id)
        if wait:
            deadline = time.monotonic() + max(timeout, 0.0)
            for thread in threads:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                thread.join(remaining)

    def _record_value(self, run_id: str, field: str) -> Any:
        with self._condition:
            return getattr(self._require_locked(run_id), field)

    def _add_cancel_callback(
        self,
        run_id: str,
        callback: Callable[[], Any],
    ) -> None:
        if not callable(callback):
            raise TypeError("cancel callback must be callable")
        with self._condition:
            record = self._require_locked(run_id)
            if record.status in TERMINAL_STATUSES:
                return
            if not record.cancel_requested:
                record.cancel_callbacks.append(callback)
                return
            loop = record.loop

        # Cancellation may have copied and cleared the callback list just
        # before a runtime component finished constructing itself. Deliver the
        # already-active signal instead of waiting for the forced-cancel timer.
        try:
            current_loop = asyncio.get_running_loop()
        except RuntimeError:
            current_loop = None
        if current_loop is loop:
            self._invoke_cancel_callback(callback)
        elif loop is not None and loop.is_running():
            try:
                loop.call_soon_threadsafe(self._invoke_cancel_callback, callback)
            except RuntimeError:
                self._invoke_cancel_callback(callback)
        else:
            self._invoke_cancel_callback(callback)

    @staticmethod
    def _invoke_cancel_callback(callback: Callable[[], Any]) -> None:
        try:
            callback()
        except Exception as exc:  # noqa: BLE001
            _log.debug("cooperative run cancellation failed: %s", exc)

    def _thread_main(self, run_id: str) -> None:
        try:
            asyncio.run(self._drive_run(run_id))
        except Exception:  # noqa: BLE001 - last-resort daemon-thread guard
            _log.exception("unexpected web run thread failure: %s", run_id)
            self._finish(run_id, "failed", error="internal run-thread failure")

    async def _drive_run(self, run_id: str) -> None:
        loop = asyncio.get_running_loop()
        cancel_event = asyncio.Event()
        with self._condition:
            record = self._require_locked(run_id)
            record.loop = loop
            record.cancel_event = cancel_event
            if record.cancel_requested:
                cancel_event.set()  # owner loop
                cancelled_before_start = True
            else:
                cancelled_before_start = False
                record.status = "running"
                record.started_at = _utc_now()
                self._append_event_locked(record, "run.started", {})

        if cancelled_before_start:
            self._finish(run_id, "cancelled")
            return

        context = WebRunContext(self, run_id)
        task = loop.create_task(self._invoke_runner(context))
        with self._condition:
            record = self._require_locked(run_id)
            record.task = task
            if record.cancel_requested:
                loop.call_soon(self._cancel_on_owner_loop, run_id)
        try:
            result = await task
        except asyncio.CancelledError:
            self._finish(run_id, "cancelled")
        except BaseException as exc:  # runner failures become a stable API row
            if self._record_value(run_id, "cancel_requested"):
                self._finish(run_id, "cancelled")
            else:
                self._finish(
                    run_id,
                    "failed",
                    error=redact_sensitive_text(str(exc) or type(exc).__name__),
                )
        else:
            if self._record_value(run_id, "cancel_requested"):
                if _production_completed_after_cancel(result):
                    # A synchronous Platform submit may commit and return its
                    # receipt while the owner loop is unable to process the
                    # cancel signal. Completion wins; retain an explicit late
                    # cancel marker instead of presenting a committed effect as
                    # "stopped before completion".
                    self._finish(
                        run_id,
                        "completed",
                        result=_mark_late_cancel_after_commit(result),
                    )
                else:
                    # A cooperative runner may return a typed partial receipt
                    # after cancellation (notably a Production saga that
                    # already saved a chain). Preserve it instead of reducing
                    # the run to an information-free terminal marker.
                    self._finish(run_id, "cancelled", result=result)
            else:
                self._finish(run_id, "completed", result=result)
        finally:
            # Any abandoned human-input futures are loop-owned and are cancelled
            # here, before asyncio.run closes the owner loop.
            self._cancel_pending_on_owner_loop(run_id)
            with self._condition:
                record = self._records.get(run_id)
                if record is not None:
                    record.task = None
                    record.cancel_event = None
                    record.loop = None
                    record.cancel_callbacks.clear()
                    self._condition.notify_all()

    async def _invoke_runner(self, context: WebRunContext) -> Any:
        result = self._runner(context)
        if inspect.isawaitable(result):
            return await result
        return result

    def _cancel_on_owner_loop(self, run_id: str) -> None:
        """Run only inside the run's asyncio owner loop."""

        with self._condition:
            record = self._records.get(run_id)
            if record is None or record.status in TERMINAL_STATUSES:
                return
            cancel_event = record.cancel_event
            task = record.task
            callbacks = list(record.cancel_callbacks)
            record.cancel_callbacks.clear()
        if cancel_event is not None:
            cancel_event.set()
        for callback in callbacks:
            self._invoke_cancel_callback(callback)
        self._cancel_pending_on_owner_loop(run_id)
        if task is not None and not task.done():
            if self._cancel_grace_seconds == 0:
                task.cancel()
            else:
                asyncio.get_running_loop().call_later(
                    self._cancel_grace_seconds,
                    self._force_cancel_on_owner_loop,
                    run_id,
                    task,
                )

    def _force_cancel_on_owner_loop(
        self,
        run_id: str,
        task: asyncio.Task[Any],
    ) -> None:
        """Force-cancel a runner that ignored the bounded cooperative window."""

        with self._condition:
            record = self._records.get(run_id)
            should_cancel = (
                record is not None
                and record.status not in TERMINAL_STATUSES
                and record.task is task
                and not task.done()
            )
        if should_cancel:
            task.cancel()

    def _cancel_pending_on_owner_loop(self, run_id: str) -> None:
        with self._condition:
            record = self._records.get(run_id)
            if record is None:
                return
            pending = list(record.pending_inputs.values())
            record.pending_inputs.clear()
        for item in pending:
            try:
                if item.future is not None and not item.future.done():
                    item.future.cancel()
            except Exception:  # noqa: BLE001
                pass

    def _register_human_input(
        self,
        run_id: str,
        prompt: str,
        future: Any,
        loop: asyncio.AbstractEventLoop,
    ) -> str:
        # This method is invoked by a callback running on `loop`; it never
        # resolves the future itself, so normal lock-protected bookkeeping is
        # safe. Resolution/cancellation always returns through call_soon_threadsafe.
        request_id = uuid.uuid4().hex
        with self._condition:
            record = self._require_locked(run_id)
            if record.status in TERMINAL_STATUSES or record.cancel_requested:

                def _cancel_future() -> None:
                    if future is not None and not future.done():
                        future.cancel()

                if loop.is_running():
                    loop.call_soon_threadsafe(_cancel_future)
                return request_id
            record.pending_inputs[request_id] = _PendingInput(
                request_id=request_id,
                prompt=str(prompt),
                future=future,
                loop=loop,
            )
            record.status = "waiting_for_input"
            self._append_event_locked(
                record,
                "human_input.requested",
                {"request_id": request_id, "prompt": str(prompt)},
            )
        add_done_callback = getattr(future, "add_done_callback", None)
        if callable(add_done_callback):
            add_done_callback(
                lambda _future: self._human_input_done(run_id, request_id),
            )
        return request_id

    def _human_input_done(self, run_id: str, request_id: str) -> None:
        """Drop a future that CARL timed out/cancelled without an HTTP reply."""

        with self._condition:
            record = self._records.get(run_id)
            if record is None or request_id not in record.pending_inputs:
                return
            record.pending_inputs.pop(request_id, None)
            if record.status == "waiting_for_input" and not record.pending_inputs:
                record.status = "running"
            self._append_event_locked(
                record,
                "human_input.expired",
                {"request_id": request_id},
            )

    def _finish(
        self,
        run_id: str,
        status: str,
        *,
        result: Any = None,
        error: str | None = None,
    ) -> None:
        with self._condition:
            record = self._records.get(run_id)
            if record is None or record.status in TERMINAL_STATUSES:
                return
            record.status = status
            record.finished_at = _utc_now()
            record.result = (
                json_safe(result, redact_strings=True)
                if status in {"completed", "cancelled"} and result is not None
                else None
            )
            record.error = redact_sensitive_text(error) if error else None
            if self._session_store is not None and record.session_id is not None:
                try:
                    provenance: dict[str, Any] = {
                        "run_id": run_id,
                        "status": status,
                    }
                    if (
                        isinstance(record.result, Mapping)
                        and record.result.get("cancel_requested_after_commit") is True
                    ):
                        provenance["cancel_requested_after_commit"] = True
                    pointer = _production_revision_pointer(record.result)
                    if pointer is not None:
                        provenance["production_revision_pointer"] = pointer
                    if status == "completed":
                        self._session_store.append_assistant(
                            _session_result_text(record.result),
                            session_id=record.session_id,
                            provenance=provenance,
                        )
                    elif status == "failed":
                        self._session_store.append_system(
                            f"Run failed: {record.error or 'run failed'}",
                            session_id=record.session_id,
                            provenance=provenance,
                        )
                    else:
                        self._session_store.append_system(
                            "Run cancelled.",
                            session_id=record.session_id,
                            provenance=provenance,
                        )
                except DesktopSessionError as exc:
                    _log.warning(
                        "desktop session terminal turn was not persisted: %s",
                        type(exc).__name__,
                    )
            if status in {"completed", "cancelled"} and record.result is not None:
                data = {"result": record.result}
            elif status == "failed":
                data = {"error": record.error or "run failed"}
            else:
                data = {}
            self._append_event_locked(record, f"run.{status}", data)

    def _make_capacity_locked(self) -> None:
        while len(self._records) >= self._max_history:
            evict_id = next(
                (
                    run_id
                    for run_id, record in self._records.items()
                    if record.status in TERMINAL_STATUSES
                ),
                None,
            )
            if evict_id is None:
                raise RunCapacityError(
                    "all in-memory run-history slots are active; try again later",
                )
            self._records.pop(evict_id, None)

    def _require_locked(self, run_id: str) -> _RunRecord:
        record = self._records.get(run_id)
        if record is None:
            raise RunNotFoundError(f"unknown run: {run_id}")
        return record

    def _append_event_locked(
        self,
        record: _RunRecord,
        event_type: str,
        data: Any,
    ) -> dict[str, Any]:
        record.event_sequence += 1
        event = {
            "seq": record.event_sequence,
            "type": event_type,
            "timestamp": _utc_now(),
            "data": json_safe(
                data if data is not None else {},
                redact_strings=True,
            ),
        }
        record.events.append(event)
        self._condition.notify_all()
        return dict(event)

    def _snapshot_locked(self, record: _RunRecord) -> dict[str, Any]:
        pending = [
            {
                "request_id": item.request_id,
                "prompt": redact_sensitive_text(item.prompt),
            }
            for item in record.pending_inputs.values()
        ]
        return {
            "id": record.id,
            "prompt": redact_sensitive_text(record.prompt),
            "mode": record.mode,
            "session_mode": record.session_mode,
            "session_id": record.session_id,
            "execute": record.execute,
            "status": record.status,
            "created_at": record.created_at,
            "started_at": record.started_at,
            "finished_at": record.finished_at,
            "result": json_safe(record.result),
            "error": record.error,
            "cancel_requested": record.cancel_requested,
            "event_count": record.event_sequence,
            "events_retained": len(record.events),
            "waiting_for_input": pending[0] if pending else None,
            "pending_inputs": pending,
        }


def _build_session_generation_prompt(turns: Sequence[Any], prompt: str) -> str:
    """Add the newest complete persisted turns without exposing them via run APIs."""

    selected: list[str] = []
    used = 0
    labels = {
        "user": "User",
        "assistant": "Assistant",
        "system": "System",
        "tool": "Tool",
    }
    for turn in reversed(tuple(turns)):
        role = getattr(turn, "role", None)
        text = getattr(turn, "text", None)
        if not isinstance(text, str) or not text:
            continue
        entry = f"{labels.get(str(role), 'Context')}: {text}"
        required = len(entry) + 2
        if required > MAX_SESSION_CONTEXT_CHARS - used:
            continue
        selected.append(entry)
        used += required
    if not selected:
        return prompt
    selected.reverse()
    return (
        "Previous conversation in this Maestro session:\n\n"
        + "\n\n".join(selected)
        + "\n\nCurrent request:\n"
        + prompt
    )


def _session_result_text(result: Any) -> str:
    """Project a completed run to the assistant text useful on the next turn."""

    if isinstance(result, str) and result.strip():
        return redact_sensitive_text(result, max_chars=128 * 1024)
    preferred = ("answer", "final_answer", "response", "output", "summary", "message")
    queue: deque[tuple[Any, int]] = deque([(result, 0)])
    seen: set[int] = set()
    while queue:
        value, depth = queue.popleft()
        if depth > 4 or not isinstance(value, Mapping):
            continue
        identity = id(value)
        if identity in seen:
            continue
        seen.add(identity)
        for key in preferred:
            candidate = value.get(key)
            if isinstance(candidate, str) and candidate.strip():
                return redact_sensitive_text(candidate, max_chars=128 * 1024)
        for candidate in value.values():
            if isinstance(candidate, Mapping):
                queue.append((candidate, depth + 1))
    return "Run completed successfully."


def _production_completed_after_cancel(result: Any) -> bool:
    """Recognise a fully committed Production result returned after cancel."""

    if not isinstance(result, Mapping):
        return False
    states: list[str] = []
    direct = result.get("production_state")
    if isinstance(direct, str):
        states.append(direct)
    production = result.get("production")
    if isinstance(production, Mapping):
        nested = production.get("state")
        if isinstance(nested, str):
            states.append(nested)
    return bool(states) and all(state == "completed" for state in states)


def _mark_late_cancel_after_commit(result: Any) -> Any:
    if not isinstance(result, Mapping):
        return result
    marked = dict(result)
    marked["cancel_requested_after_commit"] = True
    return marked


def _production_revision_pointer(result: Any) -> dict[str, str] | None:
    """Extract the safe Production lineage pointer persisted with a session.

    The pointer contains only a bounded Memory entity id and the fixed
    ``latest`` channel.  It lets the native composer route the next Agent-mode
    turn through the reviewed ``/revise`` preview/save flow instead of silently
    generating a second entity.
    """

    if not isinstance(result, Mapping):
        return None
    production = result.get("production")
    if not isinstance(production, Mapping):
        return None
    candidate = production.get("revision_pointer")
    if not isinstance(candidate, Mapping):
        return None
    entity_id = candidate.get("entity_id") or candidate.get("id")
    channel = candidate.get("channel") or "latest"
    if (
        not isinstance(entity_id, str)
        or not entity_id.strip()
        or len(entity_id) > 512
    ):
        return None
    if any(ord(char) < 32 or ord(char) == 127 for char in entity_id):
        return None
    if channel != "latest":
        return None
    return {"entity_id": entity_id, "channel": "latest"}


def _translate_session_error(error: DesktopSessionError) -> WebError:
    if isinstance(error, DesktopSessionNotFoundError):
        return SessionNotFoundError(str(error))
    if isinstance(error, DesktopSessionValidationError):
        return RequestValidationError(str(error))
    if isinstance(error, DesktopSessionStorageError):
        return SessionStateError("Desktop session storage is unavailable.")
    return SessionStateError("Desktop session state is unavailable.")


def _translate_command_job_error(error: BaseException) -> WebError:
    if isinstance(error, CommandJobNotFound):
        return CommandJobNotFoundError("Command job was not found.")
    if isinstance(error, CommandJobResultUnavailable):
        return CommandJobConflictError(str(error))
    if isinstance(error, CommandJobCapacityError):
        return CommandJobCapacityWebError(str(error))
    if isinstance(error, CommandJobClosedError):
        return CommandJobUnavailableError(str(error))
    if isinstance(error, (CommandJobError, ValueError)):
        return RequestValidationError(str(error))
    return CommandJobUnavailableError("Command job service is unavailable.")


def _is_export_command(command: Any) -> bool:
    if not isinstance(command, str):
        return False
    normalized = " ".join(command.strip().lower().split())
    return normalized == "/export" or normalized.startswith("/export ")


def _extract_chain_dict(result: Any) -> dict[str, Any]:
    candidate = getattr(result, "chain_dict", None)
    if isinstance(candidate, dict):
        return candidate
    if isinstance(result, dict):
        candidate = result.get("chain_dict")
        if isinstance(candidate, dict):
            return candidate
    chain = getattr(result, "chain", None)
    if isinstance(chain, dict):
        return chain
    to_dict = getattr(chain, "to_dict", None)
    if callable(to_dict):
        candidate = to_dict()
        if isinstance(candidate, dict):
            return candidate
    return {}


async def _prepare_chain_runtime(
    context: WebRunContext,
    config: Any,
    chain_dict: Mapping[str, Any],
    *,
    api: Any | None = None,
) -> dict[str, Any]:
    """Freeze compatibility/live-data rewrites before typed admission/save."""

    from care.runtime.llm_client import build_carl_llm_client
    from care.runtime.executor import normalize_tool_input_literals_in_dict
    from care.tool_planning import (
        augment_chain_for_live_data,
        downgrade_unsupported_step_types,
    )

    executable = copy.deepcopy(dict(chain_dict))
    downgrade_unsupported_step_types(executable)
    selected_api = api if api is not None else build_carl_llm_client(config.mage)

    # Match the TUI run path: an all-LLM plan that actually needs current data
    # gets routed to a registered/live tool before the typed chain is parsed.
    try:
        augmentation = await augment_chain_for_live_data(
            executable,
            task=context.prompt,
            api=selected_api,
            config=config,
        )
        if augmentation.get("rewrote"):
            executable = augmentation["chain_dict"]
            context.emit(
                "execution.routing",
                {
                    "step_number": augmentation.get("step_number"),
                    "step_title": augmentation.get("step_title"),
                    "tool_name": augmentation.get("tool_name"),
                    "reason": augmentation.get("reason"),
                },
            )
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 - parity path is best-effort
        context.emit(
            "execution.warning",
            {
                "message": redact_sensitive_text(
                    f"tool routing skipped: {exc}",
                )
            },
        )

    # CARL's current resolver requires literal tool inputs to be quoted. Do
    # this before Production validation/save, not as an execution-only
    # mutation, so Memory, baseline, and evolution share one graph snapshot.
    normalize_tool_input_literals_in_dict(executable)
    return executable


async def _execute_chain_runtime(
    context: WebRunContext,
    config: Any,
    chain_dict: Mapping[str, Any],
    *,
    prepared: bool = False,
    allow_tool_synthesis: bool = True,
    expected_state: str | None = None,
    state_guard: Callable[[], str] | None = None,
) -> tuple[Any, dict[str, Any]]:
    """Execute one generated chain through the same CARL seams as the TUI.

    Production passes an already-prepared mapping and disables late tool
    synthesis, so the admitted step graph consumed by Memory, execution, and
    evolution does not drift after save (Memory may still attach persistence
    metadata). ``state_guard`` binds configured/cached user tools; execution
    imports immutable verified copies of those files.
    """

    from care.runtime.carl_streamer import CarlStreamer
    from care.runtime.executor import build_run_context, execute_chain_async
    from care.runtime.llm_client import build_carl_llm_client

    try:
        from mmar_carl import ReasoningChain
    except ImportError as exc:
        raise RuntimeError(
            "the chain runtime is not installed; install maestro-care[carl]",
        ) from exc

    # Check the confirmed provider/tool snapshot before any configured user
    # tool module can be imported by ``build_run_context``.  The second check
    # below closes changes that happen while the runtime is being assembled.
    if expected_state is not None:
        if state_guard is None:
            raise RuntimeError("production execution state guard is unavailable")
        initial_state = state_guard()
        if not hmac.compare_digest(initial_state, expected_state):
            raise RuntimeError(
                "provider/tool state changed after confirmation; execution was blocked"
            )

    if prepared:
        executable = copy.deepcopy(dict(chain_dict))
    else:
        preparation_api = build_carl_llm_client(config.mage)
        executable = await _prepare_chain_runtime(
            context,
            config,
            chain_dict,
            api=preparation_api,
        )

    snapshot_context = (
        _production_execution_config_snapshot(config)
        if expected_state is not None
        else nullcontext((config, None))
    )
    with snapshot_context as (runtime_config, snapshot_tool_state):
        if expected_state is not None:
            assert snapshot_tool_state is not None
            snapshot_state = _production_config_state_sha256(
                config,
                executable,
                _tool_state_override=snapshot_tool_state,
            )
            if not hmac.compare_digest(snapshot_state, expected_state):
                raise RuntimeError(
                    "provider/tool state changed while creating the verified "
                    "execution snapshot; execution was blocked"
                )

        api = build_carl_llm_client(runtime_config.mage)
        chain = ReasoningChain.from_dict(executable, use_typed_steps=True)
        execution_sink = context.sink("execution")
        streamer = CarlStreamer(execution_sink)
        run_context = build_run_context(
            query=context.prompt,
            api=api,
            config=runtime_config,
            streamer=streamer,
        )
        cooperative_cancel = getattr(run_context, "cancel", None)
        if callable(cooperative_cancel):
            context.add_cancel_callback(cooperative_cancel)
        # Also match the TUI's self-healing seam. The helper is policy-gated by
        # CareConfig and returns immediately when no tool is missing. Production
        # disables this late mutation: missing tools fail honestly instead of
        # creating unreviewed executable code after confirmation.
        if allow_tool_synthesis:
            try:
                from care.tool_synthesis import synthesize_missing_tools

                synthesis = await synthesize_missing_tools(
                    executable,
                    run_context,
                    api=api,
                    config=runtime_config,
                    notify=lambda message: context.emit(
                        "execution.tool.status",
                        {"message": message},
                    ),
                )
                for name in synthesis.get("reused", []):
                    context.emit("execution.tool.reused", {"name": name})
                for name in synthesis.get("created", []):
                    context.emit("execution.tool.created", {"name": name})
                for failed in synthesis.get("failed", []):
                    if isinstance(failed, (list, tuple)) and len(failed) >= 2:
                        name, reason = failed[0], failed[1]
                    else:
                        name, reason = failed, "unknown error"
                    context.emit(
                        "execution.tool.failed",
                        {"name": name, "reason": redact_sensitive_text(reason)},
                    )
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - execution may still work
                context.emit(
                    "execution.warning",
                    {
                        "message": redact_sensitive_text(
                            f"tool synthesis skipped: {exc}",
                        )
                    },
                )

        if expected_state is not None:
            assert state_guard is not None
            current_state = state_guard()
            if not hmac.compare_digest(current_state, expected_state):
                raise RuntimeError(
                    "provider/tool state changed after confirmation; "
                    "execution was blocked"
                )

        context.emit(
            "execution.started",
            {"steps": len(getattr(chain, "steps", None) or [])},
        )

        return await execute_chain_async(chain, run_context), executable


def _production_file_set_state(
    raw_directory: Any,
    pattern: str,
) -> dict[str, Any]:
    """Return a bounded content fingerprint for executable/cache tool files."""

    directory = Path(raw_directory).expanduser()
    if not directory.is_absolute():
        directory = Path.cwd() / directory
    try:
        directory = directory.resolve()
    except OSError:
        return {"state": "unresolvable"}
    if not directory.exists():
        return {"state": "missing", "root": str(directory)}
    if not directory.is_dir():
        return {"state": "not-directory", "root": str(directory)}

    try:
        candidates = sorted(
            (path for path in directory.glob(pattern) if path.is_file()),
            key=lambda path: path.name,
        )
    except OSError as exc:
        return {
            "state": f"unreadable:{type(exc).__name__}",
            "root": str(directory),
        }
    if len(candidates) > MAX_PRODUCTION_STATE_FILES:
        raise RuntimeError("configured tool state exceeds the file limit")

    total = 0
    entries: list[dict[str, Any]] = []
    for path in candidates:
        digest = hashlib.sha256()
        try:
            before = path.stat()
            total += before.st_size
            if total > MAX_PRODUCTION_STATE_BYTES:
                raise RuntimeError("configured tool state exceeds the byte limit")
            with path.open("rb") as stream:
                while chunk := stream.read(64 * 1024):
                    digest.update(chunk)
            after = path.stat()
        except RuntimeError:
            raise
        except OSError as exc:
            entries.append(
                {
                    "name": path.name,
                    "state": f"unreadable:{type(exc).__name__}",
                }
            )
            continue
        if (before.st_size, before.st_mtime_ns) != (
            after.st_size,
            after.st_mtime_ns,
        ):
            raise RuntimeError("configured tool state changed while fingerprinting")
        entries.append(
            {
                "name": path.name,
                "size": after.st_size,
                "sha256": digest.hexdigest(),
            }
        )
    return {"state": "present", "root": str(directory), "entries": entries}


def _snapshot_production_file_set(
    raw_directory: Any,
    destination: Path,
    pattern: str,
) -> dict[str, Any]:
    """Copy one flat executable file set and return its source-root manifest."""

    destination.mkdir(parents=True, exist_ok=True)
    directory = Path(raw_directory).expanduser()
    if not directory.is_absolute():
        directory = Path.cwd() / directory
    try:
        directory = directory.resolve()
    except OSError:
        return {"state": "unresolvable"}
    if not directory.exists():
        return {"state": "missing", "root": str(directory)}
    if not directory.is_dir():
        return {"state": "not-directory", "root": str(directory)}
    try:
        candidates = sorted(
            (path for path in directory.glob(pattern) if path.is_file()),
            key=lambda path: path.name,
        )
    except OSError as exc:
        return {
            "state": f"unreadable:{type(exc).__name__}",
            "root": str(directory),
        }
    if len(candidates) > MAX_PRODUCTION_STATE_FILES:
        raise RuntimeError("configured tool state exceeds the file limit")

    total = 0
    entries: list[dict[str, Any]] = []
    for source in candidates:
        try:
            before = source.stat()
            if before.st_size > MAX_PRODUCTION_STATE_BYTES - total:
                raise RuntimeError("configured tool state exceeds the byte limit")
            content = source.read_bytes()
            after = source.stat()
        except RuntimeError:
            raise
        except OSError as exc:
            entries.append(
                {
                    "name": source.name,
                    "state": f"unreadable:{type(exc).__name__}",
                }
            )
            continue
        if len(content) != before.st_size or (
            before.st_size,
            before.st_mtime_ns,
        ) != (after.st_size, after.st_mtime_ns):
            raise RuntimeError("configured tool state changed while snapshotting")
        total += len(content)
        try:
            (destination / source.name).write_bytes(content)
        except OSError as exc:
            raise RuntimeError("could not create the verified tool snapshot") from exc
        entries.append(
            {
                "name": source.name,
                "size": len(content),
                "sha256": hashlib.sha256(content).hexdigest(),
            }
        )
    return {"state": "present", "root": str(directory), "entries": entries}


@contextmanager
def _production_execution_config_snapshot(
    config: Any,
) -> Iterator[tuple[Any, dict[str, Any]]]:
    """Yield a config whose user-authored executable tools are immutable copies."""

    copier = getattr(config, "model_copy", None)
    runtime_config = copier(deep=True) if callable(copier) else copy.deepcopy(config)
    tools = getattr(runtime_config, "tools", None)
    if tools is None:
        yield runtime_config, {}
        return
    with tempfile.TemporaryDirectory(prefix="maestro-production-tools-") as raw_root:
        root = Path(raw_root)
        custom = root / "custom"
        synthesized = root / "synthesized"
        manifest = {
            "custom": _snapshot_production_file_set(
                getattr(tools, "path", "~/.config/care/tools"),
                custom,
                "*.py",
            ),
            "synthesized": _snapshot_production_file_set(
                getattr(
                    tools,
                    "synthesized_tools_path",
                    "~/.config/care/synthesized_tools",
                ),
                synthesized,
                "*.json",
            ),
        }
        tools.path = custom
        tools.synthesized_tools_path = synthesized
        yield runtime_config, manifest


def _production_config_state_sha256(
    config: Any | None = None,
    chain_dict: Mapping[str, Any] | None = None,
    *,
    _tool_state_override: Mapping[str, Any] | None = None,
) -> str:
    """Bind confirmation to effective config, chain, env and tool contents.

    Provider secrets may contribute to this one-way digest but are never
    returned, logged, or placed in confirmation previews.  Keychain itself is
    deliberately not re-read: the frozen config instance is the exact provider
    snapshot that the runner will use.
    """

    from care.config import DEFAULT_CONFIG_PATH, PROJECT_CONFIG_FILENAME

    cwd = Path.cwd().resolve()
    files: dict[str, str] = {}
    for label, path in (
        ("global", DEFAULT_CONFIG_PATH.expanduser()),
        ("project", cwd / PROJECT_CONFIG_FILENAME),
        ("dotenv", cwd / ".env"),
    ):
        digest = hashlib.sha256()
        try:
            with path.open("rb") as stream:
                while chunk := stream.read(64 * 1024):
                    digest.update(chunk)
        except FileNotFoundError:
            files[label] = "missing"
        except OSError as exc:
            files[label] = f"unreadable:{type(exc).__name__}"
        else:
            files[label] = digest.hexdigest()
    environment = {
        key: hashlib.sha256(value.encode("utf-8")).hexdigest()
        for key, value in os.environ.items()
        if key.startswith("CARE_")
    }
    effective_config: Any = None
    tool_state: dict[str, Any] = (
        copy.deepcopy(dict(_tool_state_override))
        if _tool_state_override is not None
        else {}
    )
    if config is not None:
        dump = getattr(config, "model_dump", None)
        if callable(dump):
            effective_config = dump(mode="json")
        else:
            effective_config = {"type": type(config).__qualname__}
        tools = getattr(config, "tools", None)
        if tools is not None and _tool_state_override is None:
            tool_state = {
                "custom": _production_file_set_state(
                    getattr(tools, "path", "~/.config/care/tools"),
                    "*.py",
                ),
                "synthesized": _production_file_set_state(
                    getattr(
                        tools,
                        "synthesized_tools_path",
                        "~/.config/care/synthesized_tools",
                    ),
                    "*.json",
                ),
            }
    chain_snapshot = copy.deepcopy(dict(chain_dict)) if chain_dict is not None else None
    encoded = json.dumps(
        {
            "cwd": str(cwd),
            "files": files,
            "environment": environment,
            "effective_config": effective_config,
            "chain": chain_snapshot,
            "tool_state": tool_state,
        },
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _resolve_production_policies(config: Any) -> Any:
    """Merge the native Production preset with per-stage config overrides."""

    from care.config import StagePolicy
    from care.desktop_production import ProductionPolicies

    override = config.chat.mode.production
    return ProductionPolicies(
        run=override.run if override.run is not None else StagePolicy.ASK,
        save=override.save if override.save is not None else StagePolicy.AUTO,
        baseline=(
            override.baseline
            if override.baseline is not None
            else StagePolicy.AUTO
        ),
        evolve=(
            override.evolve
            if override.evolve is not None
            else StagePolicy.AUTO
        ),
    )


def _platform_evolution_configured(config: Any) -> bool:
    """Return whether Production has an explicit Platform opt-in.

    ``PlatformConfig`` carries a localhost URL as a discovery default.  Treating
    that placeholder as configured makes every Production run attempt an
    evolution request.  A non-default effective endpoint, a Platform API key,
    or an explicit Production evolve override is the opt-in instead.
    """

    from care.config import PlatformConfig, resolve_platform_api_base_url

    endpoint = resolve_platform_api_base_url(config.platform).strip().rstrip("/")
    if not endpoint:
        return False
    default_endpoint = (
        resolve_platform_api_base_url(PlatformConfig()).strip().rstrip("/")
    )
    override = config.chat.mode.production
    has_api_key = bool(str(config.platform.api_key or "").strip())
    explicit_evolve_policy = override.evolve is not None
    return endpoint != default_endpoint or has_api_key or explicit_evolve_policy


def _production_result_payload(result: Any) -> dict[str, Any]:
    """Project a production saga result without private task/run objects."""

    def value(item: Any) -> Any:
        return getattr(item, "value", item)

    stages = [
        {
            "stage": value(item.stage),
            "state": value(item.state),
            "code": value(item.code),
            "message": item.message,
        }
        for item in result.stages
    ]
    payload: dict[str, Any] = {
        "state": value(result.state),
        "committed": bool(result.committed),
        "chain_id": result.chain_id,
        "display_name": result.display_name,
        "stages": stages,
    }
    if result.baseline is not None:
        payload["baseline"] = {
            "card_id": result.baseline.card_id,
            "status": result.baseline.status,
        }
    if result.evolution is not None:
        payload["evolution"] = {
            "evolution_id": result.evolution.evolution_id,
            "status": result.evolution.status,
        }
    if result.revision_pointer is not None:
        payload["revision_pointer"] = {
            "entity_id": result.revision_pointer.entity_id,
            "channel": result.revision_pointer.channel,
        }
    if result.duplicate is not None:
        payload["duplicate"] = {
            "entity_id": result.duplicate.entity_id,
            "display_name": result.duplicate.display_name,
        }
    return payload


async def _default_production_runner(
    context: WebRunContext,
    config: Any,
) -> dict[str, Any]:
    """Run Production against one immutable provider/tool config snapshot."""

    copier = getattr(config, "model_copy", None)
    base_config = copier(deep=True) if callable(copier) else copy.deepcopy(config)
    # Keep these verified copies alive across generation, baseline, and
    # optional Platform bundling. A baseline cannot therefore observe tool A
    # while evolution later reads mutable cache entry B.
    with _production_execution_config_snapshot(base_config) as (
        snapshot_config,
        _tool_manifest,
    ):
        return await _run_default_production_with_snapshot(
            context,
            snapshot_config,
        )


async def _run_default_production_with_snapshot(
    context: WebRunContext,
    frozen_config: Any,
) -> dict[str, Any]:
    """Implementation body; ``frozen_config`` owns live verified tool copies."""

    from care.capability_priming import build_capabilities_for_generation
    from care.desktop_production import (
        DesktopProductionService,
        ProductionRequest,
        ProductionStageEvent,
        ProductionTransactionState,
    )
    from care.generation import build_mage_generator, run_generation
    from care.memory import CareMemory
    from care.runtime.cancellation import CancellationToken, CancelledByUserError
    from care.runtime.mage_poster import MagePoster

    token = CancellationToken(reason="native Production run cancelled")
    context.add_cancel_callback(token.cancel)
    capabilities = build_capabilities_for_generation(
        frozen_config,
        query=context.prompt,
    )
    generated_chain: dict[str, Any] = {}
    baseline_result: dict[str, Any] = {}
    confirmed_run_state: dict[str, str] = {}

    async def generate_deployable(request: Any, cancellation: Any) -> Any:
        cancellation.raise_if_cancelled()
        generator = build_mage_generator(
            frozen_config,
            progress=MagePoster(context.sink("generation")),
            mode=context.mode,
            # Both ProductionGenerationInput and the execute-off preview's
            # ProductionRequest reach this adapter. Production is deployable by
            # definition; reading a field only present on the former caused the
            # native preview crash reported as:
            # 'ProductionRequest' object has no attribute 'deployable'.
            deployable=True,
        )
        try:
            generated = await run_generation(
                generator,
                request.user_context or request.private_task,
                cancel=context.cancel_event,
                capabilities=request.capabilities,
                safe_task=request.safe_task,
            )
        except asyncio.CancelledError:
            if token.is_cancelled:
                raise CancelledByUserError(token.reason) from None
            raise
        cancellation.raise_if_cancelled()
        chain_dict = _extract_chain_dict(generated)
        if not chain_dict:
            raise RuntimeError("chain generator returned no chain")
        prepared_chain = await _prepare_chain_runtime(
            context,
            frozen_config,
            chain_dict,
        )
        cancellation.raise_if_cancelled()
        metadata = (
            generated.get("metadata")
            if isinstance(generated, Mapping)
            else getattr(generated, "metadata", None)
        )
        generated_chain.clear()
        generated_chain.update(copy.deepcopy(prepared_chain))
        context.emit(
            "generation.completed",
            {"chain": json_safe(prepared_chain)},
        )
        return {
            "chain_dict": prepared_chain,
            "metadata": metadata,
        }

    request = ProductionRequest(
        private_task=context.prompt,
        safe_task=context.prompt,
        user_context=context.generation_prompt,
        capabilities=capabilities,
    )

    # Execute-off remains a true preview: deployable generation and typed CARL
    # admission occur, but neither Memory nor Platform is touched.
    if not context.execute:
        generated = await generate_deployable(request, token)
        chain_dict = _extract_chain_dict(generated)
        try:
            from mmar_carl import ReasoningChain
        except ImportError as exc:
            raise RuntimeError(
                "the chain runtime is not installed; install maestro-care[carl]",
            ) from exc
        ReasoningChain.from_dict(copy.deepcopy(chain_dict), use_typed_steps=True)
        production = {
            "state": "preview_only",
            "committed": False,
            "deployable": True,
        }
        context.emit("production.completed", production)
        return {
            "answer": "Deployable agent generated and typed-validated; Execute was off, so nothing was saved or run.",
            "chain": json_safe(chain_dict),
            "production": production,
        }

    async def run_baseline(request: Any, cancellation: Any) -> Any:
        cancellation.raise_if_cancelled()
        expected_state = confirmed_run_state.get("sha256")
        if expected_state is None:
            expected_state = _production_config_state_sha256(
                frozen_config,
                generated_chain,
            )
        try:
            executed, executable = await _execute_chain_runtime(
                context,
                frozen_config,
                request.chain_dict,
                prepared=True,
                allow_tool_synthesis=False,
                expected_state=expected_state,
                state_guard=lambda: _production_config_state_sha256(
                    frozen_config,
                    generated_chain,
                ),
            )
        except asyncio.CancelledError:
            if token.is_cancelled:
                raise CancelledByUserError(token.reason) from None
            raise
        cancellation.raise_if_cancelled()
        baseline_result["raw"] = executed
        baseline_result["chain"] = executable
        return executed

    async def confirm_stage(confirmation: Any) -> bool:
        effects = "; ".join(confirmation.effects)
        preview = json.dumps(
            dict(confirmation.preview),
            ensure_ascii=False,
            sort_keys=True,
            separators=(", ", ": "),
        )
        try:
            response = await context.request_human_input(
                f"{confirmation.reason}\n"
                f"Effects: {effects}.\n"
                f"Preview: {preview}\n"
                f"Confirmation: {confirmation.binding_sha256[:12]} · "
                f"state {confirmation.state_sha256[:12]}.\n"
                "Reply yes to continue or no to stop."
            )
        except asyncio.CancelledError:
            if token.is_cancelled:
                raise CancelledByUserError(token.reason) from None
            raise
        return response.strip().lower() in {"yes", "y", "да", "д", "confirm"}

    def production_state(stage: Any) -> str:
        digest = _production_config_state_sha256(
            frozen_config,
            generated_chain,
        )
        if getattr(stage, "value", stage) == "run":
            confirmed_run_state["sha256"] = digest
        return digest

    def emit_stage(event: ProductionStageEvent) -> None:
        context.emit(
            "production.stage",
            {
                "sequence": event.sequence,
                "stage": event.stage.value,
                "state": event.state.value,
                "code": event.code.value,
                "message": event.message,
            },
        )

    start_evolution: Callable[[Any], Any] | None = None
    if _platform_evolution_configured(frozen_config):

        def start_evolution(request: Any) -> Any:
            # This desktop adapter deliberately avoids CarePlatform.from_config:
            # that constructor can bootstrap containers. A confirmed Agent run
            # may submit to an already-configured Platform, never spawn one.
            from care.desktop_remaining_commands import _default_build_platform

            platform = _default_build_platform(frozen_config)
            return platform.start_evolution(
                base_chain_id=request.chain_id,
                tags=list(request.tags),
                base_chain_content=copy.deepcopy(request.base_chain_content),
            )

    memory = CareMemory.from_config(frozen_config)
    service = DesktopProductionService(
        memory=memory,
        generate_deployable=generate_deployable,
        run_baseline=run_baseline,
        start_evolution=start_evolution,
        policies=_resolve_production_policies(frozen_config),
        confirm=confirm_stage,
        state_sha256=production_state,
        on_event=emit_stage,
    )
    result = await service.execute(request, cancellation=token)
    production = _production_result_payload(result)
    context.emit("production.completed", production)

    if result.state is ProductionTransactionState.CANCELLED:
        answer = (
            "Production run cancelled. Durable effects completed before "
            "cancellation are preserved in the stage receipts."
            if result.committed
            else "Production run cancelled before a durable agent was saved."
        )
        return {
            "answer": answer,
            "chain": json_safe(generated_chain),
            "production_state": result.state.value,
            "production": production,
        }
    if result.state is ProductionTransactionState.FAILED:
        failed = next(
            (
                item.message
                for item in result.stages
                if item.state.value == "failed"
            ),
            "Production transaction failed.",
        )
        raise RuntimeError(failed)

    if "raw" in baseline_result:
        from care.carl_summary import summarise_carl_result

        answer = summarise_carl_result(baseline_result["raw"])
    elif result.duplicate is not None:
        answer = (
            "A matching production agent already exists as "
            f"{result.duplicate.entity_id}; no duplicate was saved."
        )
    elif result.chain_id:
        answer = (
            f"Agent {result.chain_id} was saved, but the production transaction "
            f"ended with state {result.state.value}. Review the stage cards."
        )
    else:
        answer = f"Production transaction ended with state {result.state.value}."
    if result.state is ProductionTransactionState.PARTIAL:
        incomplete = ", ".join(
            item.stage.value
            for item in result.stages
            if item.state.value in {"failed", "skipped", "cancelled"}
        )
        suffix = f" Incomplete stages: {incomplete}." if incomplete else ""
        answer = (
            "Production transaction is partial: prior committed effects remain, "
            "but one or more later stages did not complete."
            f"{suffix} Review the stage cards.\n\n{answer}"
        )
    return {
        "answer": answer,
        "chain": json_safe(generated_chain),
        "production_state": result.state.value,
        "production": production,
    }


async def default_async_runner(context: WebRunContext) -> dict[str, Any]:
    """Generate a chain and optionally execute it through Maestro's runtime.

    Imports are deliberately local so ``GET /api/health`` and static serving
    remain usable even when an optional execution dependency is absent.
    """

    from care.capability_priming import build_capabilities_for_generation
    from care.config import CareConfig
    from care.generation import build_mage_generator, run_generation
    from care.runtime.mage_poster import MagePoster

    config = CareConfig.load(cwd=Path.cwd())
    if context.session_mode == "production":
        return await _default_production_runner(context, config)
    generation_sink = context.sink("generation")
    generator = build_mage_generator(
        config,
        progress=MagePoster(generation_sink),
        mode=context.mode,
        deployable=False,
    )
    capabilities = build_capabilities_for_generation(config, query=context.prompt)
    generated = await run_generation(
        generator,
        context.generation_prompt,
        cancel=context.cancel_event,
        capabilities=capabilities,
        safe_task=context.prompt,
    )
    chain_dict = _extract_chain_dict(generated)
    if not chain_dict:
        raise RuntimeError("chain generator returned no chain")
    chain_payload = json_safe(chain_dict)
    context.emit("generation.completed", {"chain": chain_payload})
    if not context.execute:
        return {"chain": chain_payload}

    if context.cancelled:
        raise asyncio.CancelledError

    from care.carl_summary import summarise_carl_result
    from care.runtime.carl_streamer import CarlStreamer
    from care.runtime.executor import build_run_context, execute_chain_async
    from care.runtime.llm_client import build_carl_llm_client
    from care.tool_planning import (
        augment_chain_for_live_data,
        downgrade_unsupported_step_types,
    )

    try:
        from mmar_carl import ReasoningChain
    except ImportError as exc:
        raise RuntimeError(
            "the chain runtime is not installed; install maestro-care[carl]",
        ) from exc

    executable = copy.deepcopy(chain_dict)
    downgrade_unsupported_step_types(executable)
    api = build_carl_llm_client(config.mage)

    # Match the TUI run path: an all-LLM plan that actually needs current data
    # gets routed to a registered/live tool before the typed chain is parsed.
    try:
        augmentation = await augment_chain_for_live_data(
            executable,
            task=context.prompt,
            api=api,
            config=config,
        )
        if augmentation.get("rewrote"):
            executable = augmentation["chain_dict"]
            context.emit(
                "execution.routing",
                {
                    "step_number": augmentation.get("step_number"),
                    "step_title": augmentation.get("step_title"),
                    "tool_name": augmentation.get("tool_name"),
                    "reason": augmentation.get("reason"),
                },
            )
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 - parity path is best-effort
        context.emit(
            "execution.warning",
            {
                "message": redact_sensitive_text(
                    f"tool routing skipped: {exc}",
                )
            },
        )

    chain = ReasoningChain.from_dict(executable, use_typed_steps=True)
    execution_sink = context.sink("execution")
    streamer = CarlStreamer(execution_sink)
    run_context = build_run_context(
        query=context.prompt,
        api=api,
        config=config,
        streamer=streamer,
    )
    cooperative_cancel = getattr(run_context, "cancel", None)
    if callable(cooperative_cancel):
        context.add_cancel_callback(cooperative_cancel)
    context.emit(
        "execution.started",
        {"steps": len(getattr(chain, "steps", None) or [])},
    )

    # Also match the TUI's self-healing seam.  The helper is policy-gated by
    # CareConfig (Docker/code execution/auto-synthesis) and returns immediately
    # when no tool is missing.
    try:
        from care.tool_synthesis import synthesize_missing_tools

        synthesis = await synthesize_missing_tools(
            executable,
            run_context,
            api=api,
            config=config,
            notify=lambda message: context.emit(
                "execution.tool.status",
                {"message": message},
            ),
        )
        for name in synthesis.get("reused", []):
            context.emit("execution.tool.reused", {"name": name})
        for name in synthesis.get("created", []):
            context.emit("execution.tool.created", {"name": name})
        for failed in synthesis.get("failed", []):
            if isinstance(failed, (list, tuple)) and len(failed) >= 2:
                name, reason = failed[0], failed[1]
            else:
                name, reason = failed, "unknown error"
            context.emit(
                "execution.tool.failed",
                {"name": name, "reason": redact_sensitive_text(reason)},
            )
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 - execution itself can still work
        context.emit(
            "execution.warning",
            {
                "message": redact_sensitive_text(
                    f"tool synthesis skipped: {exc}",
                )
            },
        )

    executed = await execute_chain_async(chain, run_context)
    answer = summarise_carl_result(executed)
    return {"answer": answer, "chain": json_safe(executable)}


def _validate_loopback_host(host: str) -> str:
    value = host.strip()
    if value.lower() == "localhost":
        return value
    candidate = value.strip("[]")
    try:
        address = ipaddress.ip_address(candidate)
    except ValueError as exc:
        raise ValueError(
            "Maestro Web is loopback-only; use 127.0.0.1, ::1, or localhost",
        ) from exc
    if not address.is_loopback:
        raise ValueError(
            "Maestro Web is loopback-only; use 127.0.0.1, ::1, or localhost",
        )
    return value


def _host_header_is_loopback(value: str) -> bool:
    # urlsplit handles bracketed IPv6 and optional ports reliably.
    try:
        parsed = urlsplit(f"//{value}")
        hostname = parsed.hostname or ""
    except ValueError:
        return False
    if hostname.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        return False


def _image_header_matches(header: bytes, mime: str) -> bool:
    if mime == "image/png":
        return header.startswith(b"\x89PNG\r\n\x1a\n")
    if mime == "image/jpeg":
        return header.startswith(b"\xff\xd8\xff")
    if mime == "image/gif":
        return header.startswith((b"GIF87a", b"GIF89a"))
    if mime == "image/bmp":
        return header.startswith(b"BM")
    if mime == "image/webp":
        return len(header) >= 12 and header[:4] == b"RIFF" and header[8:12] == b"WEBP"
    return False


def _write_workspace_text(
    workspace_root: Path,
    *,
    relative_path: Any,
    content: Any,
) -> dict[str, Any]:
    """Atomically write one bounded UTF-8 file below ``workspace_root``.

    The desktop ``/blocks save`` form supplies content that exists only in the
    WebKit renderer, so it cannot use the ordinary CLI bridge.  Keep this seam
    deliberately narrow: paths must be relative, traversal and NUL bytes are
    rejected, and the response never reflects the submitted content.
    """

    if not isinstance(relative_path, str):
        raise RequestValidationError("path must be a string")
    if not relative_path or len(relative_path) > MAX_WORKSPACE_PATH_CHARS:
        raise RequestValidationError(
            f"path must contain 1-{MAX_WORKSPACE_PATH_CHARS} characters"
        )
    if "\x00" in relative_path:
        raise RequestValidationError("path contains an invalid character")
    relative = Path(relative_path)
    if relative.is_absolute() or any(part in {"", ".", ".."} for part in relative.parts):
        raise RequestValidationError("path must stay inside the current workspace")
    if not isinstance(content, str):
        raise RequestValidationError("content must be a string")
    body = content.encode("utf-8")
    if len(body) > MAX_WORKSPACE_TEXT_BYTES:
        raise RequestValidationError(
            f"content exceeds the {MAX_WORKSPACE_TEXT_BYTES}-byte workspace-file limit"
        )

    root = workspace_root.resolve(strict=True)
    try:
        target = (root / relative).resolve(strict=False)
        target.relative_to(root)
    except (OSError, RuntimeError, ValueError) as exc:
        raise RequestValidationError(
            "path must stay inside the current workspace"
        ) from exc
    if target == root or (target.exists() and not target.is_file()):
        raise RequestValidationError("path must name a workspace file")

    temporary: Path | None = None
    descriptor: int | None = None
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        # Re-resolve after mkdir so an existing symlinked parent cannot escape
        # the workspace between validation and file creation.
        resolved_parent = target.parent.resolve(strict=True)
        resolved_parent.relative_to(root)
        target = resolved_parent / target.name
        if target.exists() and not target.is_file():
            raise RequestValidationError("path must name a workspace file")
        descriptor, raw_temporary = tempfile.mkstemp(
            prefix=f".{target.name}.",
            suffix=".tmp",
            dir=resolved_parent,
        )
        temporary = Path(raw_temporary)
        mode = (target.stat().st_mode & 0o777) if target.exists() else 0o644
        os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = None
            stream.write(body)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
        temporary = None
    except RequestValidationError:
        raise
    except OSError as exc:
        raise WorkspaceWriteError(
            "workspace file could not be written"
        ) from exc
    finally:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass

    return {
        "ok": True,
        "path": target.relative_to(root).as_posix(),
        "bytes": len(body),
        "lines": len(content.splitlines()),
        "sha256": hashlib.sha256(body).hexdigest(),
    }


class MaestroWebServer(ThreadingHTTPServer):
    """Threading server carrying the manager/static-root dependencies."""

    daemon_threads = True
    allow_reuse_address = True

    def __init__(
        self,
        server_address: tuple[str, int],
        handler: type[BaseHTTPRequestHandler],
        *,
        run_manager: RunManager,
        session_store: DesktopSessionStore,
        command_bridge: CommandBridge,
        command_jobs: DesktopCommandJobManager,
        settings_bridge: DesktopSettingsBridge,
        assets_dir: Path,
        workspace_root: Path,
    ) -> None:
        self.run_manager = run_manager
        self.session_store = session_store
        self.command_bridge = command_bridge
        self.command_jobs = command_jobs
        self.settings_bridge = settings_bridge
        self.assets_dir = assets_dir.resolve()
        self.workspace_root = workspace_root.resolve()
        self._closing = threading.Event()
        self._auth_lock = threading.Lock()
        self._launch_token: str | None = secrets.token_urlsafe(32)
        self._session_token = secrets.token_urlsafe(32)
        super().__init__(server_address, handler)

    @property
    def base_url(self) -> str:
        host, port = self.server_address[:2]
        display_host = f"[{host}]" if ":" in str(host) else host
        return f"http://{display_host}:{port}/"

    @property
    def launch_url(self) -> str:
        """One-time bootstrap URL for the desktop wrapper/browser opener."""

        with self._auth_lock:
            token = self._launch_token
        if token is None:
            return self.base_url
        return f"{self.base_url}?token={token}"

    def exchange_launch_token(self, supplied: str) -> bool:
        with self._auth_lock:
            expected = self._launch_token
            if expected is None or not hmac.compare_digest(expected, supplied):
                return False
            self._launch_token = None
            return True

    def is_authorized(self, supplied: str | None) -> bool:
        return bool(supplied and hmac.compare_digest(self._session_token, supplied))

    def server_close(self) -> None:
        self._closing.set()
        self.run_manager.shutdown(wait=False)
        self.command_jobs.shutdown(wait=False, cancel_pending=True)
        super().server_close()


class _IPv6MaestroWebServer(MaestroWebServer):
    address_family = socket.AF_INET6


class _WebHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "MaestroWeb"
    sys_version = ""

    @property
    def web_server(self) -> MaestroWebServer:
        return self.server  # type: ignore[return-value]

    def do_GET(self) -> None:  # noqa: N802 - stdlib handler contract
        self._dispatch(head=False)

    def do_HEAD(self) -> None:  # noqa: N802
        self._dispatch(head=True)

    def do_POST(self) -> None:  # noqa: N802
        self._dispatch_post()

    def do_PUT(self) -> None:  # noqa: N802
        self._method_not_allowed()

    def do_PATCH(self) -> None:  # noqa: N802
        self._method_not_allowed()

    def do_DELETE(self) -> None:  # noqa: N802
        self._method_not_allowed()

    def _dispatch(self, *, head: bool) -> None:
        if not self._request_host_allowed():
            return
        parsed = urlsplit(self.path)
        path = parsed.path
        if path == "/" and not head and "token" in parse_qs(parsed.query):
            self._bootstrap_session(parse_qs(parsed.query).get("token", [""])[0])
            return
        if not self._request_authorized():
            return
        if len(path) > 2_048:
            self._send_error(
                HTTPStatus.REQUEST_URI_TOO_LONG, "uri_too_long", "URI too long"
            )
            return
        try:
            if path == "/api/health":
                if head:
                    self._method_not_allowed()
                    return
                self._send_json(
                    HTTPStatus.OK,
                    {
                        "status": "ok",
                        "service": "maestro-web",
                        "history": "persistent workspace sessions plus bounded process-local runs",
                        "runs": self.web_server.run_manager.stats(),
                    },
                )
                return
            if path == "/api/runs":
                if head:
                    self._method_not_allowed()
                    return
                self._send_json(
                    HTTPStatus.OK,
                    {"runs": self.web_server.run_manager.list_runs()},
                )
                return
            if path == "/api/session":
                if head:
                    self._method_not_allowed()
                    return
                try:
                    session = self.web_server.session_store.current_session()
                except DesktopSessionError as exc:
                    raise _translate_session_error(exc) from exc
                self._send_json(HTTPStatus.OK, session.to_dict())
                return
            if path == "/api/sessions":
                if head:
                    self._method_not_allowed()
                    return
                try:
                    sessions = self.web_server.session_store.list_sessions()
                except DesktopSessionError as exc:
                    raise _translate_session_error(exc) from exc
                self._send_json(
                    HTTPStatus.OK,
                    {"sessions": [session.to_dict() for session in sessions]},
                )
                return
            if path.startswith("/api/sessions/"):
                if head:
                    self._method_not_allowed()
                    return
                session_id = unquote(path.removeprefix("/api/sessions/"))
                if not session_id or "/" in session_id:
                    self._send_error(
                        HTTPStatus.NOT_FOUND, "not_found", "API route not found"
                    )
                    return
                try:
                    session = self.web_server.session_store.get_session(session_id)
                except DesktopSessionError as exc:
                    raise _translate_session_error(exc) from exc
                self._send_json(HTTPStatus.OK, session.to_dict())
                return
            if path == "/api/command-jobs":
                if head:
                    self._method_not_allowed()
                    return
                try:
                    jobs = self.web_server.command_jobs.list_jobs()
                except (CommandJobError, ValueError) as exc:
                    raise _translate_command_job_error(exc) from exc
                self._send_json(HTTPStatus.OK, {"jobs": jobs})
                return
            if path == "/api/command-jobs/events":
                if head:
                    self._method_not_allowed()
                    return
                values = parse_qs(parsed.query)
                try:
                    after = int(values.get("after", ["0"])[0])
                    limit = int(values.get("limit", ["512"])[0])
                    events = self.web_server.command_jobs.events(
                        after=after,
                        limit=limit,
                    )
                except (CommandJobError, ValueError) as exc:
                    raise _translate_command_job_error(exc) from exc
                self._send_json(HTTPStatus.OK, events)
                return
            job_parts = self._command_job_path_parts(path)
            if job_parts is not None:
                if head:
                    self._method_not_allowed()
                    return
                job_id, action = job_parts
                if action is not None:
                    self._method_not_allowed()
                    return
                try:
                    job = self.web_server.command_jobs.get_job(job_id)
                except (CommandJobError, ValueError) as exc:
                    raise _translate_command_job_error(exc) from exc
                self._send_json(HTTPStatus.OK, job)
                return
            if path == "/api/commands":
                if head:
                    self._method_not_allowed()
                    return
                self._send_json(
                    HTTPStatus.OK,
                    self.web_server.command_bridge.registry(),
                )
                return
            if path == "/api/settings":
                if head:
                    self._method_not_allowed()
                    return
                try:
                    snapshot = self.web_server.settings_bridge.snapshot()
                except SettingsLoadError:
                    self._send_error(
                        HTTPStatus.INTERNAL_SERVER_ERROR,
                        "settings_load_failed",
                        "Settings could not be loaded safely.",
                    )
                    return
                self._send_json(
                    HTTPStatus.OK,
                    snapshot,
                )
                return
            if path.startswith("/api/workspace-images/"):
                self._serve_workspace_image(path, parsed.query, head=head)
                return
            parts = self._run_path_parts(path)
            if parts is not None:
                run_id, action = parts
                if action == "events":
                    if head:
                        self._method_not_allowed()
                        return
                    self._serve_events(run_id, parsed.query)
                    return
                if action is None:
                    if head:
                        self._method_not_allowed()
                        return
                    self._send_json(
                        HTTPStatus.OK,
                        self.web_server.run_manager.get_run(run_id),
                    )
                    return
                self._method_not_allowed()
                return
            if path.startswith("/api/"):
                self._send_error(
                    HTTPStatus.NOT_FOUND, "not_found", "API route not found"
                )
                return
            self._serve_static(path, head=head)
        except WebError as exc:
            self._send_web_error(exc)
        except Exception:  # noqa: BLE001
            _log.exception("unhandled GET %s", self.path)
            self._send_error(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                "internal_error",
                "internal server error",
            )

    def _dispatch_post(self) -> None:
        if (
            not self._request_host_allowed()
            or not self._request_authorized()
            or not self._mutation_origin_allowed()
        ):
            return
        parsed = urlsplit(self.path)
        path = parsed.path
        try:
            payload = self._read_json_body(
                max_bytes=(
                    MAX_COMMAND_REQUEST_BYTES
                    if path == "/api/commands"
                    else MAX_WORKSPACE_TEXT_BYTES + MAX_REQUEST_BYTES
                    if path == "/api/workspace-files"
                    else MAX_REQUEST_BYTES
                )
            )
            if path == "/api/runs":
                allowed = {"prompt", "task", "mode", "session_mode", "execute"}
                unknown = sorted(set(payload) - allowed)
                if unknown:
                    raise RequestValidationError(
                        f"unknown field(s): {', '.join(unknown)}",
                    )
                prompt = payload.get("prompt", payload.get("task"))
                try:
                    current_session = self.web_server.session_store.current_session()
                except DesktopSessionError as exc:
                    raise _translate_session_error(exc) from exc
                run = self.web_server.run_manager.submit(
                    prompt,
                    mode=payload.get("mode", current_session.generation_mode),
                    session_mode=payload.get(
                        "session_mode", current_session.session_mode
                    ),
                    execute=payload.get("execute", True),
                )
                self._send_json(HTTPStatus.ACCEPTED, run)
                return
            if path == "/api/session/new":
                allowed = {"session_mode", "generation_mode"}
                unknown = sorted(set(payload) - allowed)
                if unknown:
                    raise RequestValidationError(
                        f"unknown field(s): {', '.join(unknown)}"
                    )
                try:
                    session = self.web_server.session_store.new_session(
                        session_mode=payload.get("session_mode"),
                        generation_mode=payload.get("generation_mode"),
                    )
                except DesktopSessionError as exc:
                    raise _translate_session_error(exc) from exc
                self._send_json(HTTPStatus.CREATED, session.to_dict())
                return
            if path == "/api/session/clear":
                if payload:
                    raise RequestValidationError(
                        "clear body must be an empty JSON object"
                    )
                try:
                    session = self.web_server.session_store.clear_current()
                except DesktopSessionError as exc:
                    raise _translate_session_error(exc) from exc
                self._send_json(HTTPStatus.OK, session.to_dict())
                return
            if path == "/api/session/resume":
                unknown = sorted(set(payload) - {"target"})
                if unknown:
                    raise RequestValidationError(
                        f"unknown field(s): {', '.join(unknown)}"
                    )
                try:
                    session = self.web_server.session_store.resume_session(
                        payload.get("target")
                    )
                except DesktopSessionError as exc:
                    raise _translate_session_error(exc) from exc
                self._send_json(HTTPStatus.OK, session.to_dict())
                return
            if path == "/api/session/modes":
                allowed = {"session_mode", "generation_mode"}
                unknown = sorted(set(payload) - allowed)
                if unknown:
                    raise RequestValidationError(
                        f"unknown field(s): {', '.join(unknown)}"
                    )
                if not payload:
                    raise RequestValidationError(
                        "at least one session mode field is required"
                    )
                try:
                    current = self.web_server.session_store.current_session()
                    requested_session_mode = payload.get("session_mode")
                    if (
                        requested_session_mode is not None
                        and requested_session_mode != current.session_mode
                        and current.turns
                    ):
                        session = self.web_server.session_store.new_session(
                            session_mode=requested_session_mode,
                            generation_mode=payload.get(
                                "generation_mode", current.generation_mode
                            ),
                        )
                    else:
                        session = self.web_server.session_store.set_modes(
                            session_mode=requested_session_mode,
                            generation_mode=payload.get("generation_mode"),
                        )
                except DesktopSessionError as exc:
                    raise _translate_session_error(exc) from exc
                self._send_json(HTTPStatus.OK, session.to_dict())
                return
            if path == "/api/workspace-files":
                allowed = {"path", "content"}
                unknown = sorted(set(payload) - allowed)
                if unknown:
                    raise RequestValidationError(
                        f"unknown field(s): {', '.join(unknown)}"
                    )
                result = _write_workspace_text(
                    self.web_server.workspace_root,
                    relative_path=payload.get("path"),
                    content=payload.get("content"),
                )
                self._send_json(HTTPStatus.CREATED, result)
                return
            if path == "/api/command-jobs":
                allowed = {"command", "confirmation_token"}
                unknown = sorted(set(payload) - allowed)
                if unknown:
                    raise RequestValidationError(
                        f"unknown field(s): {', '.join(unknown)}"
                    )
                try:
                    job = self.web_server.command_jobs.submit(
                        payload.get("command"),
                        confirmation_token=payload.get("confirmation_token"),
                    )
                except (CommandJobError, ValueError) as exc:
                    raise _translate_command_job_error(exc) from exc
                self._send_json(HTTPStatus.ACCEPTED, job)
                return
            job_parts = self._command_job_path_parts(path)
            if job_parts is not None:
                job_id, action = job_parts
                if action == "cancel":
                    if payload:
                        raise RequestValidationError(
                            "cancel body must be an empty JSON object"
                        )
                    try:
                        job = self.web_server.command_jobs.cancel(job_id)
                    except (CommandJobError, ValueError) as exc:
                        raise _translate_command_job_error(exc) from exc
                    self._send_json(HTTPStatus.ACCEPTED, job)
                    return
                if action == "result":
                    if payload:
                        raise RequestValidationError(
                            "result body must be an empty JSON object"
                        )
                    try:
                        result = self.web_server.command_jobs.take_result(job_id)
                    except (CommandJobError, ValueError) as exc:
                        raise _translate_command_job_error(exc) from exc
                    self._send_json(HTTPStatus.OK, result)
                    return
                self._method_not_allowed()
                return
            if path == "/api/commands":
                allowed = {"command", "confirmation_token", "transcript"}
                unknown = sorted(set(payload) - allowed)
                if unknown:
                    raise RequestValidationError(
                        f"unknown field(s): {', '.join(unknown)}",
                    )
                command = payload.get("command")
                transcript = payload.get("transcript")
                if _is_export_command(command):
                    try:
                        transcript = self.web_server.session_store.transcript()
                    except DesktopSessionError as exc:
                        raise _translate_session_error(exc) from exc
                result = self.web_server.command_bridge.execute(
                    command,
                    confirmation_token=payload.get("confirmation_token"),
                    transcript=transcript,
                )
                self._send_json(result.status, result.payload)
                return
            if path == "/api/settings":
                confirmation_token = payload.pop("confirmation_token", None)
                result = self.web_server.settings_bridge.update(
                    payload,
                    confirmation_token=confirmation_token,
                )
                self._send_json(result.status, result.payload)
                return

            parts = self._run_path_parts(path)
            if parts is None:
                self._send_error(
                    HTTPStatus.NOT_FOUND, "not_found", "API route not found"
                )
                return
            run_id, action = parts
            if action == "cancel":
                if payload:
                    raise RequestValidationError(
                        "cancel body must be an empty JSON object"
                    )
                run = self.web_server.run_manager.cancel_run(run_id)
                self._send_json(HTTPStatus.ACCEPTED, run)
                return
            if action in {"input", "human-input"}:
                unknown = sorted(set(payload) - {"value", "request_id"})
                if unknown:
                    raise RequestValidationError(
                        f"unknown field(s): {', '.join(unknown)}",
                    )
                run = self.web_server.run_manager.provide_human_input(
                    run_id,
                    payload.get("value"),
                    request_id=payload.get("request_id"),
                )
                self._send_json(HTTPStatus.ACCEPTED, run)
                return
            self._method_not_allowed()
        except WebError as exc:
            self._send_web_error(exc)
        except Exception:  # noqa: BLE001
            _log.exception("unhandled POST %s", self.path)
            self._send_error(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                "internal_error",
                "internal server error",
            )

    @staticmethod
    def _command_job_path_parts(path: str) -> tuple[str, str | None] | None:
        parts = [unquote(part) for part in path.split("/") if part]
        if len(parts) < 3 or parts[:2] != ["api", "command-jobs"]:
            return None
        if len(parts) == 3:
            return parts[2], None
        if len(parts) == 4 and parts[3] in {"cancel", "result"}:
            return parts[2], parts[3]
        return None

    @staticmethod
    def _run_path_parts(path: str) -> tuple[str, str | None] | None:
        parts = [unquote(part) for part in path.split("/") if part]
        if len(parts) < 3 or parts[:2] != ["api", "runs"]:
            return None
        if len(parts) == 3:
            return parts[2], None
        if len(parts) == 4 and parts[3] in {
            "events",
            "cancel",
            "input",
            "human-input",
        }:
            return parts[2], parts[3]
        return None

    def _read_json_body(self, *, max_bytes: int = MAX_REQUEST_BYTES) -> dict[str, Any]:
        media_type = (
            self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
        )
        if media_type != "application/json":
            raise UnsupportedMediaTypeError("Content-Type must be application/json")
        transfer_encoding = self.headers.get("Transfer-Encoding")
        if transfer_encoding:
            raise RequestValidationError("chunked request bodies are not supported")
        length_text = self.headers.get("Content-Length")
        if length_text is None:
            raise RequestValidationError("Content-Length is required")
        try:
            length = int(length_text)
        except ValueError as exc:
            raise RequestValidationError("invalid Content-Length") from exc
        if length < 0:
            raise RequestValidationError("invalid Content-Length")
        if length > max_bytes:
            # Close instead of attempting to drain an attacker-controlled body.
            self.close_connection = True
            error = WebError(f"request body exceeds {max_bytes} bytes")
            error.status = HTTPStatus.REQUEST_ENTITY_TOO_LARGE
            error.code = "request_too_large"
            raise error
        raw = self.rfile.read(length)
        try:
            decoded = raw.decode("utf-8")
            value = json.loads(decoded) if decoded.strip() else None
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RequestValidationError(
                "request body must be valid UTF-8 JSON"
            ) from exc
        if not isinstance(value, dict):
            raise RequestValidationError("request body must be a JSON object")
        return value

    def _request_host_allowed(self) -> bool:
        host = self.headers.get("Host", "")
        if not host or not _host_header_is_loopback(host):
            self._send_error(
                HTTPStatus.MISDIRECTED_REQUEST,
                "invalid_host",
                "Host must identify a loopback address",
            )
            return False
        return True

    def _request_authorized(self) -> bool:
        raw = self.headers.get("Cookie", "")
        supplied: str | None = None
        if raw:
            try:
                cookie = SimpleCookie()
                cookie.load(raw)
                morsel = cookie.get(SESSION_COOKIE_NAME)
                supplied = morsel.value if morsel is not None else None
            except Exception:  # noqa: BLE001
                supplied = None
        if self.web_server.is_authorized(supplied):
            return True
        self._send_error(
            HTTPStatus.UNAUTHORIZED,
            "unauthorized",
            "bootstrap this local Maestro session first",
        )
        return False

    def _bootstrap_session(self, token: str) -> None:
        if not self.web_server.exchange_launch_token(token):
            self._send_error(
                HTTPStatus.FORBIDDEN,
                "invalid_launch_token",
                "invalid or already-used launch token",
            )
            return
        self.send_response(HTTPStatus.SEE_OTHER)
        self._security_headers(api=False)
        self.send_header("Location", "/")
        self.send_header(
            "Set-Cookie",
            f"{SESSION_COOKIE_NAME}={self.web_server._session_token}; "
            "Path=/; HttpOnly; SameSite=Strict",
        )
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _mutation_origin_allowed(self) -> bool:
        fetch_site = self.headers.get("Sec-Fetch-Site", "").lower()
        if fetch_site and fetch_site not in {"same-origin", "none"}:
            self._send_error(
                HTTPStatus.FORBIDDEN,
                "cross_origin",
                "cross-origin mutation rejected",
            )
            return False
        origin = self.headers.get("Origin")
        if not origin:  # CLI/test clients do not send Origin.
            return True
        try:
            parsed = urlsplit(origin)
        except ValueError:
            parsed = None
        host = self.headers.get("Host", "")
        if (
            parsed is None
            # The loopback server is plain HTTP. Exact-origin validation must
            # reject an https Origin even when host and port text match.
            or parsed.scheme != "http"
            or parsed.netloc.casefold() != host.casefold()
            or not parsed.hostname
            or not _host_header_is_loopback(parsed.netloc)
        ):
            self._send_error(
                HTTPStatus.FORBIDDEN,
                "cross_origin",
                "Origin must match this Maestro Web origin",
            )
            return False
        return True

    def _serve_events(self, run_id: str, query: str) -> None:
        after = 0
        last_id = self.headers.get("Last-Event-ID", "").strip()
        if last_id:
            try:
                after = max(0, int(last_id))
            except ValueError:
                after = 0
        if query:
            for field in query.split("&"):
                key, separator, value = field.partition("=")
                if key == "after" and separator:
                    try:
                        after = max(after, int(value))
                    except ValueError:
                        pass

        # Validate before committing the streaming response.
        self.web_server.run_manager.get_run(run_id)
        self.send_response(HTTPStatus.OK)
        self._security_headers(api=True)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()

        try:
            while not self.web_server._closing.is_set():
                events, terminal = self.web_server.run_manager.wait_for_events(
                    run_id,
                    after=after,
                    timeout=SSE_HEARTBEAT_SECONDS,
                )
                if not events:
                    if terminal:
                        break
                    self.wfile.write(b": keep-alive\n\n")
                    self.wfile.flush()
                    continue
                for event in events:
                    event_name = str(event["type"]).replace("\r", "").replace("\n", "")
                    payload = _json_bytes(event)
                    self.wfile.write(f"id: {event['seq']}\n".encode("ascii"))
                    self.wfile.write(f"event: {event_name}\n".encode("utf-8"))
                    self.wfile.write(b"data: " + payload + b"\n\n")
                    after = int(event["seq"])
                self.wfile.flush()
                if terminal:
                    break
        except (BrokenPipeError, ConnectionResetError, TimeoutError, OSError):
            return
        finally:
            self.close_connection = True

    def _serve_workspace_image(
        self,
        path: str,
        query: str,
        *,
        head: bool,
    ) -> None:
        """Serve one command-validated workspace image by content digest."""

        prefix = "/api/workspace-images/"
        relative_text = unquote(path[len(prefix) :])
        relative = Path(relative_text)
        expected = parse_qs(query).get("sha256", [""])[0].lower()
        if (
            not relative_text
            or relative.is_absolute()
            or any(
                part in {"", ".", ".."} or part.startswith(".")
                for part in relative.parts
            )
            or len(expected) != 64
            or any(char not in "0123456789abcdef" for char in expected)
        ):
            self._send_error(HTTPStatus.NOT_FOUND, "not_found", "image not found")
            return
        root = self.web_server.workspace_root
        try:
            candidate = (root / relative).resolve(strict=True)
            candidate.relative_to(root)
        except (OSError, RuntimeError, ValueError):
            self._send_error(HTTPStatus.NOT_FOUND, "not_found", "image not found")
            return
        mime = _WORKSPACE_IMAGE_MIME.get(candidate.suffix.lower())
        if not mime or not candidate.is_file():
            self._send_error(HTTPStatus.NOT_FOUND, "not_found", "image not found")
            return
        try:
            stream = candidate.open("rb")
        except OSError:
            self._send_error(HTTPStatus.NOT_FOUND, "not_found", "image not found")
            return
        with stream:
            size = os.fstat(stream.fileno()).st_size
            if size > MAX_WORKSPACE_IMAGE_BYTES:
                self._send_error(
                    HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                    "image_too_large",
                    "image exceeds local preview limit",
                )
                return
            try:
                digest = hashlib.sha256()
                header = stream.read(32)
                digest.update(header)
                while chunk := stream.read(64 * 1024):
                    digest.update(chunk)
                stream.seek(0)
            except OSError:
                self._send_error(HTTPStatus.NOT_FOUND, "not_found", "image not found")
                return
            if not hmac.compare_digest(
                digest.hexdigest(),
                expected,
            ) or not _image_header_matches(header, mime):
                self._send_error(HTTPStatus.CONFLICT, "image_changed", "image changed")
                return
            self.send_response(HTTPStatus.OK)
            self._security_headers(api=True)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(size))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Disposition", "inline")
            self.end_headers()
            if head:
                return
            try:
                while chunk := stream.read(64 * 1024):
                    self.wfile.write(chunk)
            except (BrokenPipeError, ConnectionResetError, OSError):
                return

    def _serve_static(self, path: str, *, head: bool) -> None:
        decoded = unquote(path)
        relative = decoded.lstrip("/") or "index.html"
        if relative.startswith("assets/"):
            relative = relative[len("assets/") :]
        if not relative or any(part.startswith(".") for part in Path(relative).parts):
            self._send_error(HTTPStatus.NOT_FOUND, "not_found", "asset not found")
            return
        root = self.web_server.assets_dir
        candidate = (root / relative).resolve()
        try:
            candidate.relative_to(root)
        except ValueError:
            self._send_error(HTTPStatus.NOT_FOUND, "not_found", "asset not found")
            return
        if not candidate.is_file():
            self._send_error(HTTPStatus.NOT_FOUND, "not_found", "asset not found")
            return
        size = candidate.stat().st_size
        if size > MAX_STATIC_BYTES:
            self._send_error(
                HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                "asset_too_large",
                "asset exceeds local serving limit",
            )
            return
        content_type = (
            mimetypes.guess_type(candidate.name)[0] or "application/octet-stream"
        )
        if candidate.suffix == ".js":
            content_type = "text/javascript"
        self.send_response(HTTPStatus.OK)
        self._security_headers(api=False)
        self.send_header(
            "Content-Type",
            f"{content_type}; charset=utf-8"
            if content_type.startswith("text/")
            else content_type,
        )
        self.send_header("Content-Length", str(size))
        self.send_header(
            "Cache-Control",
            "no-cache" if candidate.name == "index.html" else "public, max-age=300",
        )
        self.end_headers()
        if head:
            return
        with candidate.open("rb") as stream:
            while chunk := stream.read(64 * 1024):
                self.wfile.write(chunk)

    def _send_json(self, status: HTTPStatus | int, payload: Any) -> None:
        body = _json_bytes(payload)
        self.send_response(int(status))
        self._security_headers(api=True)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _send_web_error(self, error: WebError) -> None:
        self._send_error(error.status, error.code, str(error))

    def _send_error(self, status: HTTPStatus | int, code: str, message: str) -> None:
        self._send_json(
            status,
            {"error": {"code": code, "message": message}},
        )

    def _method_not_allowed(self) -> None:
        self._send_error(
            HTTPStatus.METHOD_NOT_ALLOWED,
            "method_not_allowed",
            "method not allowed for this route",
        )

    def _security_headers(self, *, api: bool) -> None:
        self.send_header("Content-Security-Policy", _CSP)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header(
            "Permissions-Policy", "camera=(), microphone=(), geolocation=()"
        )
        self.send_header("Cross-Origin-Opener-Policy", "same-origin")
        self.send_header("Cross-Origin-Resource-Policy", "same-origin")
        if api:
            self.send_header("Vary", "Origin")

    def log_message(self, format: str, *args: Any) -> None:
        # Never include the bootstrap query token in logs.
        _log.debug(
            "%s - %s %s",
            self.address_string(),
            self.command,
            urlsplit(self.path).path,
        )


def create_server(
    *,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    run_manager: RunManager | None = None,
    session_store: DesktopSessionStore | None = None,
    command_bridge: CommandBridge | None = None,
    command_jobs: DesktopCommandJobManager | None = None,
    settings_bridge: DesktopSettingsBridge | None = None,
    assets_dir: str | os.PathLike[str] | None = None,
    workspace_root: str | os.PathLike[str] | None = None,
) -> ThreadingHTTPServer:
    """Create (but do not start) a loopback-only Maestro web server."""

    host = _validate_loopback_host(host)
    if not isinstance(port, int) or not 0 <= port <= 65_535:
        raise ValueError("port must be between 0 and 65535")
    workspace = (
        (Path(workspace_root) if workspace_root is not None else Path.cwd())
        .expanduser()
        .resolve()
    )
    if not workspace.is_dir():
        raise ValueError("workspace_root must be an existing directory")
    settings = settings_bridge or DesktopSettingsBridge()
    sessions = session_store or (
        run_manager.session_store if run_manager is not None else None
    )
    if sessions is None:
        default_session_mode = "interactive"
        default_generation_mode = "fast"
        try:
            public_settings = settings.snapshot().get("settings", {})
            default_session_mode = public_settings.get(
                "default_mode", default_session_mode
            )
            default_generation_mode = public_settings.get(
                "generation_mode", default_generation_mode
            )
        except SettingsLoadError:
            pass
        sessions = DesktopSessionStore(
            workspace_root=workspace,
            default_session_mode=default_session_mode,
            default_generation_mode=default_generation_mode,
        )
    if sessions.workspace_root != workspace:
        raise ValueError("session_store must be scoped to workspace_root")
    manager = run_manager or RunManager(session_store=sessions)
    if manager.session_store is None:
        manager.attach_session_store(sessions)
    elif manager.session_store is not sessions:
        raise ValueError("run_manager and session_store must use the same store")
    static_root = (
        Path(assets_dir)
        if assets_dir is not None
        else Path(__file__).with_name("web_assets")
    )
    selected_command_bridge = command_bridge or CommandBridge()

    def run_command_job(
        command: str,
        *,
        confirmation_token: str | None = None,
        cancel_event: threading.Event,
    ) -> Any:
        raise_if_cancelled(cancel_event)
        transcript = sessions.transcript() if _is_export_command(command) else None
        return selected_command_bridge.execute(
            command,
            confirmation_token=confirmation_token,
            transcript=transcript,
        )

    selected_command_jobs = command_jobs or DesktopCommandJobManager(
        bridge=selected_command_bridge,
        runner=run_command_job,
    )
    server_type = _IPv6MaestroWebServer if ":" in host.strip("[]") else MaestroWebServer
    return server_type(
        (host, port),
        _WebHandler,
        run_manager=manager,
        session_store=sessions,
        command_bridge=selected_command_bridge,
        command_jobs=selected_command_jobs,
        settings_bridge=settings,
        assets_dir=static_root,
        workspace_root=workspace,
    )


def serve_web(
    *,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    open_browser: bool = True,
    stdout: TextIO | None = None,
) -> None:
    """Serve Maestro Web until interrupted.

    ``open_browser`` is best-effort; a headless host still serves normally.
    """

    output = stdout or sys.stdout
    server = create_server(host=host, port=port)
    url = server.base_url  # type: ignore[attr-defined]
    launch_url = server.launch_url  # type: ignore[attr-defined]
    output.write(f"Maestro Web: {url}\n")
    output.flush()
    if open_browser:
        try:
            webbrowser.open(launch_url)
        except Exception as exc:  # noqa: BLE001
            _log.debug("could not open browser: %s", exc)
    try:
        server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown()
        manager = getattr(server, "run_manager", None)
        if manager is not None:
            manager.shutdown(wait=True)
        server.server_close()


__all__ = [
    "DEFAULT_HOST",
    "DEFAULT_PORT",
    "MAX_REQUEST_BYTES",
    "RunManager",
    "RuntimeEventSink",
    "WebRunContext",
    "create_server",
    "default_async_runner",
    "json_safe",
    "map_runtime_message",
    "serve_web",
]
