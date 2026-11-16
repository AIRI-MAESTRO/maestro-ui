"""Bounded background jobs for native desktop command execution.

The current :class:`care.desktop_commands.CommandBridge` is synchronous.  This
module gives a future web/native adapter a small thread-safe job boundary
without coupling it to HTTP:

* bounded queued/running jobs and a fixed worker pool;
* status snapshots and cursor-based events that never contain command output;
* an atomically consumed, one-use result transcript;
* cancellation before start and an optional cooperative ``cancel_event`` seam;
* bounded terminal history and deterministic shutdown.

Python cannot safely kill an arbitrary running thread.  Consequently, a
non-cooperative ``CommandBridge.execute`` call may finish (and may have already
performed side effects) after cancellation is requested.  Such a job is
reported honestly as ``completed``/``failed`` with
``cancellation_effective=False``.  A cooperative runner must accept the
``cancel_event`` keyword and raise :class:`CommandJobCancelled` when it observes
the event.
"""

from __future__ import annotations

import copy
import inspect
import json
import math
import re
import secrets
import shlex
import threading
import time
from collections import OrderedDict, deque
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from care.desktop_commands import CommandBridge, CommandDispatch, MAX_COMMAND_CHARS
from care.redaction import redact_sensitive_text


DEFAULT_MAX_JOBS = 32
DEFAULT_MAX_HISTORY = 64
DEFAULT_MAX_CONCURRENCY = 2
DEFAULT_MAX_EVENTS = 512
DEFAULT_MAX_TRANSCRIPT_CHARS = 256 * 1024
MIN_TRANSCRIPT_CHARS = 256
MAX_EVENT_LIMIT = 512
MAX_DISPLAY_COMMAND_CHARS = 1_000
MAX_CONFIRMATION_TOKEN_CHARS = 512
MAX_JOB_ID_CHARS = 128
MAX_PROJECTED_NODES = 4_000
MAX_PROJECTED_DEPTH = 8
MAX_PROJECTED_STRING_CHARS = 128 * 1024

_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_JOB_ID_RE = re.compile(r"^[A-Za-z0-9_-]{8,128}$")


class CommandJobStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    CANCELLING = "cancelling"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


_TERMINAL_STATUSES = frozenset(
    {
        CommandJobStatus.COMPLETED,
        CommandJobStatus.FAILED,
        CommandJobStatus.CANCELLED,
    }
)


class CommandJobError(RuntimeError):
    """Base class for stable job-manager failures."""


class CommandJobNotFound(CommandJobError):
    """The requested job is unknown or has left bounded history."""


class CommandJobCapacityError(CommandJobError):
    """The bounded queued/running capacity is full."""


class CommandJobClosedError(CommandJobError):
    """The manager has been shut down and accepts no new jobs."""


class CommandJobResultUnavailable(CommandJobError):
    """The transcript is not ready, has been consumed, or was evicted."""


class CommandJobCancelled(CommandJobError):
    """Cooperative runner signal: stop without publishing a normal result."""


@dataclass
class _JobRecord:
    id: str
    display_command: str
    command: str | None
    confirmation_token: str | None
    supplied_secrets: tuple[str, ...]
    cooperative: bool
    created_at: float
    status: CommandJobStatus = CommandJobStatus.QUEUED
    started_at: float | None = None
    finished_at: float | None = None
    cancellation_requested: bool = False
    cancellation_effective: bool | None = None
    cancel_event: threading.Event = field(default_factory=threading.Event)
    future: Future[Any] | None = None
    transcript: dict[str, Any] | None = None
    transcript_consumed: bool = False


@dataclass
class _ProjectionBudget:
    chars: int
    nodes: int = MAX_PROJECTED_NODES
    truncated: bool = False

    def consume_node(self) -> bool:
        if self.nodes <= 0:
            self.truncated = True
            return False
        self.nodes -= 1
        return True

    def consume_text(self, value: str, cap: int) -> str:
        allowed = max(0, min(cap, self.chars))
        output = value[:allowed]
        self.chars -= len(output)
        if len(output) < len(value):
            self.truncated = True
        return output


