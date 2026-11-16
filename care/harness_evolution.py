"""Safe, transport-neutral Platform evolution operations for the harness.

DeepSeek Harness owns conversation policy and approval.  This module owns the
CARE boundary after that decision: resolve and strictly admit an exact staged
CARL seed chain, submit it through :class:`care.platform.CarePlatform`, expose
a value-bounded status snapshot, and request cancellation through the same
public facade.

The start contract deliberately has no filesystem path, arbitrary ``extras``,
provider/model override, or model-supplied ``approved`` flag.  A dataset upload
and advanced Platform options need a separate trusted-ingress design instead
of allowing an agent to smuggle local paths through a tool call.

Start and explicit-cancel effects use process-local idempotency ledgers.  A
caller key is bound to the exact request fingerprint, concurrent duplicates
coalesce, and terminal outcomes remain cached for a bounded TTL.  Platform
does not currently accept a durable idempotency token, so process restart can
still duplicate an uncertain submission.  Explicit cancellation received
while synchronous submission is in flight waits for the resulting run id and
then dispatches a best-effort server-side cancel, returning that id in a typed
receipt rather than orphaning the run.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from dataclasses import dataclass
import hashlib
import hmac
import json
import math
import re
import threading
import time
from typing import Any, Literal, Protocol

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

from care.harness_contracts import canonical_sha256
from care.preflight import PreflightResult, validate_chain
from care.runtime.cancellation import CancelledByUserError, CancellationToken


_IDEMPOTENCY_KEY_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{7,127}")
_ARTIFACT_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,255}")
_EVOLUTION_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,255}")
_TARGET_COLUMN_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_.-]{0,127}")
_SAFE_EXCEPTION_TYPE_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_.]{0,159}")


EvolutionLifecycleStatus = Literal[
    "unknown",
    "prepared",
    "pending",
    "queued",
    "dispatching",
    "initializing",
    "preparing",
    "running",
    "paused",
    "stopping",
    "completed",
    "failed",
    "cancelled",
]

_KNOWN_STATUSES = frozenset(
    {
        "unknown",
        "prepared",
        "pending",
        "queued",
        "dispatching",
        "initializing",
        "preparing",
        "running",
        "paused",
        "stopping",
        "completed",
        "failed",
        "cancelled",
    }
)
_STATUS_ALIASES = {
    "canceled": "cancelled",
    "complete": "completed",
    "succeeded": "completed",
    "success": "completed",
    "error": "failed",
}
_TERMINAL_STATUSES = frozenset({"completed", "failed", "cancelled"})


class HarnessEvolutionModel(BaseModel):
    """Strict immutable base for evolution transport models."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        allow_inf_nan=False,
    )


class EvolutionChainArtifact(HarnessEvolutionModel):
    """Opaque exact reference to one privately staged CARL chain."""

    artifact_id: str = Field(min_length=1, max_length=256)
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @field_validator("artifact_id")
    @classmethod
    def validate_artifact_id(cls, value: str) -> str:
        if _ARTIFACT_ID_RE.fullmatch(value) is None:
            raise ValueError("artifact_id has an invalid format")
        return value


