"""Safe, transport-neutral CARL chain execution for the resident harness.

DeepSeek Harness owns conversation policy and the approval prompt.  This
module owns the CARE execution boundary after that host decision: resolve an
exact chain and input from the private artifact store, revalidate both against
the pinned chain contract, build CARE's configured CARL runtime, execute it,
and retain a bounded result artifact.

No request field is a filesystem path and there is deliberately no
model-supplied ``approved`` boolean.  Input is either an exact digest-pinned
artifact that already satisfies the chain's declared JSON Schema, or a
single-use lossless-adaptation preview created by :mod:`care.harness_contracts`.

Idempotency is process-local.  Concurrent duplicates coalesce onto one CARL
task and terminal outcomes remain cached for a bounded TTL.  A process restart
forgets this ledger; durable cross-restart idempotency belongs in a future
host-owned operation journal.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
import copy
from dataclasses import dataclass
import hashlib
import hmac
import inspect
import json
import math
import re
import threading
import time
from typing import Annotated, Any, Literal, Protocol

from jsonschema import Draft202012Validator
from pydantic import BaseModel, ConfigDict, Field, field_validator

from care.config import CareConfig
from care.harness_contracts import (
    AdaptationPreviewStore,
    HarnessContractError,
    InvalidInputContractError,
    MissingInputContractError,
    PreviewUnavailableError,
    canonical_sha256,
    chain_input_contract,
)
from care.preflight import PreflightResult, validate_chain
from care.runtime.cancellation import CancelledByUserError, CancellationToken
from care.runtime.executor import build_run_context, execute_chain_async
from care.runtime.llm_client import build_carl_llm_client
from care.runtime.run_recorder import extract_final_output


_IDEMPOTENCY_KEY_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{7,127}")
_OPAQUE_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,256}")
_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_SAFE_EXCEPTION_TYPE_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_.]{0,159}")


class HarnessExecutionModel(BaseModel):
    """Strict immutable base for execution transport models."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        allow_inf_nan=False,
    )


class ChainArtifactReference(HarnessExecutionModel):
    """Opaque exact reference to one staged CARL chain object."""

    artifact_id: str = Field(min_length=1, max_length=256)
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @field_validator("artifact_id")
    @classmethod
    def validate_artifact_id(cls, value: str) -> str:
        if _OPAQUE_ID_RE.fullmatch(value) is None:
            raise ValueError("artifact_id has an invalid format")
        return value


class ExactInputArtifact(HarnessExecutionModel):
    """An already contract-conforming, digest-pinned JSON input object."""

    kind: Literal["artifact"] = "artifact"
    artifact_id: str = Field(min_length=1, max_length=256)
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @field_validator("artifact_id")
    @classmethod
    def validate_artifact_id(cls, value: str) -> str:
        if _OPAQUE_ID_RE.fullmatch(value) is None:
            raise ValueError("artifact_id has an invalid format")
        return value


class AdaptationPreviewInput(HarnessExecutionModel):
    """Exact confirmation binding for one ready, single-use preview."""

    kind: Literal["adaptation_preview"] = "adaptation_preview"
    preview_id: str = Field(min_length=1, max_length=256)
    chain_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    schema_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    adapted_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    confirmation_digest: str = Field(pattern=r"^[0-9a-f]{64}$")

    @field_validator("preview_id")
    @classmethod
    def validate_preview_id(cls, value: str) -> str:
        # Process-local preview ids currently use URL-safe random tokens.  Keep
        # the transport grammar narrower than an arbitrary path/string.
        if _OPAQUE_ID_RE.fullmatch(value) is None:
            raise ValueError("preview_id has an invalid format")
        return value


ExecutionInput = Annotated[
    ExactInputArtifact | AdaptationPreviewInput,
    Field(discriminator="kind"),
]


