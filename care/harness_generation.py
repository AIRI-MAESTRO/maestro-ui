"""Safe, transport-neutral chain generation for the resident harness.

DeepSeek Harness owns approval and conversation policy.  This module owns one
domain operation only: ask MAGE to generate a CARL chain, admit the returned
chain through CARE's strict preflight boundary, and stage it in a private
artifact store.  It deliberately does not save, run, deploy, or evolve the
chain and it does not accept a model-supplied ``approved`` flag.

Idempotency is process-local.  A caller-supplied key is bound to the exact
request fingerprint, concurrent duplicates coalesce onto one provider task,
and terminal outcomes are retained for a bounded TTL.  A process restart
forgets this ledger; durable cross-restart idempotency belongs in a future
host-owned operation journal.
"""

from __future__ import annotations

import asyncio
from collections import Counter
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
import hashlib
import json
import math
import re
import threading
import time
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, field_validator

from care.config import CareConfig
from care.generation import build_mage_generator, run_generation
from care.harness_contracts import (
    CARE_METADATA_KEY,
    HarnessContractError,
    RUNTIME_INPUT_SCHEMA_KEY,
    chain_input_contract,
)
from care.preflight import PreflightResult, validate_chain
from care.runtime.cancellation import CancelledByUserError, CancellationToken


_IDEMPOTENCY_KEY_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{7,127}")
_SAFE_STEP_TYPE_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,79}")
_SAFE_EXCEPTION_TYPE_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_.]{0,159}")