class DesktopCommandJobManager:
    """Run synchronous command bridge calls in a bounded worker pool.

    ``runner`` defaults to ``bridge.execute``.  A custom/cooperative runner may
    declare ``cancel_event`` and ``confirmation_token`` keyword parameters.  It
    must raise :class:`CommandJobCancelled` after observing cancellation; merely
    returning is treated as a completed call because its side effects cannot be
    inferred or rolled back safely.
    """

    def __init__(
        self,
        bridge: CommandBridge | None = None,
        *,
        runner: Callable[..., Any] | None = None,
        max_jobs: int = DEFAULT_MAX_JOBS,
        max_history: int = DEFAULT_MAX_HISTORY,
        max_concurrency: int = DEFAULT_MAX_CONCURRENCY,
        max_events: int = DEFAULT_MAX_EVENTS,
        max_transcript_chars: int = DEFAULT_MAX_TRANSCRIPT_CHARS,
        id_factory: Callable[[], str] | None = None,
        wall_clock: Callable[[], float] = time.time,
    ) -> None:
        for label, value in (
            ("max_jobs", max_jobs),
            ("max_history", max_history),
            ("max_concurrency", max_concurrency),
            ("max_events", max_events),
        ):
            if not isinstance(value, int) or isinstance(value, bool) or value < 1:
                raise ValueError(f"{label} must be a positive integer")
        if max_concurrency > max_jobs:
            raise ValueError("max_concurrency must not exceed max_jobs")
        if (
            not isinstance(max_transcript_chars, int)
            or isinstance(max_transcript_chars, bool)
            or max_transcript_chars < MIN_TRANSCRIPT_CHARS
        ):
            raise ValueError(
                f"max_transcript_chars must be at least {MIN_TRANSCRIPT_CHARS}"
            )

        if runner is None:
            self._bridge = bridge or CommandBridge()
            self._runner = self._bridge.execute
        else:
            self._bridge = bridge
            self._runner = runner
        self._accepts_cancel_event = _accepts_keyword(self._runner, "cancel_event")
        self._accepts_confirmation = _accepts_keyword(
            self._runner, "confirmation_token"
        )
        self._max_jobs = max_jobs
        self._max_history = max_history
        self._max_events = max_events
        self._max_transcript_chars = max_transcript_chars
        self._id_factory = id_factory or (lambda: secrets.token_urlsafe(18))
        self._wall_clock = wall_clock
        self._condition = threading.Condition(threading.RLock())
        self._records: OrderedDict[str, _JobRecord] = OrderedDict()
        self._events: deque[dict[str, Any]] = deque(maxlen=max_events)
        self._event_seq = 0
        self._closed = False
        self._executor = ThreadPoolExecutor(
            max_workers=max_concurrency,
            thread_name_prefix="maestro-command",
        )

    @property
    def closed(self) -> bool:
        with self._condition:
            return self._closed

    def submit(
        self,
        command: str,
        *,
        confirmation_token: str | None = None,
    ) -> dict[str, Any]:
        if not isinstance(command, str) or not command.strip():
            raise ValueError("command must be a non-empty string")
        if len(command) > MAX_COMMAND_CHARS or "\x00" in command:
            raise ValueError("command exceeds its safety limit or contains NUL")
        if confirmation_token is not None:
            if (
                not isinstance(confirmation_token, str)
                or not confirmation_token
                or len(confirmation_token) > MAX_CONFIRMATION_TOKEN_CHARS
                or _CONTROL_RE.search(confirmation_token)
            ):
                raise ValueError("confirmation_token is invalid")
            if not self._accepts_confirmation:
                raise ValueError(
                    "the configured runner does not accept confirmation_token"
                )

        with self._condition:
            if self._closed:
                raise CommandJobClosedError("command job manager is shut down")
            active = sum(
                record.status not in _TERMINAL_STATUSES
                for record in self._records.values()
            )
            if active >= self._max_jobs:
                raise CommandJobCapacityError("command job capacity is full")
            job_id = self._new_id_locked()
            supplied_secrets = _extract_supplied_secrets(command)
            if confirmation_token is not None:
                supplied_secrets = (*supplied_secrets, confirmation_token)
            record = _JobRecord(
                id=job_id,
                display_command=_safe_text(
                    command,
                    MAX_DISPLAY_COMMAND_CHARS,
                    supplied_secrets=supplied_secrets,
                ),
                command=command,
                confirmation_token=confirmation_token,
                supplied_secrets=supplied_secrets,
                cooperative=self._accepts_cancel_event,
                created_at=self._wall_clock(),
            )
            self._records[job_id] = record
            self._emit_locked("command_job.queued", record, {})
            future = self._executor.submit(self._run_job, job_id)
            record.future = future
            return self._snapshot_locked(record)

    def get_job(self, job_id: str) -> dict[str, Any]:
        with self._condition:
            return self._snapshot_locked(self._record_locked(job_id))

    def list_jobs(self, *, limit: int | None = None) -> list[dict[str, Any]]:
        if limit is not None and (
            not isinstance(limit, int) or isinstance(limit, bool) or limit < 1
        ):
            raise ValueError("limit must be a positive integer")
        with self._condition:
            records = list(reversed(self._records.values()))
            if limit is not None:
                records = records[:limit]
            return [self._snapshot_locked(record) for record in records]

    def cancel(self, job_id: str) -> dict[str, Any]:
        with self._condition:
            record = self._record_locked(job_id)
            if record.status in _TERMINAL_STATUSES:
                return self._snapshot_locked(record)
            first_request = not record.cancellation_requested
            record.cancellation_requested = True
            record.cancel_event.set()
            if first_request:
                self._emit_locked("command_job.cancellation_requested", record, {})

            if record.status is CommandJobStatus.QUEUED:
                future = record.future
                if future is None or future.cancel():
                    self._finish_cancelled_locked(record, cooperative=False)
                else:
                    record.status = CommandJobStatus.CANCELLING
                    self._condition.notify_all()
            elif record.status is CommandJobStatus.RUNNING:
                record.status = CommandJobStatus.CANCELLING
                self._condition.notify_all()
            return self._snapshot_locked(record)

    def wait(self, job_id: str, *, timeout: float | None = None) -> dict[str, Any]:
        if timeout is not None and (not math.isfinite(timeout) or timeout < 0):
            raise ValueError("timeout must be a finite non-negative number")
        with self._condition:
            record = self._record_locked(job_id)
            completed = self._condition.wait_for(
                lambda: record.status in _TERMINAL_STATUSES,
                timeout=timeout,
            )
            if not completed:
                return self._snapshot_locked(record)
            return self._snapshot_locked(record)

    def take_result(self, job_id: str) -> dict[str, Any]:
        """Atomically copy and remove the terminal transcript.

        Status/event APIs never include it.  A second call always fails, even if
        the first caller mutates its returned copy.
        """

        with self._condition:
            record = self._record_locked(job_id)
            if record.status not in _TERMINAL_STATUSES:
                raise CommandJobResultUnavailable("command result is not ready")
            if record.transcript is None or record.transcript_consumed:
                raise CommandJobResultUnavailable(
                    "command result was consumed or evicted"
                )
            transcript = copy.deepcopy(record.transcript)
            record.transcript = None
            record.transcript_consumed = True
            self._emit_locked("command_job.result_consumed", record, {})
            return transcript

    def events(
        self,
        *,
        after: int = 0,
        limit: int = MAX_EVENT_LIMIT,
    ) -> dict[str, Any]:
        if not isinstance(after, int) or isinstance(after, bool) or after < 0:
            raise ValueError("after must be a non-negative integer cursor")
        if (
            not isinstance(limit, int)
            or isinstance(limit, bool)
            or limit < 1
            or limit > MAX_EVENT_LIMIT
        ):
            raise ValueError(f"limit must be between 1 and {MAX_EVENT_LIMIT}")
        with self._condition:
            return self._events_locked(after=after, limit=limit)

    def wait_for_events(
        self,
        *,
        after: int = 0,
        timeout: float = 30.0,
        limit: int = MAX_EVENT_LIMIT,
    ) -> dict[str, Any]:
        if not math.isfinite(timeout) or timeout < 0 or timeout > 60:
            raise ValueError("timeout must be between 0 and 60 seconds")
        # Reuse validation without returning an unlocked stale result.
        self.events(after=after, limit=limit)
        with self._condition:
            self._condition.wait_for(
                lambda: self._event_seq > after or self._closed,
                timeout=timeout,
            )
            return self._events_locked(after=after, limit=limit)

    def shutdown(
        self,
        *,
        wait: bool = True,
        cancel_pending: bool = True,
    ) -> None:
        """Stop accepting jobs and request cancellation of unfinished work.

        ``wait=False`` never kills running threads.  Non-cooperative work keeps
        running in the background and may still perform its side effects.
        """

        with self._condition:
            if not self._closed:
                self._closed = True
                for record in list(self._records.values()):
                    if record.status in _TERMINAL_STATUSES:
                        continue
                    if not cancel_pending:
                        continue
                    record.cancellation_requested = True
                    record.cancel_event.set()
                    if record.status is CommandJobStatus.QUEUED:
                        future = record.future
                        if future is None or future.cancel():
                            self._finish_cancelled_locked(record, cooperative=False)
                        else:
                            record.status = CommandJobStatus.CANCELLING
                    elif record.status is CommandJobStatus.RUNNING:
                        record.status = CommandJobStatus.CANCELLING
                self._event_seq += 1
                self._events.append(
                    {
                        "seq": self._event_seq,
                        "type": "command_jobs.shutdown",
                        "job_id": None,
                        "timestamp": self._wall_clock(),
                        "data": {"cancel_pending": bool(cancel_pending)},
                    }
                )
                self._condition.notify_all()
        self._executor.shutdown(wait=wait, cancel_futures=cancel_pending)

    def __enter__(self) -> DesktopCommandJobManager:
        return self

    def __exit__(self, *_exc: Any) -> None:
        self.shutdown(wait=True, cancel_pending=True)

    def _run_job(self, job_id: str) -> None:
        with self._condition:
            record = self._records.get(job_id)
            if record is None or record.status in _TERMINAL_STATUSES:
                return
            if record.cancellation_requested:
                self._finish_cancelled_locked(record, cooperative=False)
                return
            record.status = CommandJobStatus.RUNNING
            record.started_at = self._wall_clock()
            command = record.command or ""
            confirmation_token = record.confirmation_token
            supplied_secrets = record.supplied_secrets
            record.command = None
            record.confirmation_token = None
            record.supplied_secrets = ()
            self._emit_locked("command_job.started", record, {})

        try:
            result = self._invoke(
                command,
                confirmation_token=confirmation_token,
                cancel_event=record.cancel_event,
            )
        except CommandJobCancelled:
            with self._condition:
                current = self._records.get(job_id)
                if current is not None:
                    self._finish_cancelled_locked(current, cooperative=True)
            return
        except BaseException as exc:  # keep all worker failures inside the job
            with self._condition:
                current = self._records.get(job_id)
                if current is not None:
                    self._finish_failed_locked(
                        current,
                        exc,
                        supplied_secrets=supplied_secrets,
                    )
            return

        with self._condition:
            current = self._records.get(job_id)
            if current is not None:
                self._finish_completed_locked(
                    current,
                    result,
                    supplied_secrets=supplied_secrets,
                )

    def _invoke(
        self,
        command: str,
        *,
        confirmation_token: str | None,
        cancel_event: threading.Event,
    ) -> Any:
        kwargs: dict[str, Any] = {}
        if confirmation_token is not None:
            kwargs["confirmation_token"] = confirmation_token
        if self._accepts_cancel_event:
            kwargs["cancel_event"] = cancel_event
        return self._runner(command, **kwargs)

    def _finish_completed_locked(
        self,
        record: _JobRecord,
        result: Any,
        *,
        supplied_secrets: tuple[str, ...],
    ) -> None:
        cancelled_after_start = record.cancellation_requested
        record.status = CommandJobStatus.COMPLETED
        record.finished_at = self._wall_clock()
        record.cancellation_effective = False if cancelled_after_start else None
        record.transcript = self._project_transcript(
            {
                "job_id": record.id,
                "status": record.status.value,
                "cancellation_requested": cancelled_after_start,
                "cancellation_effective": record.cancellation_effective,
                "result": _dispatch_shape(result),
            },
            supplied_secrets=supplied_secrets,
        )
        self._emit_locked(
            "command_job.completed",
            record,
            {
                "completed_after_cancellation": cancelled_after_start,
                "result_available": True,
            },
        )
        self._prune_history_locked()

    def _finish_failed_locked(
        self,
        record: _JobRecord,
        error: BaseException,
        *,
        supplied_secrets: tuple[str, ...],
    ) -> None:
        cancelled_after_start = record.cancellation_requested
        record.status = CommandJobStatus.FAILED
        record.finished_at = self._wall_clock()
        record.cancellation_effective = False if cancelled_after_start else None
        record.transcript = self._project_transcript(
            {
                "job_id": record.id,
                "status": record.status.value,
                "cancellation_requested": cancelled_after_start,
                "cancellation_effective": record.cancellation_effective,
                "error": {
                    "code": "command_job_failed",
                    "message": _safe_text(
                        error,
                        2_000,
                        supplied_secrets=supplied_secrets,
                    ),
                },
            },
            supplied_secrets=supplied_secrets,
        )
        self._emit_locked(
            "command_job.failed",
            record,
            {
                "failed_after_cancellation": cancelled_after_start,
                "result_available": True,
            },
        )
        self._prune_history_locked()

    def _finish_cancelled_locked(
        self,
        record: _JobRecord,
        *,
        cooperative: bool,
    ) -> None:
        if record.status in _TERMINAL_STATUSES:
            return
        record.status = CommandJobStatus.CANCELLED
        record.finished_at = self._wall_clock()
        record.command = None
        record.confirmation_token = None
        record.supplied_secrets = ()
        record.cancellation_requested = True
        record.cancellation_effective = True
        record.transcript = self._project_transcript(
            {
                "job_id": record.id,
                "status": record.status.value,
                "cancellation_requested": True,
                "cancellation_effective": True,
                "cooperative": cooperative,
            }
        )
        self._emit_locked(
            "command_job.cancelled",
            record,
            {"cooperative": cooperative, "result_available": True},
        )
        self._prune_history_locked()

    def _project_transcript(
        self,
        value: Any,
        *,
        supplied_secrets: tuple[str, ...] = (),
    ) -> dict[str, Any]:
        budget = _ProjectionBudget(chars=self._max_transcript_chars)
        try:
            projected = _project_value(
                value,
                budget=budget,
                path=(),
                supplied_secrets=supplied_secrets,
            )
        except BaseException as exc:  # keep hostile result objects contained
            return {
                "error": {
                    "code": "command_result_projection_failed",
                    "type": type(exc).__name__[:128],
                }
            }
        output = projected if isinstance(projected, dict) else {}
        if budget.truncated:
            output["truncated"] = True
        try:
            serialized_size = len(json.dumps(output, ensure_ascii=True, sort_keys=True))
        except BaseException:
            serialized_size = self._max_transcript_chars + 1
        if serialized_size > self._max_transcript_chars:
            # Enforce the configured public-output bound even when structural
            # JSON overhead or hostile mapping keys exceed the text budget.
            return _truncated_transcript_summary(value)
        return output

    def _emit_locked(
        self,
        event_type: str,
        record: _JobRecord,
        data: Mapping[str, Any],
    ) -> None:
        self._event_seq += 1
        budget = _ProjectionBudget(chars=8_000, nodes=200)
        projected = _project_value(data, budget=budget, path=("event",))
        self._events.append(
            {
                "seq": self._event_seq,
                "type": event_type,
                "job_id": record.id,
                "timestamp": self._wall_clock(),
                "data": projected if isinstance(projected, dict) else {},
            }
        )
        self._condition.notify_all()

    def _events_locked(self, *, after: int, limit: int) -> dict[str, Any]:
        values = list(self._events)
        oldest_seq = values[0]["seq"] if values else self._event_seq + 1
        gap = after < oldest_seq - 1
        available = [event for event in values if event["seq"] > after]
        limited = available[:limit]
        next_cursor = limited[-1]["seq"] if limited else max(after, oldest_seq - 1)
        return {
            "events": copy.deepcopy(limited),
            "next_cursor": next_cursor,
            "latest_cursor": self._event_seq,
            "cursor_gap": gap,
            "truncated": gap or len(available) > len(limited),
        }

    def _snapshot_locked(self, record: _JobRecord) -> dict[str, Any]:
        return {
            "id": record.id,
            "status": record.status.value,
            "command": record.display_command,
            "created_at": record.created_at,
            "started_at": record.started_at,
            "finished_at": record.finished_at,
            "cancellation_requested": record.cancellation_requested,
            "cancellation_effective": record.cancellation_effective,
            "cooperative": record.cooperative,
            "result_available": (
                record.transcript is not None and not record.transcript_consumed
            ),
            "result_consumed": record.transcript_consumed,
        }

    def _record_locked(self, job_id: str) -> _JobRecord:
        if not isinstance(job_id, str) or job_id not in self._records:
            raise CommandJobNotFound("command job was not found")
        return self._records[job_id]

    def _new_id_locked(self) -> str:
        for _attempt in range(32):
            candidate = self._id_factory()
            if (
                isinstance(candidate, str)
                and _JOB_ID_RE.fullmatch(candidate)
                and candidate not in self._records
            ):
                return candidate
        raise RuntimeError("could not generate a unique command job id")

    def _prune_history_locked(self) -> None:
        terminal = [
            record
            for record in self._records.values()
            if record.status in _TERMINAL_STATUSES
        ]
        excess = len(terminal) - self._max_history
        if excess <= 0:
            self._condition.notify_all()
            return
        terminal.sort(
            key=lambda item: (
                item.finished_at or item.created_at,
                item.created_at,
            )
        )
        for record in terminal[:excess]:
            record.transcript = None
            self._records.pop(record.id, None)
        self._condition.notify_all()