class ChainExecutionRequest(HarnessExecutionModel):
    """One explicit, idempotent request to run an exact chain revision."""

    idempotency_key: str = Field(min_length=8, max_length=128)
    chain_id: str = Field(
        min_length=1,
        max_length=512,
        pattern=r"^[^\x00-\x1f\x7f]+$",
    )
    task: str = Field(min_length=1, max_length=200_000)
    chain: ChainArtifactReference
    input: ExecutionInput

    @field_validator("idempotency_key")
    @classmethod
    def validate_idempotency_key(cls, value: str) -> str:
        if _IDEMPOTENCY_KEY_RE.fullmatch(value) is None:
            raise ValueError(
                "idempotency_key must use 8-128 letters, digits, dots, "
                "underscores, colons, or hyphens"
            )
        return value

    @field_validator("chain_id")
    @classmethod
    def validate_chain_id(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("chain_id must not be blank")
        return value

    @field_validator("task")
    @classmethod
    def validate_task(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("task must not be blank")
        if "\x00" in value:
            raise ValueError("task must not contain NUL characters")
        return value


class ExecutionOutputArtifact(HarnessExecutionModel):
    """Opaque reference to the retained, bounded execution result."""

    artifact_id: str = Field(min_length=1, max_length=256)
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    size_bytes: int = Field(ge=1)
    expires_at_unix: float
    session_id: str | None = Field(default=None, min_length=1, max_length=128)


class ChainExecutionReceipt(HarnessExecutionModel):
    """Completed CARL run, including its model-visible final answer."""

    status: Literal["completed"] = "completed"
    idempotency_key: str
    request_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    idempotency_scope: Literal["process_ttl_only"] = "process_ttl_only"
    replayed: bool = False
    chain_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    input_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    success: bool
    final_output: str | None = Field(default=None, max_length=200_000)
    step_count: int = Field(ge=0)
    output_artifact: ExecutionOutputArtifact


ExecutionFailureCode = Literal[
    "idempotency_conflict",
    "idempotency_ledger_full",
    "artifact_unavailable",
    "chain_binding_mismatch",
    "input_contract_unavailable",
    "input_contract_rejected",
    "preview_unavailable",
    "chain_admission_failed",
    "provider_configuration_failed",
    "runtime_initialization_failed",
    "execution_failed",
    "invalid_execution_result",
    "result_stage_failed",
]


class ChainExecutionFailure(HarnessExecutionModel):
    """Redacted typed terminal failure suitable for an MCP adapter."""

    code: ExecutionFailureCode
    failure_type: str | None = Field(default=None, min_length=1, max_length=160)


class ChainExecutionOperationError(RuntimeError):
    """An execution attempt failed without exposing private/provider text."""

    def __init__(self, failure: ChainExecutionFailure) -> None:
        self.failure = failure
        suffix = f" ({failure.failure_type})" if failure.failure_type else ""
        super().__init__(f"chain execution failed: {failure.code}{suffix}")


class ArtifactStore(Protocol):
    """Minimum private artifact-store surface required by this operation."""

    def resolve(
        self,
        artifact_id: str,
        *,
        content_sha256: str,
    ) -> dict[str, Any]: ...

    def stage(self, payload: Mapping[str, Any]) -> Any: ...


ApiBuilder = Callable[[Any], Any]
ContextBuilder = Callable[..., Any]
ChainValidator = Callable[..., PreflightResult]
ChainExecutor = Callable[[Any, Any], Awaitable[Any]]


@dataclass(frozen=True, slots=True)
class _Outcome:
    receipt: ChainExecutionReceipt | None = None
    failure: ChainExecutionFailure | None = None
    cancelled: bool = False


@dataclass(slots=True)
class _LedgerEntry:
    fingerprint: str
    loop: asyncio.AbstractEventLoop
    cancel_event: asyncio.Event
    task: asyncio.Task[_Outcome] | None = None
    outcome: _Outcome | None = None
    expires_at: float | None = None


class HarnessChainExecutionOperation:
    """Resolve, validate, execute, and retain one exact CARL chain run.

    This object is approval-agnostic.  A DeepSeek policy hook (or another
    trusted host) must gate the invocation before calling :meth:`execute`.
    Configured CARE tools may load from trusted local configuration, but no
    model-provided path crosses this API.
    """

    def __init__(
        self,
        *,
        config: CareConfig,
        artifacts: ArtifactStore,
        previews: AdaptationPreviewStore,
        idempotency_ttl_seconds: float = 900.0,
        max_idempotency_entries: int = 256,
        max_final_output_chars: int = 200_000,
        clock: Callable[[], float] = time.monotonic,
        api_builder: ApiBuilder = build_carl_llm_client,
        context_builder: ContextBuilder = build_run_context,
        chain_validator: ChainValidator = validate_chain,
        chain_executor: ChainExecutor = execute_chain_async,
    ) -> None:
        if not isinstance(config, CareConfig):
            raise TypeError("config must be a CareConfig")
        if not callable(getattr(artifacts, "resolve", None)) or not callable(
            getattr(artifacts, "stage", None)
        ):
            raise TypeError("artifacts must expose resolve(...) and stage(payload)")
        if not isinstance(previews, AdaptationPreviewStore):
            raise TypeError("previews must be an AdaptationPreviewStore")
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
        if (
            not isinstance(max_final_output_chars, int)
            or isinstance(max_final_output_chars, bool)
            or max_final_output_chars < 1
            or max_final_output_chars > 200_000
        ):
            raise ValueError("max_final_output_chars must be in 1..200000")
        for name, callback in (
            ("clock", clock),
            ("api_builder", api_builder),
            ("context_builder", context_builder),
            ("chain_validator", chain_validator),
            ("chain_executor", chain_executor),
        ):
            if not callable(callback):
                raise TypeError(f"{name} must be callable")

        self._config = config
        self._artifacts = artifacts
        self._previews = previews
        self._ttl_seconds = float(idempotency_ttl_seconds)
        self._max_entries = max_idempotency_entries
        self._max_final_output_chars = max_final_output_chars
        self._clock = clock
        self._api_builder = api_builder
        self._context_builder = context_builder
        self._chain_validator = chain_validator
        self._chain_executor = chain_executor
        self._ledger: dict[str, _LedgerEntry] = {}
        self._lock = threading.RLock()

    async def execute(
        self,
        request: ChainExecutionRequest,
        *,
        cancellation: CancellationToken | None = None,
    ) -> ChainExecutionReceipt:
        """Execute or replay one exact idempotent chain request."""

        if not isinstance(request, ChainExecutionRequest):
            raise TypeError("request must be a ChainExecutionRequest")
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
                    raise ChainExecutionOperationError(
                        ChainExecutionFailure(code="idempotency_conflict")
                    )
                if entry.loop is not loop and entry.outcome is None:
                    raise ChainExecutionOperationError(
                        ChainExecutionFailure(code="idempotency_conflict")
                    )
            else:
                if len(self._ledger) >= self._max_entries:
                    raise ChainExecutionOperationError(
                        ChainExecutionFailure(code="idempotency_ledger_full")
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
            # Transport cancellation is not authority to retry a side-effectful
            # provider run.  Host cancellation travels through the explicit
            # CancellationToken and the operation remains shielded otherwise.
            outcome = await asyncio.shield(task)
        return _project_outcome(outcome, replayed=replayed)

    async def _execute_entry(
        self,
        entry: _LedgerEntry,
        request: ChainExecutionRequest,
        fingerprint: str,
    ) -> _Outcome:
        try:
            receipt = await self._execute_once(
                request,
                fingerprint=fingerprint,
                cancel_event=entry.cancel_event,
            )
            outcome = _Outcome(receipt=receipt)
        except (CancelledByUserError, asyncio.CancelledError):
            outcome = _Outcome(cancelled=True)
        except ChainExecutionOperationError as exc:
            outcome = _Outcome(failure=exc.failure)
        except Exception as exc:  # noqa: BLE001 - last redacted boundary
            outcome = _Outcome(
                failure=ChainExecutionFailure(
                    code="execution_failed",
                    failure_type=_exception_type(exc),
                )
            )

        with self._lock:
            entry.outcome = outcome
            entry.expires_at = self._now() + self._ttl_seconds
        return outcome

    async def _execute_once(
        self,
        request: ChainExecutionRequest,
        *,
        fingerprint: str,
        cancel_event: asyncio.Event,
    ) -> ChainExecutionReceipt:
        self._raise_if_cancelled(cancel_event)
        chain_payload = self._resolve_artifact(
            request.chain.artifact_id,
            request.chain.content_sha256,
        )
        try:
            contract = chain_input_contract(request.chain_id, chain_payload)
        except (MissingInputContractError, InvalidInputContractError) as exc:
            raise ChainExecutionOperationError(
                ChainExecutionFailure(
                    code="input_contract_unavailable",
                    failure_type=_exception_type(exc),
                )
            ) from None
        if not hmac.compare_digest(
            contract.chain_digest,
            request.chain.content_sha256,
        ):
            raise ChainExecutionOperationError(
                ChainExecutionFailure(code="chain_binding_mismatch")
            )

        # First admission proves the detached chain artifact parses strictly
        # before provider configuration or configured tool loading begins.
        parsed = self._validate_chain(copy.deepcopy(chain_payload), context=None)
        self._raise_if_cancelled(cancel_event)

        input_payload, input_digest = self._resolve_input(request, contract)
        self._validate_input_contract(contract.schema, input_payload)
        self._raise_if_cancelled(cancel_event)

        try:
            api = self._api_builder(self._config.mage)
        except Exception as exc:  # noqa: BLE001 - config/provider boundary
            raise ChainExecutionOperationError(
                ChainExecutionFailure(
                    code="provider_configuration_failed",
                    failure_type=_exception_type(exc),
                )
            ) from None
        try:
            context = self._context_builder(
                query=request.task,
                api=api,
                config=self._config,
                files=input_payload,
            )
        except Exception as exc:  # noqa: BLE001 - CARL/tool setup boundary
            raise ChainExecutionOperationError(
                ChainExecutionFailure(
                    code="runtime_initialization_failed",
                    failure_type=_exception_type(exc),
                )
            ) from None

        self._raise_if_cancelled(cancel_event)
        admitted = self._validate_chain(
            copy.deepcopy(chain_payload),
            context=context,
        )
        # The context-aware parse is authoritative.  Requiring the same parsed
        # object identity would couple this boundary to CARL internals, but a
        # missing chain in either pass is never admitted.
        if parsed.chain is None or admitted.chain is None:
            raise ChainExecutionOperationError(
                ChainExecutionFailure(code="chain_admission_failed")
            )

        result = await self._run_with_cancellation(
            admitted.chain,
            context,
            cancel_event=cancel_event,
        )
        success, final_output, step_count = self._project_result(result)
        result_payload = {
            "kind": "maestro.chain_execution_result.v1",
            "success": success,
            "final_output": final_output,
            "step_count": step_count,
        }
        try:
            staged = self._artifacts.stage(result_payload)
            output_artifact = _project_artifact_receipt(staged)
        except Exception as exc:  # noqa: BLE001 - private store boundary
            raise ChainExecutionOperationError(
                ChainExecutionFailure(
                    code="result_stage_failed",
                    failure_type=_exception_type(exc),
                )
            ) from None

        return ChainExecutionReceipt(
            idempotency_key=request.idempotency_key,
            request_fingerprint=fingerprint,
            chain_digest=contract.chain_digest,
            input_digest=input_digest,
            success=success,
            final_output=final_output,
            step_count=step_count,
            output_artifact=output_artifact,
        )

    def _resolve_artifact(
        self,
        artifact_id: str,
        content_sha256: str,
    ) -> dict[str, Any]:
        try:
            value = self._artifacts.resolve(
                artifact_id,
                content_sha256=content_sha256,
            )
        except Exception as exc:  # noqa: BLE001 - store boundary
            raise ChainExecutionOperationError(
                ChainExecutionFailure(
                    code="artifact_unavailable",
                    failure_type=_exception_type(exc),
                )
            ) from None
        if not isinstance(value, dict):
            raise ChainExecutionOperationError(
                ChainExecutionFailure(code="artifact_unavailable")
            )
        return value

    def _resolve_input(
        self,
        request: ChainExecutionRequest,
        contract: Any,
    ) -> tuple[dict[str, Any], str]:
        selected = request.input
        if isinstance(selected, ExactInputArtifact):
            payload = self._resolve_artifact(
                selected.artifact_id,
                selected.content_sha256,
            )
            if not hmac.compare_digest(
                canonical_sha256(payload),
                selected.content_sha256,
            ):
                raise ChainExecutionOperationError(
                    ChainExecutionFailure(code="artifact_unavailable")
                )
            return payload, selected.content_sha256

        if not hmac.compare_digest(contract.chain_digest, selected.chain_digest) or not hmac.compare_digest(
            contract.schema_digest,
            selected.schema_digest,
        ):
            raise ChainExecutionOperationError(
                ChainExecutionFailure(code="preview_unavailable")
            )
        try:
            payload = self._previews.consume(
                selected.preview_id,
                chain_digest=selected.chain_digest,
                schema_digest=selected.schema_digest,
                source_digest=selected.source_digest,
                confirmation_digest=selected.confirmation_digest,
            )
        except (PreviewUnavailableError, HarnessContractError) as exc:
            raise ChainExecutionOperationError(
                ChainExecutionFailure(
                    code="preview_unavailable",
                    failure_type=_exception_type(exc),
                )
            ) from None
        if not isinstance(payload, dict) or not hmac.compare_digest(
            canonical_sha256(payload),
            selected.adapted_digest,
        ):
            raise ChainExecutionOperationError(
                ChainExecutionFailure(code="preview_unavailable")
            )
        return payload, selected.adapted_digest

    def _validate_input_contract(
        self,
        schema: Mapping[str, Any],
        payload: Mapping[str, Any],
    ) -> None:
        try:
            rejected = next(Draft202012Validator(schema).iter_errors(payload), None)
        except Exception as exc:  # noqa: BLE001 - schema runtime boundary
            raise ChainExecutionOperationError(
                ChainExecutionFailure(
                    code="input_contract_unavailable",
                    failure_type=_exception_type(exc),
                )
            ) from None
        if rejected is not None:
            # JSON-Schema diagnostics can quote private values.  The transport
            # only receives the stable refusal code.
            raise ChainExecutionOperationError(
                ChainExecutionFailure(code="input_contract_rejected")
            )

    def _validate_chain(
        self,
        payload: Mapping[str, Any],
        *,
        context: Any,
    ) -> PreflightResult:
        try:
            result = self._chain_validator(
                payload,
                context=context,
                strict_references=True,
            )
        except Exception as exc:  # noqa: BLE001 - CARL runtime boundary
            raise ChainExecutionOperationError(
                ChainExecutionFailure(
                    code="chain_admission_failed",
                    failure_type=_exception_type(exc),
                )
            ) from None
        if not isinstance(result, PreflightResult):
            raise ChainExecutionOperationError(
                ChainExecutionFailure(
                    code="chain_admission_failed",
                    failure_type="InvalidPreflightResult",
                )
            )
        if not result.parsed or result.parse_errors or result.chain is None:
            raise ChainExecutionOperationError(
                ChainExecutionFailure(code="chain_admission_failed")
            )
        if context is not None and (
            not result.preflight_available
            or result.missing_tools
            or result.missing_mcp_servers
            or result.missing_skills
        ):
            raise ChainExecutionOperationError(
                ChainExecutionFailure(code="chain_admission_failed")
            )
        return result

    async def _run_with_cancellation(
        self,
        chain: Any,
        context: Any,
        *,
        cancel_event: asyncio.Event,
    ) -> Any:
        execution_task = asyncio.create_task(self._chain_executor(chain, context))
        cancel_wait = asyncio.create_task(cancel_event.wait())
        try:
            done, _pending = await asyncio.wait(
                {execution_task, cancel_wait},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if cancel_wait in done and cancel_event.is_set():
                await _cancel_context(context)
                execution_task.cancel()
                try:
                    await execution_task
                except (asyncio.CancelledError, Exception):
                    pass
                raise CancelledByUserError("chain execution cancelled")

            cancel_wait.cancel()
            try:
                await cancel_wait
            except asyncio.CancelledError:
                pass
            try:
                return await execution_task
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - provider/runtime boundary
                raise ChainExecutionOperationError(
                    ChainExecutionFailure(
                        code="execution_failed",
                        failure_type=_exception_type(exc),
                    )
                ) from None
        finally:
            if not cancel_wait.done():
                cancel_wait.cancel()

    def _project_result(self, result: Any) -> tuple[bool, str | None, int]:
        if isinstance(result, Mapping):
            success = result.get("success")
            final_output = _mapping_final_output(result)
            step_results = result.get("step_results")
        else:
            success = getattr(result, "success", None)
            final_output = extract_final_output(result)
            step_results = getattr(result, "step_results", None)
        if not isinstance(success, bool):
            raise ChainExecutionOperationError(
                ChainExecutionFailure(code="invalid_execution_result")
            )
        if final_output is not None:
            if not isinstance(final_output, str):
                final_output = str(final_output)
            if len(final_output) > self._max_final_output_chars or "\x00" in final_output:
                raise ChainExecutionOperationError(
                    ChainExecutionFailure(code="invalid_execution_result")
                )
        if step_results is None:
            step_count = 0
        elif isinstance(step_results, (list, tuple)):
            step_count = len(step_results)
        else:
            raise ChainExecutionOperationError(
                ChainExecutionFailure(code="invalid_execution_result")
            )
        return success, final_output, step_count

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
                pass

        cancellation.on_cancel(signal)

    @staticmethod
    def _raise_if_cancelled(cancel_event: asyncio.Event) -> None:
        if cancel_event.is_set():
            raise CancelledByUserError("chain execution cancelled")

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


async def _cancel_context(context: Any) -> None:
    callback = getattr(context, "cancel", None)
    if not callable(callback):
        return
    try:
        outcome = callback()
        if inspect.isawaitable(outcome):
            await outcome
    except Exception:
        # Task cancellation still runs even if the optional cooperative hook
        # supplied by this CARL version fails.
        pass


def _mapping_final_output(result: Mapping[str, Any]) -> str | None:
    for key in ("final_output", "final_answer", "answer", "output"):
        value = result.get(key)
        if value is not None:
            return str(value)
    return None


def _request_fingerprint(request: ChainExecutionRequest) -> str:
    payload = request.model_dump(mode="json", exclude={"idempotency_key"})
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _project_artifact_receipt(value: Any) -> ExecutionOutputArtifact:
    def field(name: str) -> Any:
        if isinstance(value, Mapping):
            return value.get(name)
        return getattr(value, name, None)

    artifact_id = field("artifact_id")
    content_sha256 = field("content_sha256")
    size_bytes = field("size_bytes")
    expires_at_unix = field("expires_at_unix")
    session_id = field("session_id")
    if (
        not isinstance(artifact_id, str)
        or _OPAQUE_ID_RE.fullmatch(artifact_id) is None
        or not isinstance(content_sha256, str)
        or _SHA256_RE.fullmatch(content_sha256) is None
        or not isinstance(size_bytes, int)
        or isinstance(size_bytes, bool)
        or size_bytes < 1
        or not isinstance(expires_at_unix, (int, float))
        or isinstance(expires_at_unix, bool)
        or not math.isfinite(float(expires_at_unix))
        or (session_id is not None and not isinstance(session_id, str))
    ):
        raise ValueError("artifact store returned an invalid receipt")
    return ExecutionOutputArtifact(
        artifact_id=artifact_id,
        content_sha256=content_sha256,
        size_bytes=size_bytes,
        expires_at_unix=float(expires_at_unix),
        session_id=session_id,
    )


def _project_outcome(
    outcome: _Outcome,
    *,
    replayed: bool,
) -> ChainExecutionReceipt:
    if outcome.cancelled:
        raise CancelledByUserError("chain execution cancelled")
    if outcome.failure is not None:
        raise ChainExecutionOperationError(outcome.failure)
    if outcome.receipt is None:  # pragma: no cover - ledger invariant
        raise RuntimeError("execution outcome is empty")
    return outcome.receipt.model_copy(update={"replayed": replayed})


def _exception_type(exc: BaseException) -> str:
    candidate = type(exc).__name__
    if _SAFE_EXCEPTION_TYPE_RE.fullmatch(candidate) is None:
        return "RuntimeFailure"
    return candidate


__all__ = [
    "AdaptationPreviewInput",
    "ChainArtifactReference",
    "ChainExecutionFailure",
    "ChainExecutionOperationError",
    "ChainExecutionReceipt",
    "ChainExecutionRequest",
    "ExactInputArtifact",
    "ExecutionOutputArtifact",
    "HarnessChainExecutionOperation",
]