class HarnessGenerationModel(BaseModel):
    """Strict immutable base for harness generation transport models."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        allow_inf_nan=False,
    )


class ChainGenerationRequest(HarnessGenerationModel):
    """One explicit, idempotent request to generate a candidate CARL chain.

    ``safe_task`` is required even when it equals ``query``.  Passing it to
    :func:`care.generation.run_generation` selects MAGE's typed
    ``GenerationRequest`` trust-boundary path on supported MAGE releases.
    """

    idempotency_key: str = Field(min_length=8, max_length=128)
    query: str = Field(min_length=1, max_length=200_000)
    safe_task: str = Field(min_length=1, max_length=100_000)
    mode: Literal["fast", "deep"] | None = None
    deployable: bool = False
    runtime_input_schema: dict[str, Any] | None = None

    @field_validator("idempotency_key")
    @classmethod
    def validate_idempotency_key(cls, value: str) -> str:
        if _IDEMPOTENCY_KEY_RE.fullmatch(value) is None:
            raise ValueError(
                "idempotency_key must use 8-128 letters, digits, dots, "
                "underscores, colons, or hyphens"
            )
        return value

    @field_validator("query", "safe_task")
    @classmethod
    def validate_non_blank_text(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("text must not be blank")
        if "\x00" in value:
            raise ValueError("text must not contain NUL characters")
        return value

    @field_validator("runtime_input_schema")
    @classmethod
    def validate_runtime_input_schema(
        cls,
        value: dict[str, Any] | None,
    ) -> dict[str, Any] | None:
        if value is None:
            return None
        # Reuse the exact execution/adaptation contract admission rather than
        # accepting a second, subtly different JSON Schema dialect here.
        try:
            contract = chain_input_contract(
                "generated-candidate",
                {
                    "metadata": {
                        CARE_METADATA_KEY: {RUNTIME_INPUT_SCHEMA_KEY: value}
                    },
                    "steps": [],
                },
            )
        except HarnessContractError as exc:
            raise ValueError(
                "runtime_input_schema is not an admitted contract"
            ) from exc
        return contract.schema


class GeneratedChainArtifact(HarnessGenerationModel):
    """Opaque reference to the admitted chain; never contains chain values."""

    artifact_id: str = Field(min_length=1, max_length=256)
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    size_bytes: int = Field(ge=1)
    expires_at_unix: float
    session_id: str | None = Field(default=None, min_length=1, max_length=128)


class StepTypeCount(HarnessGenerationModel):
    """Non-sensitive structural count for one admitted CARL step type."""

    step_type: str = Field(min_length=1, max_length=80)
    count: int = Field(ge=1)


class GeneratedChainSummary(HarnessGenerationModel):
    """Value-free structural summary of the admitted candidate."""

    step_count: int = Field(ge=0)
    step_types: tuple[StepTypeCount, ...]
    required_tool_count: int = Field(ge=0)
    required_mcp_server_count: int = Field(ge=0)
    required_skill_count: int = Field(ge=0)
    missing_capability_count: int = Field(ge=0)
    execution_ready: bool
    preflight_available: bool
    runtime_input_contract: bool = False


class ChainGenerationReceipt(HarnessGenerationModel):
    """Successful generation receipt safe to return through MCP or native UI."""

    status: Literal["ready"] = "ready"
    idempotency_key: str
    request_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    idempotency_scope: Literal["process_ttl_only"] = "process_ttl_only"
    replayed: bool = False
    artifact: GeneratedChainArtifact
    summary: GeneratedChainSummary


GenerationFailureCode = Literal[
    "idempotency_conflict",
    "idempotency_ledger_full",
    "provider_failed",
    "invalid_generation_result",
    "chain_admission_failed",
    "artifact_stage_failed",
]


class ChainGenerationFailure(HarnessGenerationModel):
    """Redacted, typed terminal failure suitable for a transport adapter."""

    code: GenerationFailureCode
    failure_type: str | None = Field(default=None, min_length=1, max_length=160)


class ChainGenerationOperationError(RuntimeError):
    """A generation attempt failed without exposing provider messages."""

    def __init__(self, failure: ChainGenerationFailure) -> None:
        self.failure = failure
        suffix = f" ({failure.failure_type})" if failure.failure_type else ""
        super().__init__(f"chain generation failed: {failure.code}{suffix}")


class ArtifactStage(Protocol):
    """Minimum artifact-store surface required by this operation."""

    def stage(self, payload: Mapping[str, Any]) -> Any:
        """Retain a detached object-root JSON value and return a receipt."""


GeneratorBuilder = Callable[..., Any]
GenerationRunner = Callable[..., Awaitable[Any]]
ChainValidator = Callable[..., PreflightResult]


@dataclass(frozen=True, slots=True)
class _Outcome:
    receipt: ChainGenerationReceipt | None = None
    failure: ChainGenerationFailure | None = None
    cancelled: bool = False


@dataclass(slots=True)
class _LedgerEntry:
    fingerprint: str
    loop: asyncio.AbstractEventLoop
    cancel_event: asyncio.Event
    task: asyncio.Task[_Outcome] | None = None
    outcome: _Outcome | None = None
    expires_at: float | None = None


class HarnessChainGenerationOperation:
    """Generate, strictly admit, and privately stage one candidate chain.

    The object is transport-neutral and approval-agnostic.  A DeepSeek policy
    hook (or another host) must decide whether the tool may be called.  This
    class treats an invocation as already admitted by that outer authority and
    performs no side effect beyond a bounded private artifact write.

    The idempotency ledger is intentionally in-process.  It prevents repeated
    provider calls within ``idempotency_ttl_seconds`` and coalesces concurrent
    duplicates, but cannot survive a host restart.
    """

    def __init__(
        self,
        *,
        config: CareConfig,
        artifacts: ArtifactStage,
        idempotency_ttl_seconds: float = 900.0,
        max_idempotency_entries: int = 256,
        progress: Any = None,
        capabilities: Any = None,
        clock: Callable[[], float] = time.monotonic,
        generator_builder: GeneratorBuilder = build_mage_generator,
        generation_runner: GenerationRunner = run_generation,
        chain_validator: ChainValidator = validate_chain,
    ) -> None:
        if not isinstance(config, CareConfig):
            raise TypeError("config must be a CareConfig")
        if not callable(getattr(artifacts, "stage", None)):
            raise TypeError("artifacts must expose stage(payload)")
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

        self._config = config
        self._artifacts = artifacts
        self._ttl_seconds = float(idempotency_ttl_seconds)
        self._max_entries = max_idempotency_entries
        self._progress = progress
        self._capabilities = capabilities
        self._clock = clock
        self._generator_builder = generator_builder
        self._generation_runner = generation_runner
        self._chain_validator = chain_validator
        self._ledger: dict[str, _LedgerEntry] = {}
        self._lock = threading.RLock()

    async def generate(
        self,
        request: ChainGenerationRequest,
        *,
        cancellation: CancellationToken | None = None,
    ) -> ChainGenerationReceipt:
        """Execute or replay one exact idempotent generation request."""

        if not isinstance(request, ChainGenerationRequest):
            raise TypeError("request must be a ChainGenerationRequest")
        if cancellation is not None and not isinstance(
            cancellation, CancellationToken
        ):
            raise TypeError("cancellation must be a CancellationToken")
        if cancellation is not None:
            cancellation.raise_if_cancelled()

        fingerprint = _request_fingerprint(request)
        loop = asyncio.get_running_loop()
        with self._lock:
            self._purge_expired_locked(self._now())
            entry = self._ledger.get(request.idempotency_key)
            replayed = entry is not None
            if entry is not None:
                if entry.fingerprint != fingerprint:
                    raise ChainGenerationOperationError(
                        ChainGenerationFailure(code="idempotency_conflict")
                    )
                if entry.loop is not loop and entry.outcome is None:
                    raise ChainGenerationOperationError(
                        ChainGenerationFailure(code="idempotency_conflict")
                    )
            else:
                if len(self._ledger) >= self._max_entries:
                    raise ChainGenerationOperationError(
                        ChainGenerationFailure(code="idempotency_ledger_full")
                    )
                entry = _LedgerEntry(
                    fingerprint=fingerprint,
                    loop=loop,
                    cancel_event=asyncio.Event(),
                )
                self._ledger[request.idempotency_key] = entry
                entry.task = loop.create_task(
                    self._execute_entry(entry, request, fingerprint)
                )

            self._attach_cancellation(entry, cancellation)
            task = entry.task
            outcome = entry.outcome

        if outcome is None:
            if task is None:  # pragma: no cover - protected by ledger invariant
                raise RuntimeError("idempotency entry has no task")
            # A transport coroutine being cancelled must not implicitly turn a
            # retriable key into a second provider call.  Host cancellation is
            # conveyed explicitly through ``CancellationToken``.
            outcome = await asyncio.shield(task)
        return _project_outcome(outcome, replayed=replayed)

    async def _execute_entry(
        self,
        entry: _LedgerEntry,
        request: ChainGenerationRequest,
        fingerprint: str,
    ) -> _Outcome:
        try:
            receipt = await self._generate_once(
                request,
                fingerprint=fingerprint,
                cancel_event=entry.cancel_event,
            )
            outcome = _Outcome(receipt=receipt)
        except CancelledByUserError:
            outcome = _Outcome(cancelled=True)
        except asyncio.CancelledError:
            outcome = _Outcome(cancelled=True)
        except ChainGenerationOperationError as exc:
            outcome = _Outcome(failure=exc.failure)
        except Exception as exc:  # noqa: BLE001 - last-resort redacted boundary
            outcome = _Outcome(
                failure=ChainGenerationFailure(
                    code="provider_failed",
                    failure_type=_exception_type(exc),
                )
            )

        with self._lock:
            entry.outcome = outcome
            entry.expires_at = self._now() + self._ttl_seconds
        return outcome

    async def _generate_once(
        self,
        request: ChainGenerationRequest,
        *,
        fingerprint: str,
        cancel_event: asyncio.Event,
    ) -> ChainGenerationReceipt:
        if cancel_event.is_set():
            raise CancelledByUserError("chain generation cancelled")

        try:
            generator = self._generator_builder(
                self._config,
                progress=self._progress,
                mode=request.mode,
                deployable=request.deployable,
            )
        except Exception as exc:  # noqa: BLE001 - provider/config boundary
            raise ChainGenerationOperationError(
                ChainGenerationFailure(
                    code="provider_failed",
                    failure_type=_exception_type(exc),
                )
            ) from None

        if cancel_event.is_set():
            raise CancelledByUserError("chain generation cancelled")

        generation_task = asyncio.create_task(
            self._generation_runner(
                generator,
                request.query,
                cancel=cancel_event,
                capabilities=self._capabilities,
                safe_task=request.safe_task,
            )
        )
        cancel_wait = asyncio.create_task(cancel_event.wait())
        try:
            done, _pending = await asyncio.wait(
                {generation_task, cancel_wait},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if cancel_wait in done and cancel_event.is_set():
                generation_task.cancel()
                try:
                    await generation_task
                except asyncio.CancelledError:
                    pass
                except Exception:  # cancellation cleanup must remain redacted
                    pass
                raise CancelledByUserError("chain generation cancelled")
            cancel_wait.cancel()
            try:
                await cancel_wait
            except asyncio.CancelledError:
                pass
            try:
                result = await generation_task
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - provider boundary
                raise ChainGenerationOperationError(
                    ChainGenerationFailure(
                        code="provider_failed",
                        failure_type=_exception_type(exc),
                    )
                ) from None
        finally:
            if not cancel_wait.done():
                cancel_wait.cancel()

        try:
            chain = _extract_chain_dict(result)
            if request.runtime_input_schema is not None:
                chain = _attach_runtime_input_schema(
                    chain,
                    request.runtime_input_schema,
                )
        except Exception as exc:  # noqa: BLE001 - untrusted provider result
            raise ChainGenerationOperationError(
                ChainGenerationFailure(
                    code="invalid_generation_result",
                    failure_type=_exception_type(exc),
                )
            ) from None

        try:
            preflight = self._chain_validator(
                chain,
                strict_references=True,
            )
        except Exception as exc:  # noqa: BLE001 - CARL version/runtime boundary
            raise ChainGenerationOperationError(
                ChainGenerationFailure(
                    code="chain_admission_failed",
                    failure_type=_exception_type(exc),
                )
            ) from None
        if not isinstance(preflight, PreflightResult):
            raise ChainGenerationOperationError(
                ChainGenerationFailure(
                    code="chain_admission_failed",
                    failure_type="InvalidPreflightResult",
                )
            )
        # Missing capabilities are an execution-time concern and are recorded
        # in the value-free summary.  Admission requires a strict parse with no
        # unresolved-reference or preflight-exception diagnostics.
        if not preflight.parsed or preflight.parse_errors:
            raise ChainGenerationOperationError(
                ChainGenerationFailure(code="chain_admission_failed")
            )

        try:
            staged = self._artifacts.stage(chain)
            artifact = _project_artifact_receipt(staged)
        except Exception as exc:  # noqa: BLE001 - private store boundary
            raise ChainGenerationOperationError(
                ChainGenerationFailure(
                    code="artifact_stage_failed",
                    failure_type=_exception_type(exc),
                )
            ) from None

        return ChainGenerationReceipt(
            idempotency_key=request.idempotency_key,
            request_fingerprint=fingerprint,
            artifact=artifact,
            summary=_structural_summary(chain, preflight),
        )

    def _attach_cancellation(
        self,
        entry: _LedgerEntry,
        cancellation: CancellationToken | None,
    ) -> None:
        if cancellation is None or entry.outcome is not None:
            return

        def signal(_token: CancellationToken) -> None:
            try:
                entry.loop.call_soon_threadsafe(entry.cancel_event.set)
            except RuntimeError:
                # The owning loop has already closed; there is no live provider
                # task left to notify.
                pass

        cancellation.on_cancel(signal)

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


def _request_fingerprint(request: ChainGenerationRequest) -> str:
    payload = request.model_dump(
        mode="json",
        exclude={"idempotency_key"},
    )
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _extract_chain_dict(result: Any) -> dict[str, Any]:
    if isinstance(result, Mapping):
        candidate = result.get("chain_dict")
    else:
        candidate = getattr(result, "chain_dict", None)

    if isinstance(candidate, BaseModel):
        candidate = candidate.model_dump(mode="json")
    elif not isinstance(candidate, Mapping):
        to_dict = getattr(candidate, "to_dict", None)
        if callable(to_dict):
            candidate = to_dict()
    if not isinstance(candidate, Mapping):
        raise TypeError("generation result has no object chain_dict")
    return dict(candidate)


def _project_artifact_receipt(value: Any) -> GeneratedChainArtifact:
    def field(name: str, *, default: Any = None) -> Any:
        if isinstance(value, Mapping):
            return value.get(name, default)
        return getattr(value, name, default)

    return GeneratedChainArtifact(
        artifact_id=field("artifact_id"),
        content_sha256=field("content_sha256"),
        size_bytes=field("size_bytes"),
        expires_at_unix=field("expires_at_unix"),
        session_id=field("session_id"),
    )


def _attach_runtime_input_schema(
    chain: Mapping[str, Any],
    schema: Mapping[str, Any],
) -> dict[str, Any]:
    """Attach the caller-admitted contract without replacing other metadata."""

    detached = json.loads(
        json.dumps(
            dict(chain),
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    metadata = detached.get("metadata")
    if metadata is None:
        metadata = {}
        detached["metadata"] = metadata
    if not isinstance(metadata, dict):
        raise TypeError("generated chain metadata is not an object")
    care_metadata = metadata.get(CARE_METADATA_KEY)
    if care_metadata is None:
        care_metadata = {}
        metadata[CARE_METADATA_KEY] = care_metadata
    if not isinstance(care_metadata, dict):
        raise TypeError("generated chain CARE metadata is not an object")
    care_metadata[RUNTIME_INPUT_SCHEMA_KEY] = dict(schema)
    # Defensive re-admission after the merge catches accidental mutation or
    # a malformed provider-owned metadata envelope.
    chain_input_contract("generated-candidate", detached)
    return detached


def _structural_summary(
    chain: Mapping[str, Any],
    preflight: PreflightResult,
) -> GeneratedChainSummary:
    raw_steps = chain.get("steps", [])
    steps = (
        list(raw_steps)
        if isinstance(raw_steps, Sequence)
        and not isinstance(raw_steps, (str, bytes, bytearray))
        else []
    )
    counts: Counter[str] = Counter()
    for step in steps:
        raw_type = step.get("step_type") if isinstance(step, Mapping) else None
        step_type = (
            raw_type
            if isinstance(raw_type, str)
            and _SAFE_STEP_TYPE_RE.fullmatch(raw_type) is not None
            else "other"
        )
        counts[step_type] += 1
    missing_count = sum(
        len(values)
        for values in (
            preflight.missing_tools,
            preflight.missing_mcp_servers,
            preflight.missing_skills,
        )
    )
    return GeneratedChainSummary(
        step_count=len(steps),
        step_types=tuple(
            StepTypeCount(step_type=step_type, count=count)
            for step_type, count in sorted(counts.items())
        ),
        required_tool_count=len(preflight.required_tools),
        required_mcp_server_count=len(preflight.required_mcp_servers),
        required_skill_count=len(preflight.required_skills),
        missing_capability_count=missing_count,
        execution_ready=preflight.ok,
        preflight_available=preflight.preflight_available,
        runtime_input_contract=_has_runtime_input_contract(chain),
    )


def _has_runtime_input_contract(chain: Mapping[str, Any]) -> bool:
    metadata = chain.get("metadata")
    care_metadata = (
        metadata.get(CARE_METADATA_KEY) if isinstance(metadata, Mapping) else None
    )
    return (
        isinstance(care_metadata, Mapping)
        and RUNTIME_INPUT_SCHEMA_KEY in care_metadata
    )


def _exception_type(exc: BaseException) -> str:
    """Return only a bounded exception class name, never its message."""

    selected: BaseException = exc
    cause = getattr(exc, "__cause__", None)
    if isinstance(cause, BaseException):
        selected = cause
    name = type(selected).__name__
    if _SAFE_EXCEPTION_TYPE_RE.fullmatch(name) is None:
        return "ProviderError"
    return name


def _project_outcome(
    outcome: _Outcome,
    *,
    replayed: bool,
) -> ChainGenerationReceipt:
    if outcome.cancelled:
        raise CancelledByUserError("chain generation cancelled")
    if outcome.failure is not None:
        raise ChainGenerationOperationError(outcome.failure)
    if outcome.receipt is None:  # pragma: no cover - protected by outcome invariant
        raise RuntimeError("generation outcome is empty")
    if replayed:
        return outcome.receipt.model_copy(update={"replayed": True})
    return outcome.receipt


__all__ = [
    "ChainGenerationFailure",
    "ChainGenerationOperationError",
    "ChainGenerationReceipt",
    "ChainGenerationRequest",
    "GeneratedChainArtifact",
    "GeneratedChainSummary",
    "HarnessChainGenerationOperation",
    "StepTypeCount",
]