def raise_if_cancelled(cancel_event: threading.Event) -> None:
    """Cooperative runner helper with the manager's stable cancellation type."""

    if cancel_event.is_set():
        raise CommandJobCancelled("command job cancellation was observed")


def _accepts_keyword(callable_value: Callable[..., Any], name: str) -> bool:
    try:
        signature = inspect.signature(callable_value)
    except (TypeError, ValueError):
        return False
    parameter = signature.parameters.get(name)
    if parameter is not None and parameter.kind in {
        inspect.Parameter.POSITIONAL_OR_KEYWORD,
        inspect.Parameter.KEYWORD_ONLY,
    }:
        return True
    return any(
        item.kind is inspect.Parameter.VAR_KEYWORD
        for item in signature.parameters.values()
    )


def _dispatch_shape(value: Any) -> dict[str, Any]:
    if isinstance(value, CommandDispatch):
        return {"http_status": int(value.status), "payload": value.payload}
    try:
        status = getattr(value, "status", None)
        payload = getattr(value, "payload", None)
    except BaseException:
        return {"value": value}
    if isinstance(status, int) and isinstance(payload, Mapping):
        return {"http_status": status, "payload": payload}
    return {"value": value}


def _project_value(
    value: Any,
    *,
    budget: _ProjectionBudget,
    path: tuple[str, ...],
    supplied_secrets: tuple[str, ...] = (),
    depth: int = 0,
) -> Any:
    if not budget.consume_node():
        return "[truncated]"
    if depth >= MAX_PROJECTED_DEPTH:
        budget.truncated = True
        return "[depth limit]"
    if value is None or isinstance(value, bool | int):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, str):
        text = _redact_text(value, supplied_secrets=supplied_secrets)
        text = _CONTROL_RE.sub(" ", text)
        return budget.consume_text(text, MAX_PROJECTED_STRING_CHARS)
    if isinstance(value, Mapping):
        output: dict[str, Any] = {}
        for index, (raw_key, item) in enumerate(value.items()):
            if index >= 200 or budget.chars <= 0:
                budget.truncated = True
                output["__truncated__"] = True
                break
            safe_key = _safe_text(
                raw_key,
                128,
                supplied_secrets=supplied_secrets,
            )
            key = budget.consume_text(safe_key, 128)
            if not key and safe_key:
                budget.truncated = True
                break
            child_path = (*path, key)
            preserve_capability = child_path == (
                "result",
                "payload",
                "confirmation",
                "token",
            ) or (
                child_path == ("result", "payload", "ephemeral_secret")
                and _valid_ephemeral_credential(item)
            )
            if _secret_key(key) and not preserve_capability:
                output[key] = budget.consume_text("[redacted]", 10)
            else:
                output[key] = _project_value(
                    item,
                    budget=budget,
                    path=child_path,
                    supplied_secrets=supplied_secrets,
                    depth=depth + 1,
                )
        return output
    if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
        output = []
        for index, item in enumerate(value):
            if index >= 500 or budget.chars <= 0:
                budget.truncated = True
                break
            output.append(
                _project_value(
                    item,
                    budget=budget,
                    path=(*path, str(index)),
                    supplied_secrets=supplied_secrets,
                    depth=depth + 1,
                )
            )
        return output
    if hasattr(value, "model_dump"):
        try:
            return _project_value(
                value.model_dump(mode="json"),
                budget=budget,
                path=path,
                supplied_secrets=supplied_secrets,
                depth=depth + 1,
            )
        except Exception:  # noqa: BLE001 - fall through to safe text
            pass
    return _project_value(
        _redact_text(value, supplied_secrets=supplied_secrets),
        budget=budget,
        path=path,
        supplied_secrets=supplied_secrets,
        depth=depth + 1,
    )