class EvolutionStartRequest(HarnessEvolutionModel):
    """One explicit, idempotent request to evolve an exact seed chain.

    Local dataset paths and arbitrary Platform ``extras`` are intentionally
    absent.  They would cross a host-filesystem/provider trust boundary and
    require their own trusted native-ingress operation.
    """

    idempotency_key: str = Field(min_length=8, max_length=128)
    base_chain_id: str = Field(
        min_length=1,
        max_length=512,
        pattern=r"^[^\x00-\x1f\x7f]+$",
    )
    chain: EvolutionChainArtifact
    evolution_mode: Literal["full_chain", "per_step"] = "full_chain"
    max_iterations: int = Field(default=5, ge=1, le=100)
    population_size: int = Field(default=8, ge=2, le=128)
    validation_criteria: str | None = Field(default=None, max_length=20_000)
    target_column: str = Field(default="expected", min_length=1, max_length=128)
    validation_type: Literal["Continuous (0..1)", "Binary (0/1)"] = (
        "Continuous (0..1)"
    )
    continuous_metric: Literal["ROUGE-L", "BERTScore", "BLEU"] = "ROUGE-L"
    binary_method: Literal["equality", "substring", "regexp"] = "equality"
    regexp_pattern: str = Field(default="", max_length=2_000)
    name: str | None = Field(default=None, max_length=256)
    tags: tuple[str, ...] = Field(default=(), max_length=64)
    mutation_max_tokens: int | None = Field(default=None, ge=1, le=1_000_000)
    validation_max_tokens: int | None = Field(default=None, ge=1, le=1_000_000)

    @field_validator("idempotency_key")
    @classmethod
    def validate_idempotency_key(cls, value: str) -> str:
        if _IDEMPOTENCY_KEY_RE.fullmatch(value) is None:
            raise ValueError(
                "idempotency_key must use 8-128 letters, digits, dots, "
                "underscores, colons, or hyphens"
            )
        return value

    @field_validator("base_chain_id")
    @classmethod
    def validate_base_chain_id(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("base_chain_id must not be blank")
        return value

    @field_validator("target_column")
    @classmethod
    def validate_target_column(cls, value: str) -> str:
        if _TARGET_COLUMN_RE.fullmatch(value) is None:
            raise ValueError("target_column has an invalid format")
        return value

    @field_validator("validation_criteria", "name")
    @classmethod
    def validate_optional_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if not value.strip():
            raise ValueError("text must not be blank")
        if "\x00" in value:
            raise ValueError("text must not contain NUL characters")
        return value

    @field_validator("regexp_pattern")
    @classmethod
    def validate_regexp_pattern(cls, value: str) -> str:
        if "\x00" in value:
            raise ValueError("regexp_pattern must not contain NUL characters")
        return value

    @field_validator("tags", mode="before")
    @classmethod
    def freeze_json_tags(cls, value: Any) -> Any:
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
    def validate_regexp_configuration(self) -> EvolutionStartRequest:
        if (
            self.validation_type == "Binary (0/1)"
            and self.binary_method == "regexp"
            and not self.regexp_pattern
        ):
            raise ValueError("regexp_pattern is required for binary regexp validation")
        return self


class EvolutionStartReceipt(HarnessEvolutionModel):
    """Value-bounded handle for a submitted or immediately-cancelled run."""

    status: Literal["started", "cancel_requested"]
    idempotency_key: str
    request_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    idempotency_scope: Literal["process_ttl_only"] = "process_ttl_only"
    replayed: bool = False
    evolution_id: str = Field(min_length=1, max_length=256)
    base_chain_id: str = Field(min_length=1, max_length=512)
    chain_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    evolution_status: EvolutionLifecycleStatus
    cancellation_dispatched: bool | None = None


class EvolutionStatusRequest(HarnessEvolutionModel):
    """Read one current Platform lifecycle snapshot."""

    evolution_id: str = Field(min_length=1, max_length=256)

    @field_validator("evolution_id")
    @classmethod
    def validate_evolution_id(cls, value: str) -> str:
        if _EVOLUTION_ID_RE.fullmatch(value) is None:
            raise ValueError("evolution_id has an invalid format")
        return value


class EvolutionStatusReceipt(HarnessEvolutionModel):
    """Redacted, bounded snapshot suitable for MCP or native UI."""

    evolution_id: str = Field(min_length=1, max_length=256)
    status: EvolutionLifecycleStatus
    terminal: bool
    generation: int | None = Field(default=None, ge=0)
    max_iterations: int | None = Field(default=None, ge=0)
    best_fitness: float | None = None
    individual_count: int | None = Field(default=None, ge=0)
    error_present: bool = False


class EvolutionCancelRequest(HarnessEvolutionModel):
    """One explicit, idempotent server-side cancellation request."""

    idempotency_key: str = Field(min_length=8, max_length=128)
    evolution_id: str = Field(min_length=1, max_length=256)

    @field_validator("idempotency_key")
    @classmethod
    def validate_idempotency_key(cls, value: str) -> str:
        if _IDEMPOTENCY_KEY_RE.fullmatch(value) is None:
            raise ValueError(
                "idempotency_key must use 8-128 letters, digits, dots, "
                "underscores, colons, or hyphens"
            )
        return value

    @field_validator("evolution_id")
    @classmethod
    def validate_evolution_id(cls, value: str) -> str:
        if _EVOLUTION_ID_RE.fullmatch(value) is None:
            raise ValueError("evolution_id has an invalid format")
        return value


class EvolutionCancelReceipt(HarnessEvolutionModel):
    """Value-free acknowledgement that the cancel call was dispatched."""

    status: Literal["cancel_requested"] = "cancel_requested"
    idempotency_key: str
    request_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    idempotency_scope: Literal["process_ttl_only"] = "process_ttl_only"
    replayed: bool = False
    evolution_id: str = Field(min_length=1, max_length=256)
    cancellation_dispatched: Literal[True] = True
    evolution_status: EvolutionLifecycleStatus


EvolutionFailureCode = Literal[
    "idempotency_conflict",
    "idempotency_ledger_full",
    "artifact_unavailable",
    "chain_admission_failed",
    "evolution_start_failed",
    "invalid_evolution_result",
    "evolution_status_failed",
    "invalid_status_result",
    "evolution_cancel_failed",
    "invalid_cancel_result",
]


class EvolutionOperationFailure(HarnessEvolutionModel):
    """Redacted typed failure suitable for a transport adapter."""

    code: EvolutionFailureCode
    failure_type: str | None = Field(default=None, min_length=1, max_length=160)


class EvolutionOperationError(RuntimeError):
    """An evolution operation failed without exposing Platform messages."""

    def __init__(self, failure: EvolutionOperationFailure) -> None:
        self.failure = failure
        suffix = f" ({failure.failure_type})" if failure.failure_type else ""
        super().__init__(f"evolution operation failed: {failure.code}{suffix}")


class ArtifactResolver(Protocol):
    """Minimum private artifact surface consumed by this operation."""

    def resolve(
        self,
        artifact_id: str,
        *,
        content_sha256: str,
    ) -> dict[str, Any]: ...


class EvolutionPlatform(Protocol):
    """Public :class:`CarePlatform` lifecycle surface consumed here."""

    def start_evolution(self, **kwargs: Any) -> Any: ...

    def get_evolution(self, evolution_id: str) -> dict[str, Any]: ...

    def cancel(self, evolution_id: str) -> dict[str, Any]: ...


ChainValidator = Callable[..., PreflightResult]


@dataclass(frozen=True, slots=True)
class _StartOutcome:
    receipt: EvolutionStartReceipt | None = None
    failure: EvolutionOperationFailure | None = None
    cancelled: bool = False


@dataclass(slots=True)
class _StartLedgerEntry:
    fingerprint: str
    loop: asyncio.AbstractEventLoop
    cancel_event: asyncio.Event
    task: asyncio.Task[_StartOutcome] | None = None
    outcome: _StartOutcome | None = None
    expires_at: float | None = None


@dataclass(frozen=True, slots=True)
class _CancelOutcome:
    receipt: EvolutionCancelReceipt | None = None
    failure: EvolutionOperationFailure | None = None


@dataclass(slots=True)
class _CancelLedgerEntry:
    fingerprint: str
    loop: asyncio.AbstractEventLoop
    task: asyncio.Task[_CancelOutcome] | None = None
    outcome: _CancelOutcome | None = None
    expires_at: float | None = None


class HarnessEvolutionOperation:
    """Submit, inspect, and cancel CARE Platform evolution jobs safely.

    The object is approval-agnostic.  An outer DeepSeek policy must gate the
    effectful :meth:`start` and :meth:`cancel` calls.  Status reads are bounded
    snapshots; the harness can poll them without retaining raw Platform
    payloads in the conversation.
    """

    def __init__(
        self,
        *,
        platform: EvolutionPlatform,
        artifacts: ArtifactResolver,
        idempotency_ttl_seconds: float = 900.0,
        max_idempotency_entries: int = 256,
        clock: Callable[[], float] = time.monotonic,
        chain_validator: ChainValidator = validate_chain,
    ) -> None:
        for name in ("start_evolution", "get_evolution", "cancel"):
            if not callable(getattr(platform, name, None)):
                raise TypeError(f"platform must expose {name}(...)")
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

        self._platform = platform
        self._artifacts = artifacts
        self._ttl_seconds = float(idempotency_ttl_seconds)
        self._max_entries = max_idempotency_entries
        self._clock = clock
        self._chain_validator = chain_validator
        self._start_ledger: dict[str, _StartLedgerEntry] = {}
        self._cancel_ledger: dict[str, _CancelLedgerEntry] = {}
        self._lock = threading.RLock()

    async def start(
        self,
        request: EvolutionStartRequest,
        *,
        cancellation: CancellationToken | None = None,
    ) -> EvolutionStartReceipt:
        """Submit or replay one exact evolution request.

        Cancellation before the side effect raises
        :class:`CancelledByUserError`.  Cancellation while the synchronous
        Platform submission is already in flight cannot abort that HTTP call;
        once its run id is known this method dispatches ``CarePlatform.cancel``
        and returns a ``cancel_requested`` receipt containing the handle.
        """

        if not isinstance(request, EvolutionStartRequest):
            raise TypeError("request must be an EvolutionStartRequest")
        self._validate_cancellation(cancellation)
        if cancellation is not None:
            cancellation.raise_if_cancelled()

        fingerprint = _request_fingerprint(request)
        loop = asyncio.get_running_loop()
        with self._lock:
            self._purge_expired_locked(self._now())
            entry = self._start_ledger.get(request.idempotency_key)
            replayed = entry is not None
            if entry is not None:
                if entry.fingerprint != fingerprint or (
                    entry.loop is not loop and entry.outcome is None
                ):
                    raise EvolutionOperationError(
                        EvolutionOperationFailure(code="idempotency_conflict")
                    )
            else:
                self._ensure_capacity_locked()
                entry = _StartLedgerEntry(
                    fingerprint=fingerprint,
                    loop=loop,
                    cancel_event=asyncio.Event(),
                )
                self._start_ledger[request.idempotency_key] = entry
                entry.task = loop.create_task(
                    self._execute_start_entry(entry, request, fingerprint)
                )

            self._attach_cancellation(entry, cancellation)
            task = entry.task
            outcome = entry.outcome

        if outcome is None:
            if task is None:  # pragma: no cover - protected by ledger invariant
                raise RuntimeError("start idempotency entry has no task")
            # Transport cancellation is not authority to retry an uncertain
            # remote submit.  Only the explicit CancellationToken requests a
            # server-side cancellation.
            outcome = await asyncio.shield(task)
        return _project_start_outcome(outcome, replayed=replayed)

    async def status(
        self,
        request: EvolutionStatusRequest,
        *,
        cancellation: CancellationToken | None = None,
    ) -> EvolutionStatusReceipt:
        """Return one redacted lifecycle snapshot without caching it."""

        if not isinstance(request, EvolutionStatusRequest):
            raise TypeError("request must be an EvolutionStatusRequest")
        self._validate_cancellation(cancellation)
        if cancellation is not None:
            cancellation.raise_if_cancelled()
        try:
            payload = await asyncio.to_thread(
                self._platform.get_evolution,
                request.evolution_id,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - Platform may echo secrets
            raise EvolutionOperationError(
                EvolutionOperationFailure(
                    code="evolution_status_failed",
                    failure_type=_exception_type(exc),
                )
            ) from None
        if cancellation is not None:
            cancellation.raise_if_cancelled()
        return _project_status(request.evolution_id, payload)

    async def cancel(
        self,
        request: EvolutionCancelRequest,
        *,
        cancellation: CancellationToken | None = None,
    ) -> EvolutionCancelReceipt:
        """Dispatch or replay one exact server-side cancellation.

        The optional token is checked before the side effect.  Once the remote
        cancel call begins, cancelling the waiter does not start another call;
        callers can replay the same key to retrieve the cached acknowledgement.
        """

        if not isinstance(request, EvolutionCancelRequest):
            raise TypeError("request must be an EvolutionCancelRequest")
        self._validate_cancellation(cancellation)
        if cancellation is not None:
            cancellation.raise_if_cancelled()

        fingerprint = _request_fingerprint(request)
        loop = asyncio.get_running_loop()
        with self._lock:
            self._purge_expired_locked(self._now())
            entry = self._cancel_ledger.get(request.idempotency_key)
            replayed = entry is not None
            if entry is not None:
                if entry.fingerprint != fingerprint or (
                    entry.loop is not loop and entry.outcome is None
                ):
                    raise EvolutionOperationError(
                        EvolutionOperationFailure(code="idempotency_conflict")
                    )
            else:
                self._ensure_capacity_locked()
                entry = _CancelLedgerEntry(fingerprint=fingerprint, loop=loop)
                self._cancel_ledger[request.idempotency_key] = entry
                entry.task = loop.create_task(
                    self._execute_cancel_entry(entry, request, fingerprint)
                )

            task = entry.task
            outcome = entry.outcome

        if outcome is None:
            if task is None:  # pragma: no cover - protected by ledger invariant
                raise RuntimeError("cancel idempotency entry has no task")
            outcome = await asyncio.shield(task)
        return _project_cancel_outcome(outcome, replayed=replayed)

    async def _execute_start_entry(
        self,
        entry: _StartLedgerEntry,
        request: EvolutionStartRequest,
        fingerprint: str,
    ) -> _StartOutcome:
        try:
            receipt = await self._start_once(
                request,
                fingerprint=fingerprint,
                cancel_event=entry.cancel_event,
            )
            outcome = _StartOutcome(receipt=receipt)
        except (CancelledByUserError, asyncio.CancelledError):
            outcome = _StartOutcome(cancelled=True)
        except EvolutionOperationError as exc:
            outcome = _StartOutcome(failure=exc.failure)
        except Exception as exc:  # noqa: BLE001 - last redacted boundary
            outcome = _StartOutcome(
                failure=EvolutionOperationFailure(
                    code="evolution_start_failed",
                    failure_type=_exception_type(exc),
                )
            )

        with self._lock:
            entry.outcome = outcome
            entry.expires_at = self._now() + self._ttl_seconds
        return outcome

    async def _start_once(
        self,
        request: EvolutionStartRequest,
        *,
        fingerprint: str,
        cancel_event: asyncio.Event,
    ) -> EvolutionStartReceipt:
        self._raise_if_cancelled(cancel_event)
        payload = self._resolve_artifact(request.chain)
        self._validate_chain(payload)
        self._raise_if_cancelled(cancel_event)

        kwargs: dict[str, Any] = {
            "base_chain_id": request.base_chain_id,
            "base_chain_content": payload,
            "evolution_mode": request.evolution_mode,
            "max_iterations": request.max_iterations,
            "population_size": request.population_size,
            "validation_criteria": request.validation_criteria,
            "target_column": request.target_column,
            "validation_type": request.validation_type,
            "continuous_metric": request.continuous_metric,
            "binary_method": request.binary_method,
            "regexp_pattern": request.regexp_pattern,
            "name": request.name,
            "tags": list(request.tags) or None,
            "mutation_max_tokens": request.mutation_max_tokens,
            "validation_max_tokens": request.validation_max_tokens,
        }
        try:
            ref = await asyncio.to_thread(self._platform.start_evolution, **kwargs)
        except Exception as exc:  # noqa: BLE001 - Platform may echo secrets
            raise EvolutionOperationError(
                EvolutionOperationFailure(
                    code="evolution_start_failed",
                    failure_type=_exception_type(exc),
                )
            ) from None

        evolution_id, base_chain_id, evolution_status = _project_start_ref(ref)
        if not hmac.compare_digest(base_chain_id, request.base_chain_id):
            raise EvolutionOperationError(
                EvolutionOperationFailure(code="invalid_evolution_result")
            )

        cancellation_dispatched: bool | None = None
        receipt_status: Literal["started", "cancel_requested"] = "started"
        if cancel_event.is_set():
            receipt_status = "cancel_requested"
            cancellation_dispatched = await self._best_effort_cancel(evolution_id)
            if cancellation_dispatched:
                evolution_status = "stopping"

        return EvolutionStartReceipt(
            status=receipt_status,
            idempotency_key=request.idempotency_key,
            request_fingerprint=fingerprint,
            evolution_id=evolution_id,
            base_chain_id=base_chain_id,
            chain_digest=request.chain.content_sha256,
            evolution_status=evolution_status,
            cancellation_dispatched=cancellation_dispatched,
        )

    async def _execute_cancel_entry(
        self,
        entry: _CancelLedgerEntry,
        request: EvolutionCancelRequest,
        fingerprint: str,
    ) -> _CancelOutcome:
        try:
            receipt = await self._cancel_once(request, fingerprint=fingerprint)
            outcome = _CancelOutcome(receipt=receipt)
        except EvolutionOperationError as exc:
            outcome = _CancelOutcome(failure=exc.failure)
        except Exception as exc:  # noqa: BLE001 - last redacted boundary
            outcome = _CancelOutcome(
                failure=EvolutionOperationFailure(
                    code="evolution_cancel_failed",
                    failure_type=_exception_type(exc),
                )
            )

        with self._lock:
            entry.outcome = outcome
            entry.expires_at = self._now() + self._ttl_seconds
        return outcome

    async def _cancel_once(
        self,
        request: EvolutionCancelRequest,
        *,
        fingerprint: str,
    ) -> EvolutionCancelReceipt:
        try:
            payload = await asyncio.to_thread(
                self._platform.cancel,
                request.evolution_id,
            )
        except Exception as exc:  # noqa: BLE001 - uncertain remote effect
            raise EvolutionOperationError(
                EvolutionOperationFailure(
                    code="evolution_cancel_failed",
                    failure_type=_exception_type(exc),
                )
            ) from None
        if not isinstance(payload, Mapping):
            raise EvolutionOperationError(
                EvolutionOperationFailure(code="invalid_cancel_result")
            )
        return EvolutionCancelReceipt(
            idempotency_key=request.idempotency_key,
            request_fingerprint=fingerprint,
            evolution_id=request.evolution_id,
            evolution_status=_normalize_status(payload.get("status"), "stopping"),
        )

    def _resolve_artifact(
        self,
        reference: EvolutionChainArtifact,
    ) -> dict[str, Any]:
        try:
            payload = self._artifacts.resolve(
                reference.artifact_id,
                content_sha256=reference.content_sha256,
            )
        except Exception as exc:  # noqa: BLE001 - private artifact boundary
            raise EvolutionOperationError(
                EvolutionOperationFailure(
                    code="artifact_unavailable",
                    failure_type=_exception_type(exc),
                )
            ) from None
        if not isinstance(payload, Mapping):
            raise EvolutionOperationError(
                EvolutionOperationFailure(code="artifact_unavailable")
            )
        detached = dict(payload)
        try:
            actual_digest = canonical_sha256(detached)
        except Exception as exc:  # noqa: BLE001 - malformed artifact boundary
            raise EvolutionOperationError(
                EvolutionOperationFailure(
                    code="artifact_unavailable",
                    failure_type=_exception_type(exc),
                )
            ) from None
        if not hmac.compare_digest(actual_digest, reference.content_sha256):
            raise EvolutionOperationError(
                EvolutionOperationFailure(code="artifact_unavailable")
            )
        return detached

    def _validate_chain(self, payload: Mapping[str, Any]) -> PreflightResult:
        try:
            preflight = self._chain_validator(payload, strict_references=True)
        except Exception as exc:  # noqa: BLE001 - CARL boundary
            raise EvolutionOperationError(
                EvolutionOperationFailure(
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
            raise EvolutionOperationError(
                EvolutionOperationFailure(code="chain_admission_failed")
            )
        return preflight

    async def _best_effort_cancel(self, evolution_id: str) -> bool:
        try:
            await asyncio.to_thread(self._platform.cancel, evolution_id)
        except Exception:  # cancellation receipt explicitly reports failure
            return False
        return True

    def _ensure_capacity_locked(self) -> None:
        if len(self._start_ledger) + len(self._cancel_ledger) >= self._max_entries:
            raise EvolutionOperationError(
                EvolutionOperationFailure(code="idempotency_ledger_full")
            )

    def _purge_expired_locked(self, now: float) -> None:
        for ledger in (self._start_ledger, self._cancel_ledger):
            expired = [
                key
                for key, entry in ledger.items()
                if entry.outcome is not None
                and entry.expires_at is not None
                and entry.expires_at <= now
            ]
            for key in expired:
                del ledger[key]

    def _now(self) -> float:
        now = float(self._clock())
        if not math.isfinite(now):
            raise RuntimeError("idempotency clock returned a non-finite value")
        return now

    @staticmethod
    def _attach_cancellation(
        entry: _StartLedgerEntry,
        cancellation: CancellationToken | None,
    ) -> None:
        if cancellation is None:
            return

        def signal(_token: CancellationToken) -> None:
            try:
                entry.loop.call_soon_threadsafe(entry.cancel_event.set)
            except RuntimeError:
                pass

        cancellation.on_cancel(signal)

    @staticmethod
    def _validate_cancellation(cancellation: CancellationToken | None) -> None:
        if cancellation is not None and not isinstance(
            cancellation, CancellationToken
        ):
            raise TypeError("cancellation must be a CancellationToken")

    @staticmethod
    def _raise_if_cancelled(cancel_event: asyncio.Event) -> None:
        if cancel_event.is_set():
            raise CancelledByUserError("evolution start cancelled")


def _request_fingerprint(request: BaseModel) -> str:
    payload = request.model_dump(mode="json", exclude={"idempotency_key"})
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _project_start_ref(value: Any) -> tuple[str, str, EvolutionLifecycleStatus]:
    def field(name: str, default: Any = None) -> Any:
        if isinstance(value, Mapping):
            return value.get(name, default)
        return getattr(value, name, default)

    evolution_id = field("evolution_id") or field("id")
    base_chain_id = field("base_chain_id")
    if (
        not isinstance(evolution_id, str)
        or _EVOLUTION_ID_RE.fullmatch(evolution_id) is None
        or not isinstance(base_chain_id, str)
        or not base_chain_id.strip()
        or "\x00" in base_chain_id
        or len(base_chain_id) > 512
    ):
        raise EvolutionOperationError(
            EvolutionOperationFailure(code="invalid_evolution_result")
        )
    return evolution_id, base_chain_id, _normalize_status(field("status"), "queued")


def _project_status(
    evolution_id: str,
    payload: Any,
) -> EvolutionStatusReceipt:
    if not isinstance(payload, Mapping):
        raise EvolutionOperationError(
            EvolutionOperationFailure(code="invalid_status_result")
        )
    status = _normalize_status(payload.get("status"), "unknown")
    generation = _nonnegative_int(payload.get("generation"))
    max_iterations = _nonnegative_int(payload.get("max_iterations"))
    best_fitness = _finite_float(payload.get("best_fitness"))
    individuals = payload.get("individuals")
    individual_count = len(individuals) if isinstance(individuals, list) else None
    error_present = status == "failed" or bool(payload.get("error_message"))
    return EvolutionStatusReceipt(
        evolution_id=evolution_id,
        status=status,
        terminal=status in _TERMINAL_STATUSES,
        generation=generation,
        max_iterations=max_iterations,
        best_fitness=best_fitness,
        individual_count=individual_count,
        error_present=error_present,
    )


def _normalize_status(
    value: Any,
    default: EvolutionLifecycleStatus,
) -> EvolutionLifecycleStatus:
    if not isinstance(value, str):
        return default
    normalized = _STATUS_ALIASES.get(value.strip().lower(), value.strip().lower())
    if normalized not in _KNOWN_STATUSES:
        return "unknown"
    return normalized  # type: ignore[return-value]


def _nonnegative_int(value: Any) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    return None


def _finite_float(value: Any) -> float | None:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return None
    selected = float(value)
    return selected if math.isfinite(selected) else None


def _exception_type(exc: BaseException) -> str:
    """Return only a bounded exception class name, never its message."""

    selected: BaseException = exc
    cause = getattr(exc, "__cause__", None)
    if isinstance(cause, BaseException):
        selected = cause
    name = type(selected).__name__
    if _SAFE_EXCEPTION_TYPE_RE.fullmatch(name) is None:
        return "EvolutionError"
    return name


def _project_start_outcome(
    outcome: _StartOutcome,
    *,
    replayed: bool,
) -> EvolutionStartReceipt:
    if outcome.cancelled:
        raise CancelledByUserError("evolution start cancelled")
    if outcome.failure is not None:
        raise EvolutionOperationError(outcome.failure)
    if outcome.receipt is None:  # pragma: no cover - protected by invariant
        raise RuntimeError("evolution start outcome is empty")
    if replayed:
        return outcome.receipt.model_copy(update={"replayed": True})
    return outcome.receipt


def _project_cancel_outcome(
    outcome: _CancelOutcome,
    *,
    replayed: bool,
) -> EvolutionCancelReceipt:
    if outcome.failure is not None:
        raise EvolutionOperationError(outcome.failure)
    if outcome.receipt is None:  # pragma: no cover - protected by invariant
        raise RuntimeError("evolution cancel outcome is empty")
    if replayed:
        return outcome.receipt.model_copy(update={"replayed": True})
    return outcome.receipt


__all__ = [
    "EvolutionCancelReceipt",
    "EvolutionCancelRequest",
    "EvolutionChainArtifact",
    "EvolutionLifecycleStatus",
    "EvolutionOperationError",
    "EvolutionOperationFailure",
    "EvolutionStartReceipt",
    "EvolutionStartRequest",
    "EvolutionStatusReceipt",
    "EvolutionStatusRequest",
    "HarnessEvolutionOperation",
]
