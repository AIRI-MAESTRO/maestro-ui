"""UI-neutral Production transaction orchestration for the native app.

The Textual chat screen historically owns this workflow end-to-end.  This
module extracts the durable contract without importing Textual or constructing
provider clients:

``deployable generation -> typed admission -> Memory save -> baseline -> evolve``

Generation and execution are dependency-injected because both may call a paid
provider and tools.  The service itself only performs bounded validation and
the Memory writes explicitly described below.  It is intentionally a saga,
not an ACID transaction: once a chain or baseline card has been saved, a later
failure/cancellation is reported as a partial result and is never disguised as
a rollback.

Privacy boundary:

* ``private_task`` and local attachment paths may reach generation/execution;
* only ``safe_task`` and basename/hash/size/MIME attachment identities reach
  Memory;
* exception text and callback return objects never enter events or ``repr``;
* generated metadata keys that look credential-bearing are omitted.

Integration adapters should bind ``generate_deployable`` to
``care.generation.build_mage_generator(..., deployable=True)`` plus
``run_generation`` and bind ``run_baseline`` to the CARL execution path.  The
typed request passed to the generator carries a literal ``deployable=True`` so
an adapter cannot accidentally infer Interactive semantics from UI state.
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import inspect
import json
import re
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal

from care.config import StagePolicy
from care.runtime.cancellation import CancellationToken, CancelledByUserError


MAX_TASK_CHARS = 512 * 1024
MAX_CONTEXT_FILES = 64
MAX_PATH_CHARS = 4_096
MAX_CHAIN_BYTES = 2 * 1024 * 1024
MAX_METADATA_BYTES = 256 * 1024
MAX_ENTITY_ID_CHARS = 512
MAX_EVENT_MESSAGE_CHARS = 240

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
_FORCE_RE = re.compile(r"^\s*\[FORCE\]\s*", re.IGNORECASE)
_SECRET_KEY_RE = re.compile(
    r"(?:api[_-]?key|authorization|bearer|cookie|credential|password|secret|token)",
    re.IGNORECASE,
)


class ProductionStage(StrEnum):
    """Observable stages in chronological execution order."""

    GENERATE = "generate"
    PREVIEW = "preview"
    VALIDATE = "validate"
    SAVE = "save"
    RUN = "run"
    BASELINE = "baseline"
    EVOLVE = "evolve"


class ProductionStageState(StrEnum):
    """Both progress-event and terminal stage states."""

    WAITING_CONFIRMATION = "waiting_confirmation"
    RUNNING = "running"
    DONE = "done"
    SKIPPED = "skipped"
    FAILED = "failed"
    CANCELLED = "cancelled"


TERMINAL_STAGE_STATES = frozenset(
    {
        ProductionStageState.DONE,
        ProductionStageState.SKIPPED,
        ProductionStageState.FAILED,
        ProductionStageState.CANCELLED,
    }
)


class ProductionTransactionState(StrEnum):
    """Overall result, independent of how many effects already committed."""

    COMPLETED = "completed"
    PARTIAL = "partial"
    SKIPPED = "skipped"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ProductionCode(StrEnum):
    """Stable, non-secret reason codes for UI rendering and telemetry."""

    OK = "ok"
    POLICY_SKIP = "policy_skip"
    DECLINED = "declined"
    DEPENDENCY = "dependency_unmet"
    CANCELLED = "cancelled"
    INVALID_REQUEST = "invalid_request"
    GENERATION_FAILED = "generation_failed"
    INVALID_GENERATION = "invalid_generation"
    VALIDATION_FAILED = "typed_validation_failed"
    MEMORY_UNAVAILABLE = "memory_unavailable"
    DEDUP_LOOKUP_FAILED = "dedup_lookup_failed"
    DUPLICATE = "duplicate"
    SAVE_FAILED = "save_failed"
    RUN_FAILED = "baseline_run_failed"
    RECORD_FAILED = "baseline_record_failed"
    PLATFORM_UNAVAILABLE = "platform_unavailable"
    EVOLUTION_FAILED = "evolution_failed"
    CONFIRMATION_UNAVAILABLE = "confirmation_unavailable"
    CONFIRMATION_FAILED = "confirmation_failed"
    CONFIRMATION_STATE_CHANGED = "confirmation_state_changed"


class ProductionInputError(ValueError):
    """The caller supplied an invalid or unbounded transaction request."""


@dataclass(frozen=True)
class ProductionRequest:
    """One first-turn Production request.

    ``context_files`` are private local generation inputs.  The service creates
    a basename-only projection before computing the dedup key or calling
    Memory.  The object deliberately hides all user-authored fields from
    ``repr`` because callers commonly include it in debug structures.
    """

    private_task: str = field(repr=False)
    safe_task: str = field(repr=False)
    context_files: tuple[Mapping[str, Any], ...] = field(
        default_factory=tuple,
        repr=False,
    )
    user_context: str = field(default="", repr=False)
    recalled_context: str = field(default="", repr=False)
    capabilities: Any = field(default=None, repr=False, compare=False)


@dataclass(frozen=True)
class ProductionGenerationInput:
    """Exact input contract for an injected MAGE adapter."""

    private_task: str = field(repr=False)
    safe_task: str = field(repr=False)
    context_files: tuple[dict[str, Any], ...] = field(repr=False)
    user_context: str = field(default="", repr=False)
    recalled_context: str = field(default="", repr=False)
    capabilities: Any = field(default=None, repr=False, compare=False)
    deployable: Literal[True] = True


@dataclass(frozen=True)
class ProductionBaselineInput:
    """Private baseline execution input for an injected CARL adapter."""

    chain_id: str
    display_name: str
    private_task: str = field(repr=False)
    chain_dict: dict[str, Any] = field(repr=False, compare=False)
    typed_chain: Any = field(repr=False, compare=False)


@dataclass(frozen=True)
class ProductionBaselineRecordInput:
    """Safe persistence input for the baseline recorder."""

    chain_id: str
    display_name: str
    safe_task: str = field(repr=False)
    run_result: Any = field(repr=False, compare=False)


@dataclass(frozen=True)
class ProductionEvolutionInput:
    """Snapshot handed to an injected Platform facade adapter."""

    chain_id: str
    display_name: str
    base_chain_content: dict[str, Any] = field(repr=False, compare=False)
    tags: tuple[str, ...]


@dataclass(frozen=True)
class BaselineReceipt:
    card_id: str
    status: str
    agent_recorded: bool = True


@dataclass(frozen=True)
class EvolutionReceipt:
    evolution_id: str
    status: str = "queued"


@dataclass(frozen=True)
class ExistingChain:
    entity_id: str
    display_name: str = ""


@dataclass(frozen=True)
class ProductionRevisionPointer:
    """Follow-up target matching the TUI's entity + ``latest`` semantics.

    Initial ``CareMemory.save_chain`` returns only the entity id, so this is not
    an exact version pin.  A revision adapter must reload ``channel='latest'``
    immediately before editing and save back into the same ``entity_id``.
    """

    entity_id: str
    channel: Literal["latest"] = "latest"
    version_id: None = None


@dataclass(frozen=True)
class ProductionStageEvent:
    sequence: int
    stage: ProductionStage
    state: ProductionStageState
    code: ProductionCode
    message: str


@dataclass(frozen=True)
class ProductionStageResult:
    stage: ProductionStage
    state: ProductionStageState
    code: ProductionCode
    message: str


@dataclass(frozen=True)
class ProductionConfirmation:
    """Secret-free, state-bound confirmation prompt."""

    stage: ProductionStage
    binding_sha256: str
    state_sha256: str
    reason: str
    effects: tuple[str, ...]
    preview: Mapping[str, str | int | bool]


@dataclass(frozen=True)
class ProductionPolicies:
    """Production policy surface.

    ``run=ASK`` intentionally honours the declared TUI Production preset.  The
    old screen accidentally ignored it.  Use ``run=StagePolicy.AUTO`` only when
    the enclosing UI has already presented an equivalent, state-bound consent.
    """

    generate: StagePolicy = StagePolicy.AUTO
    save: StagePolicy = StagePolicy.AUTO
    run: StagePolicy = StagePolicy.ASK
    baseline: StagePolicy = StagePolicy.AUTO
    evolve: StagePolicy = StagePolicy.AUTO


@dataclass(frozen=True)
class ProductionTransactionResult:
    state: ProductionTransactionState
    stages: tuple[ProductionStageResult, ...]
    events: tuple[ProductionStageEvent, ...]
    chain_id: str | None = None
    display_name: str | None = None
    baseline: BaselineReceipt | None = None
    evolution: EvolutionReceipt | None = None
    revision_pointer: ProductionRevisionPointer | None = None
    duplicate: ExistingChain | None = None

    @property
    def committed(self) -> bool:
        """Whether at least the durable chain save completed."""

        return self.chain_id is not None

    def stage(self, stage: ProductionStage) -> ProductionStageResult:
        for result in self.stages:
            if result.stage is stage:
                return result
        raise KeyError(stage)


GenerateDeployable = Callable[
    [ProductionGenerationInput, CancellationToken],
    Any | Awaitable[Any],
]
RunBaseline = Callable[
    [ProductionBaselineInput, CancellationToken],
    Any | Awaitable[Any],
]
ConfirmStage = Callable[[ProductionConfirmation], bool | Awaitable[bool]]
StateSha256 = Callable[[ProductionStage], str | Awaitable[str]]
EventSink = Callable[[ProductionStageEvent], None]
ValidateChain = Callable[[dict[str, Any]], Any]
RecordBaseline = Callable[[Any, ProductionBaselineRecordInput], Any]
StartEvolution = Callable[[ProductionEvolutionInput], Any | Awaitable[Any]]


@dataclass
class _RunState:
    events: list[ProductionStageEvent] = field(default_factory=list)
    terminal: dict[ProductionStage, ProductionStageResult] = field(
        default_factory=dict,
    )
    chain_id: str | None = None
    display_name: str | None = None
    baseline: BaselineReceipt | None = None
    evolution: EvolutionReceipt | None = None
    revision_pointer: ProductionRevisionPointer | None = None
    duplicate: ExistingChain | None = None


class _Journal:
    def __init__(self, state: _RunState, sink: EventSink | None) -> None:
        self._state = state
        self._sink = sink

    def emit(
        self,
        stage: ProductionStage,
        state: ProductionStageState,
        code: ProductionCode,
        message: str,
    ) -> None:
        event = ProductionStageEvent(
            sequence=len(self._state.events) + 1,
            stage=stage,
            state=state,
            code=code,
            message=_bounded_message(message),
        )
        self._state.events.append(event)
        if state in TERMINAL_STAGE_STATES:
            self._state.terminal[stage] = ProductionStageResult(
                stage=stage,
                state=state,
                code=code,
                message=event.message,
            )
        if self._sink is not None:
            try:
                self._sink(event)
            except Exception:  # noqa: BLE001 - telemetry cannot alter effects
                pass

    def running(self, stage: ProductionStage, message: str) -> None:
        self.emit(stage, ProductionStageState.RUNNING, ProductionCode.OK, message)

    def done(self, stage: ProductionStage, message: str) -> None:
        self.emit(stage, ProductionStageState.DONE, ProductionCode.OK, message)

    def skipped(
        self,
        stage: ProductionStage,
        code: ProductionCode,
        message: str,
    ) -> None:
        self.emit(stage, ProductionStageState.SKIPPED, code, message)

    def failed(
        self,
        stage: ProductionStage,
        code: ProductionCode,
        message: str,
    ) -> None:
        self.emit(stage, ProductionStageState.FAILED, code, message)

    def cancelled(self, stage: ProductionStage) -> None:
        self.emit(
            stage,
            ProductionStageState.CANCELLED,
            ProductionCode.CANCELLED,
            "Cancelled before the stage completed.",
        )


class DesktopProductionService:
    """Run one bounded Production saga with injected provider/tool seams."""

    def __init__(
        self,
        *,
        memory: Any,
        generate_deployable: GenerateDeployable,
        run_baseline: RunBaseline,
        validate_chain: ValidateChain | None = None,
        record_baseline: RecordBaseline | None = None,
        start_evolution: StartEvolution | None = None,
        policies: ProductionPolicies | None = None,
        confirm: ConfirmStage | None = None,
        state_sha256: StateSha256 | None = None,
        on_event: EventSink | None = None,
    ) -> None:
        if memory is None:
            raise ValueError("memory is required for Production")
        if not callable(generate_deployable):
            raise TypeError("generate_deployable must be callable")
        if not callable(run_baseline):
            raise TypeError("run_baseline must be callable")
        if validate_chain is not None and not callable(validate_chain):
            raise TypeError("validate_chain must be callable")
        if record_baseline is not None and not callable(record_baseline):
            raise TypeError("record_baseline must be callable")
        if start_evolution is not None and not callable(start_evolution):
            raise TypeError("start_evolution must be callable")
        self._memory = memory
        self._generate = generate_deployable
        self._run_baseline = run_baseline
        self._validate = validate_chain or _default_validate_chain
        self._record_baseline = record_baseline or _default_record_baseline
        self._start_evolution = start_evolution
        self._policies = policies or ProductionPolicies()
        self._confirm = confirm
        self._state_sha256 = state_sha256
        self._on_event = on_event
        _validate_policies(self._policies)

    async def execute(
        self,
        request: ProductionRequest,
        *,
        cancellation: CancellationToken | None = None,
    ) -> ProductionTransactionResult:
        """Execute one Production saga and return all durable receipts.

        Token-driven cancellation is converted into a typed result.  Ambient
        ``asyncio`` task cancellation is emitted and re-raised, preserving the
        caller's structured-concurrency contract.
        """

        normalized = _normalize_request(request)
        token = cancellation or CancellationToken()
        run = _RunState()
        journal = _Journal(run, self._on_event)
        request_binding = _request_binding(normalized)
        chain_json: str | None = None
        typed_chain: Any = None
        metadata: dict[str, Any] = {}
        clean_safe_task = _strip_force(normalized.safe_task)[1]
        persistence_files = _persistence_context_files(normalized.context_files)
        task_hash = _task_hash(clean_safe_task, persistence_files)

        # GENERATE ---------------------------------------------------------
        if not await self._authorize(
            journal,
            ProductionStage.GENERATE,
            self._policies.generate,
            request_binding,
            reason="Generate one deployable agent chain.",
            effects=("paid provider generation may run",),
            preview={
                "deployable": True,
                "task_sha256": _sha256_text(normalized.private_task),
                "attachments": len(normalized.context_files),
            },
        ):
            return self._finish(run)
        if self._cancel_before(journal, token, ProductionStage.GENERATE):
            return self._finish(run)
        journal.running(ProductionStage.GENERATE, "Generating deployable chain.")
        generation_input = ProductionGenerationInput(
            private_task=normalized.private_task,
            safe_task=normalized.safe_task,
            context_files=normalized.context_files,
            user_context=normalized.user_context,
            recalled_context=normalized.recalled_context,
            capabilities=normalized.capabilities,
        )
        try:
            generated = await _maybe_await(self._generate(generation_input, token))
            token.raise_if_cancelled()
            chain_dict, metadata = _normalize_generation(generated)
            chain_json = _canonical_chain_json(chain_dict)
        except CancelledByUserError:
            journal.cancelled(ProductionStage.GENERATE)
            return self._finish(run)
        except asyncio.CancelledError:
            journal.cancelled(ProductionStage.GENERATE)
            raise
        except ProductionInputError:
            journal.failed(
                ProductionStage.GENERATE,
                ProductionCode.INVALID_GENERATION,
                "Generator returned an invalid or oversized chain.",
            )
            return self._finish(run)
        except Exception:  # noqa: BLE001 - never expose provider exception text
            journal.failed(
                ProductionStage.GENERATE,
                ProductionCode.GENERATION_FAILED,
                "Deployable chain generation failed.",
            )
            return self._finish(run)
        journal.done(ProductionStage.GENERATE, "Deployable chain generated.")
        journal.done(ProductionStage.PREVIEW, "Generated chain is available to inspect.")

        # VALIDATE ---------------------------------------------------------
        journal.running(ProductionStage.VALIDATE, "Running typed CARL admission.")
        try:
            token.raise_if_cancelled()
            assert chain_json is not None
            typed_chain = self._validate(json.loads(chain_json))
            if typed_chain is None:
                raise ValueError("validator returned no typed chain")
            token.raise_if_cancelled()
        except CancelledByUserError:
            journal.cancelled(ProductionStage.VALIDATE)
            return self._finish(run)
        except asyncio.CancelledError:
            journal.cancelled(ProductionStage.VALIDATE)
            raise
        except Exception:  # noqa: BLE001 - CARL may include prompt text in error
            journal.failed(
                ProductionStage.VALIDATE,
                ProductionCode.VALIDATION_FAILED,
                "Chain failed typed CARL admission; Memory was not accessed.",
            )
            return self._finish(run)
        journal.done(ProductionStage.VALIDATE, "Typed CARL admission passed.")

        # SAVE -------------------------------------------------------------
        force, _ = _strip_force(normalized.safe_task)
        try:
            duplicate = None if force else self._find_duplicate(task_hash)
        except Exception:  # noqa: BLE001 - no fail-open writes
            journal.failed(
                ProductionStage.SAVE,
                ProductionCode.DEDUP_LOOKUP_FAILED,
                "Could not verify deduplication state; chain was not saved.",
            )
            return self._finish(run)
        if duplicate is not None:
            run.duplicate = duplicate
            journal.skipped(
                ProductionStage.SAVE,
                ProductionCode.DUPLICATE,
                "An agent for this exact safe task/input already exists.",
            )
            return self._finish(run)

        display_name = _display_name(clean_safe_task)
        chain_sha256 = _sha256_text(chain_json)
        if not await self._authorize(
            journal,
            ProductionStage.SAVE,
            self._policies.save,
            request_binding + chain_sha256 + task_hash,
            reason="Persist the typed agent in Memory.",
            effects=("create a durable Memory chain",),
            preview={
                "chain_sha256": chain_sha256,
                "task_hash": task_hash,
                "attachments": len(persistence_files),
                "display_name": display_name,
            },
        ):
            return self._finish(run)
        if not force:
            try:
                duplicate = self._find_duplicate(task_hash)
            except Exception:  # noqa: BLE001 - confirmation-to-write recheck
                journal.failed(
                    ProductionStage.SAVE,
                    ProductionCode.DEDUP_LOOKUP_FAILED,
                    "Deduplication recheck failed; chain was not saved.",
                )
                return self._finish(run)
            if duplicate is not None:
                run.duplicate = duplicate
                journal.skipped(
                    ProductionStage.SAVE,
                    ProductionCode.DUPLICATE,
                    "A matching agent appeared before the save could commit.",
                )
                return self._finish(run)
        if self._cancel_before(journal, token, ProductionStage.SAVE):
            return self._finish(run)
        journal.running(ProductionStage.SAVE, "Saving typed chain to Memory.")
        tags = _production_tags(task_hash, metadata)
        domain = metadata.get("domain")
        try:
            chain_id = self._memory.save_chain(
                typed_chain,
                name=display_name,
                query=clean_safe_task,
                domain=domain if isinstance(domain, str) else None,
                context_files=[dict(item) for item in persistence_files],
                mage_metadata=copy.deepcopy(metadata) or None,
                tags=list(tags),
            )
            chain_id = _validated_id(chain_id, "chain id")
        except Exception:  # noqa: BLE001 - Memory errors may echo request data
            journal.failed(
                ProductionStage.SAVE,
                ProductionCode.SAVE_FAILED,
                "Memory rejected or failed to persist the typed chain.",
            )
            return self._finish(run)
        run.chain_id = chain_id
        run.display_name = display_name
        run.revision_pointer = ProductionRevisionPointer(entity_id=chain_id)
        journal.done(ProductionStage.SAVE, "Typed chain saved to Memory.")

        # A synchronous Memory call cannot be interrupted.  If cancellation
        # arrived during it, preserve the receipt/pointer and stop here.
        if self._cancel_before(journal, token, ProductionStage.RUN):
            return self._finish(run)

        # RUN --------------------------------------------------------------
        if not await self._authorize(
            journal,
            ProductionStage.RUN,
            self._policies.run,
            request_binding + chain_sha256 + chain_id,
            reason="Execute the saved agent once to establish a baseline.",
            effects=("provider and declared agent tools may run",),
            preview={
                "chain_id": chain_id,
                "chain_sha256": chain_sha256,
                "task_sha256": _sha256_text(normalized.private_task),
            },
        ):
            return self._finish(run)
        if self._cancel_before(journal, token, ProductionStage.RUN):
            return self._finish(run)
        journal.running(ProductionStage.RUN, "Executing private baseline task.")
        try:
            # Re-parse a fresh snapshot so an execution adapter cannot mutate
            # the object already saved or the later Platform seed.
            baseline_dict = json.loads(chain_json)
            baseline_typed = self._validate(json.loads(chain_json))
            if baseline_typed is None:
                raise ValueError("validator returned no typed chain")
            baseline_input = ProductionBaselineInput(
                chain_id=chain_id,
                display_name=display_name,
                private_task=normalized.private_task,
                chain_dict=baseline_dict,
                typed_chain=baseline_typed,
            )
            result = await _maybe_await(self._run_baseline(baseline_input, token))
            token.raise_if_cancelled()
            if not _run_succeeded(result):
                raise ValueError("baseline did not report success")
        except CancelledByUserError:
            journal.cancelled(ProductionStage.RUN)
            return self._finish(run)
        except asyncio.CancelledError:
            journal.cancelled(ProductionStage.RUN)
            raise
        except Exception:  # noqa: BLE001 - execution errors may contain secrets
            journal.failed(
                ProductionStage.RUN,
                ProductionCode.RUN_FAILED,
                "Baseline execution failed; no baseline card was recorded.",
            )
            return self._finish(run)
        journal.done(ProductionStage.RUN, "Baseline execution succeeded.")

        # BASELINE ---------------------------------------------------------
        if not await self._authorize(
            journal,
            ProductionStage.BASELINE,
            self._policies.baseline,
            request_binding + chain_sha256 + chain_id,
            reason="Persist the successful run as the first dataset entry.",
            effects=("create a durable Memory baseline card",),
            preview={"chain_id": chain_id, "task_hash": task_hash},
        ):
            return self._finish(run)
        if self._cancel_before(journal, token, ProductionStage.BASELINE):
            return self._finish(run)
        journal.running(ProductionStage.BASELINE, "Recording baseline in Memory.")
        try:
            raw_receipt = self._record_baseline(
                self._memory,
                ProductionBaselineRecordInput(
                    chain_id=chain_id,
                    display_name=display_name,
                    safe_task=clean_safe_task,
                    run_result=result,
                ),
            )
            if inspect.isawaitable(raw_receipt):
                raw_receipt = await raw_receipt
            receipt = _normalize_baseline_receipt(raw_receipt)
        except CancelledByUserError:
            journal.cancelled(ProductionStage.BASELINE)
            return self._finish(run)
        except asyncio.CancelledError:
            journal.cancelled(ProductionStage.BASELINE)
            raise
        except Exception:  # noqa: BLE001 - recorder errors may echo card data
            journal.failed(
                ProductionStage.BASELINE,
                ProductionCode.RECORD_FAILED,
                "Baseline ran but its Memory card could not be recorded.",
            )
            return self._finish(run)
        run.baseline = receipt
        journal.done(ProductionStage.BASELINE, "Baseline card recorded in Memory.")

        if self._cancel_before(journal, token, ProductionStage.EVOLVE):
            return self._finish(run)

        # EVOLVE -----------------------------------------------------------
        if self._start_evolution is None:
            journal.skipped(
                ProductionStage.EVOLVE,
                ProductionCode.PLATFORM_UNAVAILABLE,
                "Platform is not configured; optional evolution was skipped.",
            )
            return self._finish(run)
        if not await self._authorize(
            journal,
            ProductionStage.EVOLVE,
            self._policies.evolve,
            request_binding + chain_sha256 + chain_id + receipt.card_id,
            reason="Start optional Platform evolution from the saved snapshot.",
            effects=("submit a Platform evolution run",),
            preview={
                "chain_id": chain_id,
                "baseline_card_id": receipt.card_id,
                "chain_sha256": chain_sha256,
            },
        ):
            return self._finish(run)
        if self._cancel_before(journal, token, ProductionStage.EVOLVE):
            return self._finish(run)
        journal.running(ProductionStage.EVOLVE, "Submitting Platform evolution.")
        try:
            raw_evolution = await _maybe_await(
                self._start_evolution(
                    ProductionEvolutionInput(
                        chain_id=chain_id,
                        display_name=display_name,
                        base_chain_content=json.loads(chain_json),
                        tags=("source:chat-prod", f"chain:{chain_id}"),
                    )
                )
            )
            evolution = _normalize_evolution_receipt(raw_evolution)
        except CancelledByUserError:
            journal.cancelled(ProductionStage.EVOLVE)
            return self._finish(run)
        except asyncio.CancelledError:
            journal.cancelled(ProductionStage.EVOLVE)
            raise
        except Exception:  # noqa: BLE001 - Platform may echo submitted content
            journal.failed(
                ProductionStage.EVOLVE,
                ProductionCode.EVOLUTION_FAILED,
                "Platform evolution submission failed; prior writes remain.",
            )
            return self._finish(run)
        # A submit receipt means the remote effect committed.  A cancel signal
        # that arrived during a non-cooperative submit must not erase it.
        run.evolution = evolution
        journal.done(ProductionStage.EVOLVE, "Platform evolution submitted.")
        return self._finish(run)

    async def _authorize(
        self,
        journal: _Journal,
        stage: ProductionStage,
        policy: StagePolicy,
        binding_material: str,
        *,
        reason: str,
        effects: tuple[str, ...],
        preview: Mapping[str, str | int | bool],
    ) -> bool:
        if policy is StagePolicy.SKIP:
            journal.skipped(
                stage,
                ProductionCode.POLICY_SKIP,
                "Stage disabled by Production policy.",
            )
            return False
        if policy is StagePolicy.AUTO:
            return True
        if self._confirm is None or self._state_sha256 is None:
            journal.failed(
                stage,
                ProductionCode.CONFIRMATION_UNAVAILABLE,
                "Stage requires a state-bound confirmer.",
            )
            return False
        try:
            before = await _maybe_await(self._state_sha256(stage))
            before = _validated_sha256(before, "confirmation state")
            binding = _sha256_text(
                "\x1f".join((stage.value, binding_material, before))
            )
            confirmation = ProductionConfirmation(
                stage=stage,
                binding_sha256=binding,
                state_sha256=before,
                reason=reason,
                effects=effects,
                preview=dict(preview),
            )
            journal.emit(
                stage,
                ProductionStageState.WAITING_CONFIRMATION,
                ProductionCode.OK,
                "Waiting for confirmation.",
            )
            accepted = await _maybe_await(self._confirm(confirmation))
            if not isinstance(accepted, bool):
                raise TypeError("confirmer must return bool")
            if not accepted:
                journal.skipped(
                    stage,
                    ProductionCode.DECLINED,
                    "Stage declined by the user.",
                )
                return False
            after = await _maybe_await(self._state_sha256(stage))
            after = _validated_sha256(after, "confirmation state")
        except CancelledByUserError:
            journal.cancelled(stage)
            return False
        except asyncio.CancelledError:
            journal.cancelled(stage)
            raise
        except Exception:  # noqa: BLE001 - confirmation errors may carry state
            journal.failed(
                stage,
                ProductionCode.CONFIRMATION_FAILED,
                "State-bound confirmation could not be completed.",
            )
            return False
        if after != before:
            journal.failed(
                stage,
                ProductionCode.CONFIRMATION_STATE_CHANGED,
                "Provider/tool state changed after confirmation; stage was blocked.",
            )
            return False
        return True

    def _find_duplicate(self, task_hash: str) -> ExistingChain | None:
        list_entities = getattr(self._memory, "list_entities", None)
        if not callable(list_entities):
            raise RuntimeError("Memory has no list_entities")
        rows = list_entities(
            entity_type="chain",
            tags=[f"task-hash:{task_hash}"],
            limit=1,
        )
        if rows is None:
            raise RuntimeError("Memory returned no dedup result")
        if not isinstance(rows, Sequence) or isinstance(rows, (str, bytes)):
            raise RuntimeError("Memory returned malformed dedup result")
        if not rows:
            return None
        row = rows[0]
        entity_id = _row_value(row, "entity_id", "id", "chain_id")
        if not entity_id:
            raise RuntimeError("duplicate row has no entity id")
        name = _row_value(row, "name", "display_name") or ""
        return ExistingChain(
            entity_id=_validated_id(entity_id, "duplicate chain id"),
            display_name=_bounded_text(name, 256, allow_empty=True),
        )

    @staticmethod
    def _cancel_before(
        journal: _Journal,
        token: CancellationToken,
        stage: ProductionStage,
    ) -> bool:
        if not token.is_cancelled:
            return False
        journal.cancelled(stage)
        return True

    def _finish(self, run: _RunState) -> ProductionTransactionResult:
        _complete_unreached_stages(run, self._on_event)
        terminal = tuple(
            run.terminal[stage]
            for stage in ProductionStage
        )
        states = {item.state for item in terminal}
        if ProductionStageState.CANCELLED in states:
            overall = ProductionTransactionState.CANCELLED
        elif run.chain_id is not None:
            essential_done = (
                run.terminal[ProductionStage.RUN].state
                is ProductionStageState.DONE
                and run.terminal[ProductionStage.BASELINE].state
                is ProductionStageState.DONE
            )
            evolution_state = run.terminal[ProductionStage.EVOLVE].state
            if essential_done and evolution_state in {
                ProductionStageState.DONE,
                ProductionStageState.SKIPPED,
            }:
                overall = ProductionTransactionState.COMPLETED
            else:
                overall = ProductionTransactionState.PARTIAL
        elif ProductionStageState.FAILED in states:
            overall = ProductionTransactionState.FAILED
        else:
            overall = ProductionTransactionState.SKIPPED
        return ProductionTransactionResult(
            state=overall,
            stages=terminal,
            events=tuple(run.events),
            chain_id=run.chain_id,
            display_name=run.display_name,
            baseline=run.baseline,
            evolution=run.evolution,
            revision_pointer=run.revision_pointer,
            duplicate=run.duplicate,
        )


def _default_validate_chain(chain_dict: dict[str, Any]) -> Any:
    try:
        from mmar_carl import ReasoningChain
    except ImportError as exc:
        raise RuntimeError(
            "CARL runtime is required for Production typed admission"
        ) from exc
    return ReasoningChain.from_dict(
        copy.deepcopy(chain_dict),
        use_typed_steps=True,
    )


def _default_record_baseline(
    memory: Any,
    request: ProductionBaselineRecordInput,
) -> Any:
    from care.runtime.run_recorder import record_run_completion

    return record_run_completion(
        memory,
        agent_entity_id=request.chain_id,
        agent_name=request.display_name,
        result=request.run_result,
        query=request.safe_task,
        extra_tags=[f"dataset-entry:{request.chain_id}"],
    )


async def _maybe_await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


def _normalize_request(request: ProductionRequest) -> ProductionRequest:
    if not isinstance(request, ProductionRequest):
        raise ProductionInputError("request must be ProductionRequest")
    private_task = _task_text(request.private_task, "private_task")
    safe_task = _task_text(request.safe_task, "safe_task")
    user_context = _optional_text(request.user_context, "user_context")
    recalled_context = _optional_text(request.recalled_context, "recalled_context")
    if not isinstance(request.context_files, tuple):
        raise ProductionInputError("context_files must be a tuple")
    if len(request.context_files) > MAX_CONTEXT_FILES:
        raise ProductionInputError("too many context files")
    refs = tuple(_normalize_context_file(item) for item in request.context_files)
    return ProductionRequest(
        private_task=private_task,
        safe_task=safe_task,
        context_files=refs,
        user_context=user_context,
        recalled_context=recalled_context,
        capabilities=request.capabilities,
    )


def _normalize_context_file(item: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(item, Mapping):
        raise ProductionInputError("context file must be a mapping")
    raw_path = item.get("path")
    if not isinstance(raw_path, str) or not raw_path or len(raw_path) > MAX_PATH_CHARS:
        raise ProductionInputError("context file path is invalid")
    if _CONTROL_RE.search(raw_path):
        raise ProductionInputError("context file path contains control characters")
    digest = _validated_sha256(item.get("sha256"), "context file sha256")
    size = item.get("size_bytes")
    if not isinstance(size, int) or isinstance(size, bool) or size < 0:
        raise ProductionInputError("context file size_bytes is invalid")
    result: dict[str, Any] = {
        "path": raw_path,
        "sha256": digest,
        "size_bytes": size,
    }
    mime = item.get("mime_type") or item.get("media_type")
    if mime is not None:
        if not isinstance(mime, str) or not mime or len(mime) > 255:
            raise ProductionInputError("context file MIME type is invalid")
        if _CONTROL_RE.search(mime):
            raise ProductionInputError("context file MIME contains controls")
        result["mime_type"] = mime
    return result


def _persistence_context_files(
    refs: tuple[dict[str, Any], ...],
) -> tuple[dict[str, Any], ...]:
    projected: list[dict[str, Any]] = []
    for item in refs:
        name = _path_basename(item["path"])
        row: dict[str, Any] = {
            "path": name,
            "sha256": item["sha256"],
            "size_bytes": item["size_bytes"],
        }
        if "mime_type" in item:
            row["mime_type"] = item["mime_type"]
        projected.append(row)
    return tuple(projected)


def _normalize_generation(result: Any) -> tuple[dict[str, Any], dict[str, Any]]:
    if isinstance(result, Mapping):
        chain = result.get("chain_dict")
        metadata = result.get("metadata")
    else:
        chain = getattr(result, "chain_dict", None)
        metadata = getattr(result, "metadata", None)
    if not isinstance(chain, Mapping) or not chain:
        raise ProductionInputError("generation returned no chain_dict")
    chain_dict = dict(chain)
    return chain_dict, _safe_metadata(metadata)


def _safe_metadata(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        dump = getattr(value, "model_dump", None)
        if not callable(dump):
            return {}
        try:
            value = dump()
        except Exception:  # noqa: BLE001 - metadata is optional
            return {}
    if not isinstance(value, Mapping):
        return {}
    projected = _strip_secret_metadata(dict(value))
    try:
        encoded = json.dumps(
            projected,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, RecursionError):
        return {}
    if len(encoded) > MAX_METADATA_BYTES:
        return {}
    return json.loads(encoded)


def _strip_secret_metadata(value: Any, *, depth: int = 0) -> Any:
    if depth > 12:
        return None
    if isinstance(value, Mapping):
        clean: dict[str, Any] = {}
        for raw_key, item in value.items():
            if not isinstance(raw_key, str) or _SECRET_KEY_RE.search(raw_key):
                continue
            clean[raw_key] = _strip_secret_metadata(item, depth=depth + 1)
        return clean
    if isinstance(value, (list, tuple)):
        return [_strip_secret_metadata(item, depth=depth + 1) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return None


def _canonical_chain_json(chain: Mapping[str, Any]) -> str:
    try:
        encoded = json.dumps(
            dict(chain),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError, RecursionError) as exc:
        raise ProductionInputError("chain must be bounded JSON") from exc
    if len(encoded.encode("utf-8")) > MAX_CHAIN_BYTES:
        raise ProductionInputError("chain exceeds size limit")
    return encoded


def _run_succeeded(result: Any) -> bool:
    if result is None:
        return False
    if isinstance(result, Mapping):
        success = result.get("success")
    else:
        success = getattr(result, "success", None)
    return success is not False


def _normalize_baseline_receipt(value: Any) -> BaselineReceipt:
    if isinstance(value, BaselineReceipt):
        receipt = value
    else:
        card_id = _row_value(value, "memory_card_entity_id", "card_id", "id")
        summary = _row_value(value, "summary")
        status = _row_value(summary, "status_label") or _row_value(value, "status")
        agent_recorded = _row_value(value, "agent_recorded")
        receipt = BaselineReceipt(
            card_id=_validated_id(card_id, "baseline card id"),
            status=_bounded_text(status or "success", 64),
            agent_recorded=(
                agent_recorded if isinstance(agent_recorded, bool) else True
            ),
        )
    return BaselineReceipt(
        card_id=_validated_id(receipt.card_id, "baseline card id"),
        status=_bounded_text(receipt.status, 64),
        agent_recorded=bool(receipt.agent_recorded),
    )


def _normalize_evolution_receipt(value: Any) -> EvolutionReceipt:
    if isinstance(value, EvolutionReceipt):
        receipt = value
    else:
        receipt = EvolutionReceipt(
            evolution_id=_validated_id(
                _row_value(value, "evolution_id", "id"),
                "evolution id",
            ),
            status=_bounded_text(_row_value(value, "status") or "queued", 64),
        )
    return EvolutionReceipt(
        evolution_id=_validated_id(receipt.evolution_id, "evolution id"),
        status=_bounded_text(receipt.status or "queued", 64),
    )


def _complete_unreached_stages(
    run: _RunState,
    sink: EventSink | None,
) -> None:
    blocked = any(
        item.state is ProductionStageState.CANCELLED
        for item in run.terminal.values()
    )
    journal = _Journal(run, sink)
    for stage in ProductionStage:
        if stage in run.terminal:
            continue
        code = ProductionCode.CANCELLED if blocked else ProductionCode.DEPENDENCY
        message = (
            "Not started because the transaction was cancelled."
            if blocked
            else "Not started because an earlier stage did not complete."
        )
        journal.skipped(stage, code, message)


def _validate_policies(policies: ProductionPolicies) -> None:
    for name in ("generate", "save", "run", "baseline", "evolve"):
        if not isinstance(getattr(policies, name), StagePolicy):
            raise TypeError(f"{name} policy must be StagePolicy")


def _production_tags(task_hash: str, metadata: Mapping[str, Any]) -> tuple[str, ...]:
    tags = ["source:chat-prod", f"task-hash:{task_hash}"]
    mode = metadata.get("mode")
    if isinstance(mode, str) and mode.strip():
        tags.append(f"mage:{_slugify_tag(mode)}")
    model = metadata.get("model")
    if isinstance(model, str) and model.strip():
        tags.append(f"mage-model:{_slugify_tag(model)}")
    return tuple(tags)


def _task_hash(task: str, files: tuple[dict[str, Any], ...]) -> str:
    identity = [task]
    for index, item in enumerate(files):
        identity.append(
            "\x1f".join(
                (
                    str(index),
                    str(item.get("path") or ""),
                    str(item.get("sha256") or ""),
                    str(item.get("size_bytes") or ""),
                    str(item.get("mime_type") or ""),
                )
            )
        )
    return _sha256_text("\n".join(identity))[:12]


def _request_binding(request: ProductionRequest) -> str:
    identities = [
        {
            "name": _path_basename(item["path"]),
            "sha256": item["sha256"],
            "size_bytes": item["size_bytes"],
            "mime_type": item.get("mime_type", ""),
        }
        for item in request.context_files
    ]
    payload = {
        "private_task_sha256": _sha256_text(request.private_task),
        "safe_task_sha256": _sha256_text(request.safe_task),
        "user_context_sha256": _sha256_text(request.user_context),
        "recalled_context_sha256": _sha256_text(request.recalled_context),
        "context_files": identities,
        "deployable": True,
    }
    return _sha256_text(
        json.dumps(payload, sort_keys=True, separators=(",", ":"))
    )


def _strip_force(task: str) -> tuple[bool, str]:
    match = _FORCE_RE.match(task)
    if match is None:
        return False, task
    cleaned = task[match.end() :]
    if not cleaned.strip():
        raise ProductionInputError("safe_task is empty after [FORCE]")
    return True, cleaned


def _display_name(task: str) -> str:
    first = next((line.strip() for line in task.splitlines() if line.strip()), "")
    if not first:
        return "Untitled chain"
    return first if len(first) <= 60 else first[:57].rstrip() + "…"


def _slugify_tag(value: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9._-]+", "-", value.strip().lower())
    return cleaned.strip("-") or "unknown"


def _path_basename(value: str) -> str:
    # Treat both separator styles explicitly.  ``Path`` on POSIX otherwise
    # considers a Windows path one filename and could persist private folders.
    name = Path(value.replace("\\", "/")).name or "attachment"
    return _bounded_text(name, 255)


def _row_value(value: Any, *names: str) -> Any:
    for name in names:
        if isinstance(value, Mapping):
            candidate = value.get(name)
        else:
            candidate = getattr(value, name, None)
        if candidate is not None:
            return candidate
    return None


def _task_text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ProductionInputError(f"{label} must be non-empty text")
    if len(value) > MAX_TASK_CHARS or "\x00" in value:
        raise ProductionInputError(f"{label} exceeds its safety limit")
    return value


def _optional_text(value: Any, label: str) -> str:
    if not isinstance(value, str):
        raise ProductionInputError(f"{label} must be text")
    if len(value) > MAX_TASK_CHARS or "\x00" in value:
        raise ProductionInputError(f"{label} exceeds its safety limit")
    return value


def _validated_sha256(value: Any, label: str) -> str:
    if not isinstance(value, str) or not _SHA256_RE.fullmatch(value):
        raise ProductionInputError(f"{label} must be lowercase sha256")
    return value


def _validated_id(value: Any, label: str) -> str:
    if not isinstance(value, str):
        raise ProductionInputError(f"{label} is invalid")
    return _bounded_text(value, MAX_ENTITY_ID_CHARS)


def _bounded_text(value: Any, limit: int, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str):
        raise ProductionInputError("value must be text")
    if (not allow_empty and not value.strip()) or len(value) > limit:
        raise ProductionInputError("text value exceeds its safety limit")
    if _CONTROL_RE.search(value):
        raise ProductionInputError("text value contains control characters")
    return value


def _bounded_message(value: str) -> str:
    clean = _CONTROL_RE.sub(" ", str(value)).strip()
    return clean[:MAX_EVENT_MESSAGE_CHARS]


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


__all__ = [
    "BaselineReceipt",
    "DesktopProductionService",
    "EvolutionReceipt",
    "ExistingChain",
    "ProductionBaselineInput",
    "ProductionBaselineRecordInput",
    "ProductionCode",
    "ProductionConfirmation",
    "ProductionEvolutionInput",
    "ProductionGenerationInput",
    "ProductionInputError",
    "ProductionPolicies",
    "ProductionRequest",
    "ProductionRevisionPointer",
    "ProductionStage",
    "ProductionStageEvent",
    "ProductionStageResult",
    "ProductionStageState",
    "ProductionTransactionResult",
    "ProductionTransactionState",
]