def _secret_key(value: str) -> bool:
    normal = value.casefold().replace("-", "_")
    return any(
        word in normal
        for word in (
            "api_key",
            "password",
            "passwd",
            "secret",
            "authorization",
            "token",
            "credential",
        )
    )


def _truncated_transcript_summary(value: Any) -> dict[str, Any]:
    status = "unknown"
    if type(value) is dict and value.get("status") in {
        item.value for item in CommandJobStatus
    }:
        status = value["status"]
    return {
        "status": status,
        "error": {"code": "command_result_truncated"},
        "truncated": True,
    }


def _valid_ephemeral_credential(value: Any) -> bool:
    """Recognise the bridge's deliberately one-use credential envelope."""

    if type(value) is not dict or set(value) != {
        "kind",
        "value",
        "expires_in_seconds",
    }:
        return False
    secret = value.get("value")
    expires = value.get("expires_in_seconds")
    return (
        value.get("kind") == "credential"
        and isinstance(secret, str)
        and 20 <= len(secret) <= 256
        and not _CONTROL_RE.search(secret)
        and isinstance(expires, int)
        and not isinstance(expires, bool)
        and 0 < expires <= 3_600
    )


def _extract_supplied_secrets(command: str) -> tuple[str, ...]:
    """Extract values of obvious credential flags solely for output redaction."""

    try:
        tokens = shlex.split(command, posix=True)
    except ValueError:
        return ()
    if tokens and tokens[0].lower() in {"care", "maestro"}:
        tokens = tokens[1:]
    values: list[str] = []
    if (
        len(tokens) == 5
        and tokens[0].lower() == "/memory"
        and tokens[1].lower() == "edit"
        and tokens[2].lower() == "save"
        and tokens[4]
    ):
        values.append(tokens[4])
    redact_next = False
    for token in tokens:
        if redact_next:
            if token:
                values.append(token)
            redact_next = False
            continue
        if "=" in token:
            name, value = token.split("=", 1)
            if _credential_name(name) and value:
                values.append(value)
                continue
        if token.startswith("-") and _credential_name(token):
            redact_next = True
    return tuple(dict.fromkeys(values))


def _credential_name(value: str) -> bool:
    name = value.lstrip("-").casefold().replace("_", "-")
    return (
        any(
            word in name
            for word in (
                "api-key",
                "api-token",
                "access-token",
                "refresh-token",
                "password",
                "passwd",
                "secret",
                "credential",
                "authorization",
            )
        )
        or name == "token"
    )


def _redact_text(value: Any, *, supplied_secrets: tuple[str, ...]) -> str:
    try:
        return redact_sensitive_text(value, supplied_secrets)
    except BaseException:
        return f"<{type(value).__name__}>"


def _safe_text(
    value: Any,
    cap: int,
    *,
    supplied_secrets: tuple[str, ...] = (),
) -> str:
    text = _redact_text(value, supplied_secrets=supplied_secrets)[:cap]
    return _CONTROL_RE.sub(" ", text).strip()


__all__ = [
    "CommandJobCancelled",
    "CommandJobCapacityError",
    "CommandJobClosedError",
    "CommandJobError",
    "CommandJobNotFound",
    "CommandJobResultUnavailable",
    "CommandJobStatus",
    "DesktopCommandJobManager",
    "raise_if_cancelled",
]
