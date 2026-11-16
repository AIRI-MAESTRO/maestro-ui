"""Safe, transport-neutral persistence of staged CARL chains to Memory.

The resident harness stages generated chains in a private artifact store.  This
module resolves one exact ``artifact_id`` plus SHA-256 digest, reparses it at
the durable-write boundary with strict CARL reference checking, and calls the
public :meth:`care.memory.CareMemory.save_chain` surface with the resulting
typed chain.  It never accepts an ``approved`` flag: effect approval belongs to
the outer DeepSeek Harness policy.

Idempotency is deliberately process-local.  A caller key is bound to the exact
request fingerprint, concurrent duplicates coalesce onto one Memory call, and
terminal outcomes are retained for a bounded TTL.  The Memory API does not
currently accept a durable idempotency token, so process restart and a retry
under a different key can still create a duplicate entity.  A failed response
is cached because the remote write may have committed before the failure was
observed.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from dataclasses import dataclass
import hashlib
import json
import math
import re
import threading
import time
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from care.preflight import PreflightResult, validate_chain


_IDEMPOTENCY_KEY_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{7,127}")
_ARTIFACT_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}")
_SAFE_EXCEPTION_TYPE_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_.]{0,159}")


class HarnessMemoryWriteModel(BaseModel):
    """Strict immutable base for Memory-write transport models."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        allow_inf_nan=False,
    )


class ChainMemoryWriteRequest(HarnessMemoryWriteModel):
    """Persist or version one digest-pinned staged chain.

    Supplying ``entity_id`` creates a new version of that entity.  Explicit
    lineage fields are valid only for version writes.  User-provided metadata
    is written to Memory but is never echoed in the public receipt.
    """

    idempotency_key: str = Field(min_length=8, max_length=128)
    artifact_id: str = Field(min_length=1, max_length=128)
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    name: str = Field(min_length=1, max_length=256)
    query: str | None = Field(default=None, max_length=200_000)
    description: str | None = Field(default=None, max_length=20_000)
    domain: str | None = Field(default=None, max_length=256)
    tags: tuple[str, ...] = Field(default=(), max_length=64)
    when_to_use: str | None = Field(default=None, max_length=20_000)
    author: str | None = Field(default=None, max_length=256)
    entity_id: str | None = Field(default=None, max_length=512)
    channel: Literal["latest", "stable"] = "latest"
    parent_version_id: str | None = Field(default=None, max_length=512)
    change_summary: str | None = Field(default=None, max_length=2_000)

    @field_validator("idempotency_key")
    @classmethod
    def validate_idempotency_key(cls, value: str) -> str:
        if _IDEMPOTENCY_KEY_RE.fullmatch(value) is None:
            raise ValueError(
                "idempotency_key must use 8-128 letters, digits, dots, "
                "underscores, colons, or hyphens"
            )
        return value

    @field_validator("artifact_id")
    @classmethod
    def validate_artifact_id(cls, value: str) -> str:
        if _ARTIFACT_ID_RE.fullmatch(value) is None:
            raise ValueError("artifact_id has an invalid format")
        return value

    @field_validator(
        "name",
        "query",
        "description",
        "domain",
        "when_to_use",
        "author",
        "entity_id",
        "parent_version_id",
        "change_summary",
    )
    @classmethod
    def validate_optional_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if not value.strip():
            raise ValueError("text must not be blank")
        if "\x00" in value:
            raise ValueError("text must not contain NUL characters")
        return value

    @field_validator("tags", mode="before")
    @classmethod
    def freeze_json_tags(cls, value: Any) -> Any:
        # MCP carries arrays as Python lists.  Convert that one exact JSON
        # representation to an immutable tuple before strict item validation;
        # no scalar or element coercion is performed.
        if isinstance(value, list):
            return tuple(value)
        return value

    @field_validator("tags")
    @classmethod
    def validate_tags(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(set(value)) != len(value):
            raise ValueError("tags must not contain duplicates")
        for tag in value:
            if not tag.strip() or "\x00" in tag or len(tag) > 256:
                raise ValueError("each tag must be 1-256 non-NUL characters")
        return value

    @model_validator(mode="after")
    def validate_version_lineage(self) -> ChainMemoryWriteRequest:
        if self.entity_id is None and (
            self.parent_version_id is not None or self.change_summary is not None
        ):
            raise ValueError(
                "parent_version_id and change_summary require entity_id"
            )
        return self


class ChainMemoryWriteReceipt(HarnessMemoryWriteModel):
    """Value-free receipt for one durable Memory write."""

    status: Literal["saved"] = "saved"
    operation: Literal["created", "versioned"]
    idempotency_key: str
    request_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    idempotency_scope: Literal["process_ttl_only"] = "process_ttl_only"
    replayed: bool = False
    entity_id: str = Field(min_length=1, max_length=512)
    channel: Literal["latest", "stable"]
    artifact_id: str = Field(min_length=1, max_length=128)
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


MemoryWriteFailureCode = Literal[
    "idempotency_conflict",
    "idempotency_ledger_full",
    "artifact_unavailable",
    "chain_admission_failed",
    "memory_save_failed",
    "invalid_memory_result",
]


class ChainMemoryWriteFailure(HarnessMemoryWriteModel):
    """Redacted terminal failure suitable for an MCP transport adapter."""

    code: MemoryWriteFailureCode
    failure_type: str | None = Field(default=None, min_length=1, max_length=160)


class ChainMemoryWriteOperationError(RuntimeError):
    """A Memory write failed without exposing artifact or provider values."""

    def __init__(self, failure: ChainMemoryWriteFailure) -> None:
        self.failure = failure
        suffix = f" ({failure.failure_type})" if failure.failure_type else ""
        super().__init__(f"chain Memory write failed: {failure.code}{suffix}")


class ArtifactResolver(Protocol):
    """Minimum private artifact surface required by this operation."""

    def resolve(
        self,
        artifact_id: str,
        *,
        content_sha256: str,
    ) -> dict[str, Any]:
        """Resolve a detached object-root JSON payload by exact digest."""


class MemoryWriter(Protocol):
    """Public :class:`CareMemory` write surface consumed here."""

    def save_chain(self, chain: Any, *, name: str, **kwargs: Any) -> str:
        """Create or version a chain and return its entity id."""


ChainValidator = Callable[..., PreflightResult]


@dataclass(frozen=True, slots=True)
class _Outcome:
    receipt: ChainMemoryWriteReceipt | None = None
    failure: ChainMemoryWriteFailure | None = None


@dataclass(slots=True)
class _LedgerEntry:
    fingerprint: str
    loop: asyncio.AbstractEventLoop
    task: asyncio.Task[_Outcome] | None = None
    outcome: _Outcome | None = None
    expires_at: float | None = None


class HarnessChainMemoryWriteOperation:
    """Strictly admit and persist one staged chain through ``CareMemory``.

    Approval is intentionally outside this class.  A DeepSeek Harness policy
    hook (or equivalent trusted host) must authorize the effectful tool before
    invoking this operation.
    """

    def __init__(
        self,
        *,
        memory: MemoryWriter,
        artifacts: ArtifactResolver,
        idempotency_ttl_seconds: float = 900.0,
        max_idempotency_entries: int = 256,
        clock: Callable[[], float] = time.monotonic,
        chain_validator: ChainValidator = validate_chain,
    ) -> None:
        if not callable(getattr(memory, "save_chain", None)):
            raise TypeError("memory must expose save_chain(chain, *, name, ...)")
        if not callable(getattr(artifacts, "resolve", None)):
            raise TypeError(
                "artifacts must expose resolve(artifact_id, *, content_sha256)"
            )
        if (
            not isinstance(idempotency_ttl_seconds, (int, float))
            or isinstance(idempotency_ttl_seconds, bool)
            or not math.isfinite(float(idempotency_ttl_seconds))
            or float(idempotency_ttl_seconds) <= 0
        ):
            raise ValueError("idempotency_ttl_seconds must be positive and finite")
        if (
            not isinstance(max_idempotency_entries, int)
            or isinstance(max_idempotency_entries, bool)
            or max_idempotency_entries < 1
        ):
            raise ValueError("max_idempotency_entries must be positive")
        if not callable(clock):
            raise TypeError("clock must be callable")
        if not callable(chain_validator):
            raise TypeError("chain_validator must be callable")

        self._memory = memory
        self._artifacts = artifacts
        self._ttl_seconds = float(idempotency_ttl_seconds)
        self._max_entries = max_idempotency_entries
        self._clock = clock
        self._chain_validator = chain_validator
        self._ledger: dict[str, _LedgerEntry] = {}
        self._lock = threading.RLock()

    async def save(
        self,
        request: ChainMemoryWriteRequest,
    ) -> ChainMemoryWriteReceipt:
        """Execute or replay one exact idempotent Memory write request."""

        if not isinstance(request, ChainMemoryWriteRequest):
            raise TypeError("request must be a ChainMemoryWriteRequest")

        fingerprint = _request_fingerprint(request)
        loop = asyncio.get_running_loop()
        with self._lock:
            self._purge_expired_locked(self._now())
            entry = self._ledger.get(request.idempotency_key)
            replayed = entry is not None
            if entry is not None:
                if entry.fingerprint != fingerprint:
                    raise ChainMemoryWriteOperationError(
                        ChainMemoryWriteFailure(code="idempotency_conflict")
                    )
                if entry.loop is not loop and entry.outcome is None:
                    raise ChainMemoryWriteOperationError(
                        ChainMemoryWriteFailure(code="idempotency_conflict")
                    )
            else:
                if len(self._ledger) >= self._max_entries:
                    raise ChainMemoryWriteOperationError(
                        ChainMemoryWriteFailure(code="idempotency_ledger_full")
                    )
                entry = _LedgerEntry(fingerprint=fingerprint, loop=loop)
                self._ledger[request.idempotency_key] = entry
                entry.task = loop.create_task(
                    self._execute_entry(entry, request, fingerprint)
                )

            task = entry.task
            outcome = entry.outcome

        if outcome is None:
            if task is None:  # pragma: no cover - protected by ledger invariant
                raise RuntimeError("idempotency entry has no task")
            # Cancelling one transport waiter must not cancel an in-flight
            # write whose remote commit status may already be uncertain.
            outcome = await asyncio.shield(task)
        return _project_outcome(outcome, replayed=replayed)

    async def _execute_entry(
        self,
        entry: _LedgerEntry,
        request: ChainMemoryWriteRequest,
        fingerprint: str,
    ) -> _Outcome:
        try:
            receipt = await self._save_once(request, fingerprint=fingerprint)
            outcome = _Outcome(receipt=receipt)
        except ChainMemoryWriteOperationError as exc:
            outcome = _Outcome(failure=exc.failure)
        except Exception as exc:  # noqa: BLE001 - last-resort redacted boundary
            outcome = _Outcome(
                failure=ChainMemoryWriteFailure(
                    code="memory_save_failed",
                    failure_type=_exception_type(exc),
                )
            )

        with self._lock:
            entry.outcome = outcome
            entry.expires_at = self._now() + self._ttl_seconds
        return outcome

    async def _save_once(
        self,
        request: ChainMemoryWriteRequest,
        *,
        fingerprint: str,
    ) -> ChainMemoryWriteReceipt:
        try:
            payload = self._artifacts.resolve(
                request.artifact_id,
                content_sha256=request.content_sha256,
            )
        except Exception as exc:  # noqa: BLE001 - private store boundary
            raise ChainMemoryWriteOperationError(
                ChainMemoryWriteFailure(
                    code="artifact_unavailable",
                    failure_type=_exception_type(exc),
                )
            ) from None
        if not isinstance(payload, Mapping):
            raise ChainMemoryWriteOperationError(
                ChainMemoryWriteFailure(code="artifact_unavailable")
            )

        try:
            preflight = self._chain_validator(
                payload,
                strict_references=True,
            )
        except Exception as exc:  # noqa: BLE001 - CARL boundary
            raise ChainMemoryWriteOperationError(
                ChainMemoryWriteFailure(
                    code="chain_admission_failed",
                    failure_type=_exception_type(exc),
                )
            ) from None
        if (
            not isinstance(preflight, PreflightResult)
            or not preflight.parsed
            or preflight.parse_errors
            or preflight.chain is None
        ):
            raise ChainMemoryWriteOperationError(
                ChainMemoryWriteFailure(code="chain_admission_failed")
            )

        save_kwargs: dict[str, Any] = {
            "name": request.name,
            "query": request.query,
            "description": request.description,
            "domain": request.domain,
            "tags": list(request.tags) or None,
            "when_to_use": request.when_to_use,
            "author": request.author,
            "entity_id": request.entity_id,
            "channel": request.channel,
            "parent_version_id": request.parent_version_id,
            "change_summary": request.change_summary,
        }
        try:
            entity_id = await asyncio.to_thread(
                self._memory.save_chain,
                preflight.chain,
                **save_kwargs,
            )
        except Exception as exc:  # noqa: BLE001 - Memory may echo private values
            raise ChainMemoryWriteOperationError(
                ChainMemoryWriteFailure(
                    code="memory_save_failed",
                    failure_type=_exception_type(exc),
                )
            ) from None

        if (
            not isinstance(entity_id, str)
            or not entity_id.strip()
            or "\x00" in entity_id
            or len(entity_id) > 512
            or (request.entity_id is not None and entity_id != request.entity_id)
        ):
            raise ChainMemoryWriteOperationError(
                ChainMemoryWriteFailure(code="invalid_memory_result")
            )

        return ChainMemoryWriteReceipt(
            operation="versioned" if request.entity_id is not None else "created",
            idempotency_key=request.idempotency_key,
            request_fingerprint=fingerprint,
            entity_id=entity_id,
            channel=request.channel,
            artifact_id=request.artifact_id,
            content_sha256=request.content_sha256,
        )

    def _purge_expired_locked(self, now: float) -> None:
        expired = [
            key
            for key, entry in self._ledger.items()
            if entry.outcome is not None
            and entry.expires_at is not None
            and entry.expires_at <= now
        ]
        for key in expired:
            del self._ledger[key]

    def _now(self) -> float:
        now = float(self._clock())
        if not math.isfinite(now):
            raise RuntimeError("idempotency clock returned a non-finite value")
        return now


def _request_fingerprint(request: ChainMemoryWriteRequest) -> str:
    payload = request.model_dump(mode="json", exclude={"idempotency_key"})
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _exception_type(exc: BaseException) -> str:
    """Return only a bounded exception class name, never its message."""

    selected: BaseException = exc
    cause = getattr(exc, "__cause__", None)
    if isinstance(cause, BaseException):
        selected = cause
    name = type(selected).__name__
    if _SAFE_EXCEPTION_TYPE_RE.fullmatch(name) is None:
        return "MemoryWriteError"
    return name


def _project_outcome(
    outcome: _Outcome,
    *,
    replayed: bool,
) -> ChainMemoryWriteReceipt:
    if outcome.failure is not None:
        raise ChainMemoryWriteOperationError(outcome.failure)
    if outcome.receipt is None:  # pragma: no cover - protected by outcome invariant
        raise RuntimeError("Memory write outcome is empty")
    if replayed:
        return outcome.receipt.model_copy(update={"replayed": True})
    return outcome.receipt


__all__ = [
    "ChainMemoryWriteFailure",
    "ChainMemoryWriteOperationError",
    "ChainMemoryWriteReceipt",
    "ChainMemoryWriteRequest",
    "HarnessChainMemoryWriteOperation",
]
