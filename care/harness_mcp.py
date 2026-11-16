"""Typed MCP facade for the MAESTRO CARE harness.

This module is deliberately transport-thin. CARE owns the domain operations;
the official MCP SDK exposes read-only inspection plus a narrow set of
effectful operations to DeepSeek Harness. The Harness host owns one-shot
approval policy before provider work, chain execution, private staging, a
Memory write, or an evolution effect reaches this adapter. Deployment remains
unavailable until its own typed operation and durable audit boundary exists.

The domain adapter itself has no dependency on ``mcp``.  ``build_mcp_server``
and ``run_stdio`` import the required official Python SDK lazily so pure domain
tests do not have to initialize transport code.  A distributed CARE install is
expected to contain official ``mcp>=1.28,<2`` (the 2025 protocol era used by
DeepSeek Harness' ``@modelcontextprotocol/sdk`` 1.x client).

Cancellation is end-to-end for async work.  The MCP SDK cancels an in-flight
handler on ``notifications/cancelled``; :class:`CareMcpAdapter` then flips the
same :class:`~care.runtime.cancellation.CancellationToken` passed to the CARE
backend before re-raising the cancellation.  The existing synchronous Memory
SDK is run in a worker thread.  Cancellation abandons that result and flips the
token, but cannot forcibly terminate an already-running HTTP call in the
worker; its configured timeout remains the hard bound.
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import os
import re
import secrets
import sys
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol, TypeVar
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from care import __version__ as CARE_VERSION
from care.harness_artifacts import JsonArtifactReceipt
from care.harness_contracts import JsonPointerBinding
from care.harness_commands import (
    CommandCatalogReceipt,
    CommandConfirmationChallenge,
    CommandInvocationReceipt,
    CommandReviewRequest,
    ConfirmedCommandRequest,
    HarnessCommandService,
)
from care.harness_execution import (
    ChainExecutionReceipt,
    ChainExecutionRequest,
    HarnessChainExecutionOperation,
)
from care.harness_evolution import (
    EvolutionCancelReceipt,
    EvolutionCancelRequest,
    EvolutionStartReceipt,
    EvolutionStartRequest,
    EvolutionStatusReceipt,
    EvolutionStatusRequest,
    HarnessEvolutionOperation,
)
from care.harness_generation import (
    ChainGenerationOperationError,
    ChainGenerationReceipt,
    ChainGenerationRequest,
    HarnessChainGenerationOperation,
)
from care.harness_memory_write import (
    ChainMemoryWriteReceipt,
    ChainMemoryWriteRequest,
    HarnessChainMemoryWriteOperation,
)
from care.harness_service import (
    AdaptationInspection,
    AdaptationPreviewResult,
    ContractInspection,
    HarnessOrchestrationService,
)
from care.redaction import redact_sensitive_text
from care.runtime.cancellation import CancellationToken


SERVER_NAME = "maestro-care"
SERVER_VERSION = CARE_VERSION
MCP_PROTOCOL_ERA = "2025"
MCP_PROTOCOL_VERSION = "2025-11-25"
MAX_TOOL_INPUT_BYTES = 2_000_000
MAX_TOOL_OUTPUT_BYTES = 2_000_000
MAX_MEMORY_HITS = 20

_SECRET_FIELD = re.compile(
    r"(?i)(?:authorization|api[-_]?key|access[-_]?token|refresh[-_]?token|"
    r"client[-_]?secret|password|passwd|secret|(?:^|[_-])token(?:$|[_-]))"
)


class StrictModel(BaseModel):
    """Base for MCP wire models: unknown model-produced fields fail closed."""

    model_config = ConfigDict(extra="forbid")


class EmptyRequest(StrictModel):
    """Input for a tool that accepts no arguments."""


class CapabilityInfo(StrictModel):
    name: str
    availability: Literal["available", "unavailable", "approval_gated"]
    mutates_state: bool
    confirmation_required: bool
    cancellation: Literal["cooperative", "best_effort", "not_applicable"]
    note: str | None = None


class CapabilitiesResult(StrictModel):
    status: Literal["completed"] = "completed"
    request_id: str
    server_name: str = SERVER_NAME
    server_version: str = SERVER_VERSION
    transport: Literal["stdio"] = "stdio"
    protocol_era: Literal["2025"] = MCP_PROTOCOL_ERA
    protocol_version: str = MCP_PROTOCOL_VERSION
    capabilities: list[CapabilityInfo]


class ValidateChainRequest(StrictModel):
    chain: dict[str, Any]
    strict_references: bool = True


class ValidateChainData(StrictModel):
    parsed: bool
    ok: bool
    parse_errors: list[str] = Field(default_factory=list)
    required_tools: list[str] = Field(default_factory=list)
    required_mcp_servers: list[str] = Field(default_factory=list)
    required_skills: list[str] = Field(default_factory=list)
    missing_tools: list[str] = Field(default_factory=list)
    missing_mcp_servers: list[str] = Field(default_factory=list)
    missing_skills: list[str] = Field(default_factory=list)
    preflight_available: bool


class ValidateChainResult(ValidateChainData):
    status: Literal["completed"] = "completed"
    request_id: str


MemoryEntityType = Literal["chain", "agent", "agent_skill", "memory_card", "step"]
MemorySearchType = Literal["bm25", "vector", "hybrid"]


class MemorySearchRequest(StrictModel):
    query: str = Field(min_length=1, max_length=4_000)
    entity_type: MemoryEntityType = "chain"
    search_type: MemorySearchType = "bm25"
    top_k: int = Field(default=10, ge=1, le=MAX_MEMORY_HITS)


class MemorySearchData(StrictModel):
    status: Literal["completed", "unavailable"]
    hits: list[dict[str, Any]] = Field(default_factory=list)
    message: str | None = None


class MemorySearchResult(MemorySearchData):
    request_id: str


class MemoryGetChainRequest(StrictModel):
    entity_id: str = Field(min_length=1, max_length=512, pattern=r"^[^\x00-\x1f\x7f]+$")
    channel: str = Field(default="latest", min_length=1, max_length=128, pattern=r"^[A-Za-z0-9._/-]+$")


class MemoryGetChainData(StrictModel):
    status: Literal["completed", "unavailable"]
    entity_id: str
    channel: str
    chain: dict[str, Any] | None = None
    message: str | None = None


class MemoryGetChainResult(MemoryGetChainData):
    request_id: str


class ChainInputContractRequest(StrictModel):
    chain_id: str = Field(min_length=1, max_length=512, pattern=r"^[^\x00-\x1f\x7f]+$")
    channel: str = Field(
        default="latest",
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9._/-]+$",
    )


class ChainInputContractData(StrictModel):
    status: Literal["completed", "unavailable"]
    inspection: ContractInspection | None = None
    message: str | None = None


class ChainInputContractResult(ChainInputContractData):
    request_id: str


class InputAdaptationInspectRequest(ChainInputContractRequest):
    artifact_id: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9_-]+$",
    )


class InputAdaptationInspectData(StrictModel):
    status: Literal["completed", "unavailable"]
    inspection: AdaptationInspection | None = None
    message: str | None = None


class InputAdaptationInspectResult(InputAdaptationInspectData):
    request_id: str


class InputAdaptationPreviewRequest(InputAdaptationInspectRequest):
    artifact_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    bindings: list[JsonPointerBinding] = Field(max_length=256)


class InputAdaptationPreviewData(StrictModel):
    status: Literal["completed", "unavailable"]
    result: AdaptationPreviewResult | None = None
    message: str | None = None


class InputAdaptationPreviewResult(InputAdaptationPreviewData):
    request_id: str


class GenerateChainData(StrictModel):
    status: Literal["completed", "unavailable"]
    receipt: ChainGenerationReceipt | None = None
    message: str | None = None


class GenerateChainResult(GenerateChainData):
    request_id: str


class StageArtifactRequest(StrictModel):
    """Privately stage one JSON object already present in the conversation."""

    kind: Literal["input", "chain"] = "input"
    payload: dict[str, Any]


class StageArtifactData(StrictModel):
    status: Literal["completed", "unavailable"]
    kind: Literal["input", "chain"]
    receipt: JsonArtifactReceipt | None = None
    message: str | None = None


class StageArtifactResult(StageArtifactData):
    request_id: str


class ExecuteChainData(StrictModel):
    status: Literal["completed", "unavailable"]
    receipt: ChainExecutionReceipt | None = None
    message: str | None = None


class ExecuteChainResult(ExecuteChainData):
    request_id: str


class MemoryWriteData(StrictModel):
    status: Literal["completed", "unavailable"]
    receipt: ChainMemoryWriteReceipt | None = None
    message: str | None = None


class MemoryWriteResult(MemoryWriteData):
    request_id: str


class EvolutionStartData(StrictModel):
    status: Literal["completed", "unavailable"]
    receipt: EvolutionStartReceipt | None = None
    message: str | None = None


class EvolutionStartResult(EvolutionStartData):
    request_id: str


class EvolutionStatusData(StrictModel):
    status: Literal["completed", "unavailable"]
    receipt: EvolutionStatusReceipt | None = None
    message: str | None = None


class EvolutionStatusResult(EvolutionStatusData):
    request_id: str


class EvolutionCancelData(StrictModel):
    status: Literal["completed", "unavailable"]
    receipt: EvolutionCancelReceipt | None = None
    message: str | None = None


class EvolutionCancelResult(EvolutionCancelData):
    request_id: str


class CommandCatalogData(StrictModel):
    status: Literal["completed", "unavailable"]
    receipt: CommandCatalogReceipt | None = None
    message: str | None = None


class CommandCatalogResult(CommandCatalogData):
    request_id: str


class CommandReviewToolRequest(StrictModel):
    command: str = Field(min_length=1, max_length=16_384)
    # ``Any | None`` emits oneOf [{}, {type: null}], which DeepSeek's exact-one
    # validator rejects for null because an unconstrained branch also matches.
    # Plain Any admits the same runtime values and advertises one open branch.
    transcript: Any = None


class CommandConfirmToolRequest(CommandReviewToolRequest):
    challenge_id: str = Field(
        min_length=16,
        max_length=128,
        pattern=r"^[A-Za-z0-9_-]+$",
    )


class CommandConfirmationData(StrictModel):
    challenge_id: str = Field(
        min_length=16,
        max_length=128,
        pattern=r"^[A-Za-z0-9_-]+$",
    )
    ttl_seconds: float = Field(gt=0, le=86_400)
    risk: Literal[
        "filesystem_write_or_mutation",
        "external_side_effect",
        "provider_or_tool_execution",
    ]
    reason: str
    effects: list[str] = Field(default_factory=list)


class CommandInvocationData(StrictModel):
    status: Literal["completed", "unavailable"]
    phase: Literal["review", "confirm"] | None = None
    outcome: Literal["completed", "confirmation_required", "rejected"] | None = None
    status_code: int | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
    confirmation: CommandConfirmationData | None = None
    message: str | None = None


class CommandInvocationResult(CommandInvocationData):
    request_id: str


class CommandStatusToolRequest(StrictModel):
    challenge_id: str = Field(
        min_length=16,
        max_length=128,
        pattern=r"^[A-Za-z0-9_-]+$",
    )


class CommandStatusData(StrictModel):
    status: Literal["completed", "unavailable"]
    challenge_id: str | None = Field(
        default=None,
        min_length=16,
        max_length=128,
        pattern=r"^[A-Za-z0-9_-]+$",
    )
    reconciliation_state: Literal[
        "unknown",
        "running",
        "completed",
        "failed",
        "cancelled",
    ] | None = None
    invocation: CommandInvocationData | None = None
    message: str | None = None


class CommandStatusResult(CommandStatusData):
    request_id: str


class HarnessMcpBackend(Protocol):
    """Domain boundary injected into :class:`CareMcpAdapter`."""

    @property
    def memory_available(self) -> bool: ...

    @property
    def orchestration_available(self) -> bool: ...

    @property
    def generation_available(self) -> bool: ...

    @property
    def artifact_staging_available(self) -> bool: ...

    @property
    def execution_available(self) -> bool: ...

    @property
    def memory_write_available(self) -> bool: ...

    @property
    def evolution_available(self) -> bool: ...

    @property
    def command_surface_available(self) -> bool: ...

    async def validate_chain(
        self,
        request: ValidateChainRequest,
        *,
        cancellation: CancellationToken,
    ) -> ValidateChainData: ...

    async def search_memory(
        self,
        request: MemorySearchRequest,
        *,
        cancellation: CancellationToken,
    ) -> MemorySearchData: ...

    async def get_memory_chain(
        self,
        request: MemoryGetChainRequest,
        *,
        cancellation: CancellationToken,
    ) -> MemoryGetChainData: ...

    async def inspect_chain_input_contract(
        self,
        request: ChainInputContractRequest,
        *,
        cancellation: CancellationToken,
    ) -> ChainInputContractData: ...

    async def inspect_input_adaptation(
        self,
        request: InputAdaptationInspectRequest,
        *,
        cancellation: CancellationToken,
    ) -> InputAdaptationInspectData: ...

    async def preview_input_adaptation(
        self,
        request: InputAdaptationPreviewRequest,
        *,
        cancellation: CancellationToken,
    ) -> InputAdaptationPreviewData: ...

    async def generate_chain(
        self,
        request: ChainGenerationRequest,
        *,
        cancellation: CancellationToken,
    ) -> GenerateChainData: ...

    async def stage_artifact(
        self,
        request: StageArtifactRequest,
        *,
        cancellation: CancellationToken,
    ) -> StageArtifactData: ...

    async def execute_chain(
        self,
        request: ChainExecutionRequest,
        *,
        cancellation: CancellationToken,
    ) -> ExecuteChainData: ...

    async def write_chain_to_memory(
        self,
        request: ChainMemoryWriteRequest,
        *,
        cancellation: CancellationToken,
    ) -> MemoryWriteData: ...

    async def start_evolution(
        self,
        request: EvolutionStartRequest,
        *,
        cancellation: CancellationToken,
    ) -> EvolutionStartData: ...

    async def get_evolution_status(
        self,
        request: EvolutionStatusRequest,
        *,
        cancellation: CancellationToken,
    ) -> EvolutionStatusData: ...

    async def cancel_evolution(
        self,
        request: EvolutionCancelRequest,
        *,
        cancellation: CancellationToken,
    ) -> EvolutionCancelData: ...

    async def command_catalog(
        self,
        *,
        cancellation: CancellationToken,
    ) -> CommandCatalogData: ...

    async def review_command(
        self,
        request: CommandReviewToolRequest,
        *,
        cancellation: CancellationToken,
    ) -> CommandInvocationData: ...

    async def confirm_command(
        self,
        request: CommandConfirmToolRequest,
        *,
        cancellation: CancellationToken,
    ) -> CommandInvocationData: ...

    async def get_command_status(
        self,
        request: CommandStatusToolRequest,
        *,
        cancellation: CancellationToken,
    ) -> CommandStatusData: ...


@dataclass(frozen=True, slots=True)
class _PendingCommandChallenge:
    binding_sha256: str
    bridge_token: str = field(repr=False)
    expires_at: float


class _CommandChallengeLedger:
    """Keep CommandBridge bearer tokens out of durable Harness history."""

    def __init__(
        self,
        *,
        max_pending: int = 128,
        challenge_id_factory: Callable[[], str] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if max_pending < 1:
            raise ValueError("max_pending command challenges must be positive")
        self._max_pending = max_pending
        self._challenge_id_factory = challenge_id_factory or (
            lambda: secrets.token_urlsafe(24)
        )
        self._clock = clock
        self._lock = threading.Lock()
        self._pending: dict[str, _PendingCommandChallenge] = {}

    @staticmethod
    def _binding(command: str, transcript: Any) -> str:
        encoded = json.dumps(
            {"command": command, "transcript": transcript},
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def issue(
        self,
        challenge: CommandConfirmationChallenge,
        *,
        command: str,
        transcript: Any,
    ) -> CommandConfirmationData:
        now = self._clock()
        with self._lock:
            self._pending = {
                key: value
                for key, value in self._pending.items()
                if value.expires_at > now
            }
            while len(self._pending) >= self._max_pending:
                oldest = min(
                    self._pending,
                    key=lambda key: self._pending[key].expires_at,
                )
                self._pending.pop(oldest, None)
            challenge_id = self._challenge_id_factory()
            while (
                not isinstance(challenge_id, str)
                or re.fullmatch(r"[A-Za-z0-9_-]{16,128}", challenge_id) is None
                or challenge_id in self._pending
            ):
                challenge_id = self._challenge_id_factory()
            self._pending[challenge_id] = _PendingCommandChallenge(
                binding_sha256=self._binding(command, transcript),
                bridge_token=challenge.token,
                expires_at=now + challenge.ttl_seconds,
            )
        return CommandConfirmationData(
            challenge_id=challenge_id,
            ttl_seconds=challenge.ttl_seconds,
            risk=challenge.risk,
            reason=challenge.reason,
            effects=list(challenge.effects),
        )

    def consume(
        self,
        challenge_id: str,
        *,
        command: str,
        transcript: Any,
    ) -> str | None:
        now = self._clock()
        binding = self._binding(command, transcript)
        with self._lock:
            pending = self._pending.pop(challenge_id, None)
        if (
            pending is None
            or pending.expires_at <= now
            or not secrets.compare_digest(pending.binding_sha256, binding)
        ):
            return None
        return pending.bridge_token


_RESIDENT_LOCAL_REPLACEMENTS = {
    "artifacts": "Use MAESTRO artifact receipts and the Harness transcript.",
    "blocks": "Use the Harness transcript controls; CARE block actions are not wired.",
    "branch": "Use DeepSeek Harness session branching when available.",
    "clear": "Start a fresh DeepSeek Harness session.",
    "edit": "Edit or resend a message through the Harness conversation UI.",
    "exit": "Close the native MAESTRO application window.",
    "help": "Use the Harness help and the maestro_command_catalog tool.",
    "history": "Use DeepSeek Harness session history.",
    "mode": "Choose the resident-agent preset and model in Harness Settings.",
    "multi": "Use the Harness multiline composer.",
    "new": "Create a new DeepSeek Harness session.",
    "quit": "Close the native MAESTRO application window.",
    "resume": "Open the required DeepSeek Harness session from history.",
    "runs": "Use DeepSeek Harness session history and Jobs.",
    "sessions": "Use DeepSeek Harness session history.",
    "settings": (
        "Open DeepSeek Harness Settings for the chat model; CARE capability "
        "settings remain in CARE configuration."
    ),
    "subagents": "Inspect the Harness tool timeline and Jobs.",
    "theme": "Choose the application theme in Harness Settings.",
    "tour": "Use the native MAESTRO onboarding flow.",
}


def _resident_command_catalog(receipt: CommandCatalogReceipt) -> CommandCatalogReceipt:
    """Project desktop-local slash rows honestly for the resident DSH host."""

    catalog = copy.deepcopy(receipt.catalog)
    slash_commands = catalog.get("slash_commands")
    host_managed = 0
    available = 0
    if isinstance(slash_commands, list):
        for raw_entry in slash_commands:
            if not isinstance(raw_entry, dict):
                continue
            if raw_entry.get("classification") == "local":
                name = raw_entry.get("name")
                raw_entry.update(
                    {
                        "available": False,
                        "scope": "resident_host",
                        "execution": "deepseek_harness",
                        "parity": "host_replacement",
                        "reason": (
                            "This CARE desktop-local slash command is not "
                            "executable through the resident command bridge."
                        ),
                        "replacement": _RESIDENT_LOCAL_REPLACEMENTS.get(
                            name,
                            "Use the equivalent DeepSeek Harness control.",
                        ),
                    }
                )
                host_managed += 1
            if raw_entry.get("available") is True:
                available += 1
    counts = catalog.get("counts")
    if isinstance(counts, dict):
        counts["slash_host_managed"] = host_managed
        counts["slash_available_in_resident"] = available
    catalog["resident_projection"] = {
        "host": "deepseek_harness",
        "desktop_local_commands_available": False,
    }
    canonical = json.dumps(
        catalog,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return CommandCatalogReceipt(
        catalog_sha256=hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
        catalog=catalog,
    )


@dataclass(slots=True)
class _CommandRunRecord:
    binding_sha256: str
    task: asyncio.Task[CommandInvocationData]
    expires_at: float
    observed: bool = False


class _CommandRunLedger:
    """Retain outcomes when an MCP cancellation cannot stop a sync effect."""

    def __init__(
        self,
        *,
        max_runs: int = 128,
        retention_seconds: float = 3_600,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if max_runs < 1:
            raise ValueError("max_runs must be positive")
        if retention_seconds <= 0:
            raise ValueError("retention_seconds must be positive")
        self._max_runs = max_runs
        self._retention_seconds = retention_seconds
        self._clock = clock
        self._lock = threading.Lock()
        self._records: dict[str, _CommandRunRecord] = {}
        self._by_binding: dict[str, str] = {}

    @staticmethod
    def binding(command: str, transcript: Any) -> str:
        return _CommandChallengeLedger._binding(command, transcript)

    @staticmethod
    def _state(
        task: asyncio.Task[CommandInvocationData],
    ) -> Literal["running", "completed", "failed", "cancelled"]:
        if not task.done():
            return "running"
        if task.cancelled():
            return "cancelled"
        return "failed" if task.exception() is not None else "completed"

    def _remove_locked(self, challenge_id: str) -> None:
        record = self._records.pop(challenge_id, None)
        if (
            record is not None
            and self._by_binding.get(record.binding_sha256) == challenge_id
        ):
            self._by_binding.pop(record.binding_sha256, None)

    def _cleanup_locked(self, now: float) -> None:
        for challenge_id, record in tuple(self._records.items()):
            if record.task.done() and record.expires_at <= now:
                self._remove_locked(challenge_id)

    def unreconciled(
        self,
        *,
        command: str,
        transcript: Any,
    ) -> tuple[str, Literal["running", "completed", "failed", "cancelled"]] | None:
        binding = self.binding(command, transcript)
        now = self._clock()
        with self._lock:
            self._cleanup_locked(now)
            challenge_id = self._by_binding.get(binding)
            if challenge_id is None:
                return None
            record = self._records.get(challenge_id)
            if record is None or record.observed:
                self._by_binding.pop(binding, None)
                return None
            return challenge_id, self._state(record.task)

    def register(
        self,
        challenge_id: str,
        *,
        command: str,
        transcript: Any,
        task_factory: Callable[[], Any],
    ) -> asyncio.Task[CommandInvocationData] | None:
        binding = self.binding(command, transcript)
        now = self._clock()
        with self._lock:
            self._cleanup_locked(now)
            if challenge_id in self._records or binding in self._by_binding:
                return None
            if len(self._records) >= self._max_runs:
                return None
            task = asyncio.create_task(task_factory())
            record = _CommandRunRecord(
                binding_sha256=binding,
                task=task,
                # A running effect is never aged out. Completion resets this.
                expires_at=float("inf"),
            )
            self._records[challenge_id] = record
            self._by_binding[binding] = challenge_id

        def completed(_task: asyncio.Task[CommandInvocationData]) -> None:
            with self._lock:
                current = self._records.get(challenge_id)
                if current is record:
                    current.expires_at = self._clock() + self._retention_seconds

        task.add_done_callback(completed)
        return task

    def mark_delivered(self, challenge_id: str) -> None:
        with self._lock:
            record = self._records.get(challenge_id)
            if record is None:
                return
            record.observed = True
            if self._by_binding.get(record.binding_sha256) == challenge_id:
                self._by_binding.pop(record.binding_sha256, None)

    def status(self, challenge_id: str) -> CommandStatusData:
        now = self._clock()
        with self._lock:
            self._cleanup_locked(now)
            record = self._records.get(challenge_id)
            if record is None:
                return CommandStatusData(
                    status="completed",
                    challenge_id=challenge_id,
                    reconciliation_state="unknown",
                    message="No retained command execution matches this challenge.",
                )
            state = self._state(record.task)
            invocation = (
                record.task.result() if state == "completed" else None
            )
            if state != "running":
                record.observed = True
                if self._by_binding.get(record.binding_sha256) == challenge_id:
                    self._by_binding.pop(record.binding_sha256, None)
        return CommandStatusData(
            status="completed",
            challenge_id=challenge_id,
            reconciliation_state=state,
            invocation=invocation,
            message=(
                "The synchronous command is still running after transport cancellation."
                if state == "running"
                else (
                    "The command worker ended without a retained receipt."
                    if state in {"failed", "cancelled"}
                    else None
                )
            ),
        )


class CareReadOnlyMcpBackend:
    """Compatibility-named adapter over typed CARE harness operations.

    ``memory`` is injected instead of being constructed implicitly.  Starting
    an MCP server therefore never opens a network connection or reads a secret;
    the native harness may pass an already-configured :class:`CareMemory` when
    the user enabled Memory for that session.
    """

    def __init__(
        self,
        *,
        memory: Any | None = None,
        orchestration: HarnessOrchestrationService | None = None,
        generation: HarnessChainGenerationOperation | None = None,
        artifact_stager: Any | None = None,
        execution: HarnessChainExecutionOperation | None = None,
        memory_write: HarnessChainMemoryWriteOperation | None = None,
        evolution: HarnessEvolutionOperation | None = None,
        commands: HarnessCommandService | None = None,
    ) -> None:
        self._memory = memory
        self._orchestration = orchestration
        self._generation = generation
        self._artifact_stager = artifact_stager
        self._execution = execution
        self._memory_write = memory_write
        self._evolution = evolution
        self._commands = commands
        self._command_challenges = (
            _CommandChallengeLedger() if commands is not None else None
        )
        self._command_runs = _CommandRunLedger() if commands is not None else None

    @property
    def memory_available(self) -> bool:
        return self._memory is not None

    @property
    def orchestration_available(self) -> bool:
        return self._orchestration is not None

    @property
    def generation_available(self) -> bool:
        return self._generation is not None

    @property
    def artifact_staging_available(self) -> bool:
        return callable(getattr(self._artifact_stager, "stage", None))

    @property
    def execution_available(self) -> bool:
        return self._execution is not None

    @property
    def memory_write_available(self) -> bool:
        return self._memory_write is not None

    @property
    def evolution_available(self) -> bool:
        return self._evolution is not None

    @property
    def command_surface_available(self) -> bool:
        return self._commands is not None

    async def validate_chain(
        self,
        request: ValidateChainRequest,
        *,
        cancellation: CancellationToken,
    ) -> ValidateChainData:
        from care.preflight import validate_chain

        report = await _run_sync_cancellable(
            lambda: validate_chain(
                request.chain,
                context=None,
                use_typed_steps=True,
                strict_references=request.strict_references,
            ),
            cancellation,
        )
        return ValidateChainData(
            parsed=report.parsed,
            ok=report.ok,
            parse_errors=list(report.parse_errors),
            required_tools=list(report.required_tools),
            required_mcp_servers=list(report.required_mcp_servers),
            required_skills=list(report.required_skills),
            missing_tools=list(report.missing_tools),
            missing_mcp_servers=list(report.missing_mcp_servers),
            missing_skills=list(report.missing_skills),
            preflight_available=report.preflight_available,
        )

    async def search_memory(
        self,
        request: MemorySearchRequest,
        *,
        cancellation: CancellationToken,
    ) -> MemorySearchData:
        if self._memory is None:
            return MemorySearchData(
                status="unavailable",
                message="Memory is not configured for this MCP session.",
            )
        hits = await _run_sync_cancellable(
            lambda: self._memory.search(
                request.query,
                entity_type=request.entity_type,
                search_type=request.search_type,
                top_k=request.top_k,
            ),
            cancellation,
        )
        return MemorySearchData(status="completed", hits=list(hits))

    async def get_memory_chain(
        self,
        request: MemoryGetChainRequest,
        *,
        cancellation: CancellationToken,
    ) -> MemoryGetChainData:
        if self._memory is None:
            return MemoryGetChainData(
                status="unavailable",
                entity_id=request.entity_id,
                channel=request.channel,
                message="Memory is not configured for this MCP session.",
            )
        chain = await _run_sync_cancellable(
            lambda: self._memory.get_chain(request.entity_id, channel=request.channel),
            cancellation,
        )
        return MemoryGetChainData(
            status="completed",
            entity_id=request.entity_id,
            channel=request.channel,
            chain=dict(chain),
        )

    async def inspect_chain_input_contract(
        self,
        request: ChainInputContractRequest,
        *,
        cancellation: CancellationToken,
    ) -> ChainInputContractData:
        if self._orchestration is None:
            return ChainInputContractData(
                status="unavailable",
                message="Chain contract inspection is not configured for this MCP session.",
            )
        inspection = await _run_sync_cancellable(
            lambda: self._orchestration.inspect_contract(
                request.chain_id,
                channel=request.channel,
                cancellation=cancellation,
            ),
            cancellation,
        )
        return ChainInputContractData(status="completed", inspection=inspection)

    async def inspect_input_adaptation(
        self,
        request: InputAdaptationInspectRequest,
        *,
        cancellation: CancellationToken,
    ) -> InputAdaptationInspectData:
        if self._orchestration is None:
            return InputAdaptationInspectData(
                status="unavailable",
                message="Input adaptation is not configured for this MCP session.",
            )
        inspection = await _run_sync_cancellable(
            lambda: self._orchestration.inspect_adaptation(
                request.chain_id,
                request.artifact_id,
                channel=request.channel,
                cancellation=cancellation,
            ),
            cancellation,
        )
        return InputAdaptationInspectData(status="completed", inspection=inspection)

    async def preview_input_adaptation(
        self,
        request: InputAdaptationPreviewRequest,
        *,
        cancellation: CancellationToken,
    ) -> InputAdaptationPreviewData:
        if self._orchestration is None:
            return InputAdaptationPreviewData(
                status="unavailable",
                message="Input adaptation is not configured for this MCP session.",
            )
        result = await _run_sync_cancellable(
            lambda: self._orchestration.preview_adaptation(
                request.chain_id,
                request.artifact_id,
                artifact_sha256=request.artifact_sha256,
                bindings=request.bindings,
                channel=request.channel,
                cancellation=cancellation,
            ),
            cancellation,
        )
        return InputAdaptationPreviewData(status="completed", result=result)

    async def generate_chain(
        self,
        request: ChainGenerationRequest,
        *,
        cancellation: CancellationToken,
    ) -> GenerateChainData:
        if self._generation is None:
            return GenerateChainData(
                status="unavailable",
                message="MAGE generation is not configured for this MCP session.",
            )
        try:
            receipt = await self._generation.generate(
                request,
                cancellation=cancellation,
            )
        except ChainGenerationOperationError:
            # The domain error is already type-only. Keep one common MCP error
            # boundary so provider details remain only on the exception chain.
            raise
        return GenerateChainData(status="completed", receipt=receipt)

    async def stage_artifact(
        self,
        request: StageArtifactRequest,
        *,
        cancellation: CancellationToken,
    ) -> StageArtifactData:
        if not self.artifact_staging_available:
            return StageArtifactData(
                status="unavailable",
                kind=request.kind,
                message="Private artifact staging is not configured for this session.",
            )
        if request.kind == "chain":
            validation = await self.validate_chain(
                ValidateChainRequest(
                    chain=request.payload,
                    strict_references=True,
                ),
                cancellation=cancellation,
            )
            if not validation.parsed or validation.parse_errors:
                raise ValueError("chain artifact failed strict admission")
        receipt = await _run_sync_cancellable(
            lambda: self._artifact_stager.stage(request.payload),
            cancellation,
        )
        return StageArtifactData(
            status="completed",
            kind=request.kind,
            receipt=JsonArtifactReceipt.model_validate(receipt),
        )

    async def execute_chain(
        self,
        request: ChainExecutionRequest,
        *,
        cancellation: CancellationToken,
    ) -> ExecuteChainData:
        if self._execution is None:
            return ExecuteChainData(
                status="unavailable",
                message="CARL execution is not configured for this MCP session.",
            )
        receipt = await self._execution.execute(
            request,
            cancellation=cancellation,
        )
        return ExecuteChainData(status="completed", receipt=receipt)

    async def write_chain_to_memory(
        self,
        request: ChainMemoryWriteRequest,
        *,
        cancellation: CancellationToken,
    ) -> MemoryWriteData:
        if self._memory_write is None:
            return MemoryWriteData(
                status="unavailable",
                message="Memory writes are not configured for this MCP session.",
            )
        cancellation.raise_if_cancelled()
        receipt = await self._memory_write.save(request)
        cancellation.raise_if_cancelled()
        return MemoryWriteData(status="completed", receipt=receipt)

    async def start_evolution(
        self,
        request: EvolutionStartRequest,
        *,
        cancellation: CancellationToken,
    ) -> EvolutionStartData:
        if self._evolution is None:
            return EvolutionStartData(
                status="unavailable",
                message="Evolution is not configured for this MCP session.",
            )
        receipt = await self._evolution.start(
            request,
            cancellation=cancellation,
        )
        return EvolutionStartData(status="completed", receipt=receipt)

    async def get_evolution_status(
        self,
        request: EvolutionStatusRequest,
        *,
        cancellation: CancellationToken,
    ) -> EvolutionStatusData:
        if self._evolution is None:
            return EvolutionStatusData(
                status="unavailable",
                message="Evolution is not configured for this MCP session.",
            )
        receipt = await self._evolution.status(
            request,
            cancellation=cancellation,
        )
        return EvolutionStatusData(status="completed", receipt=receipt)

    async def cancel_evolution(
        self,
        request: EvolutionCancelRequest,
        *,
        cancellation: CancellationToken,
    ) -> EvolutionCancelData:
        if self._evolution is None:
            return EvolutionCancelData(
                status="unavailable",
                message="Evolution is not configured for this MCP session.",
            )
        receipt = await self._evolution.cancel(
            request,
            cancellation=cancellation,
        )
        return EvolutionCancelData(status="completed", receipt=receipt)

    async def command_catalog(
        self,
        *,
        cancellation: CancellationToken,
    ) -> CommandCatalogData:
        if self._commands is None:
            return CommandCatalogData(
                status="unavailable",
                message="The reviewed CARE command surface is unavailable.",
            )
        receipt = await self._commands.catalog(cancellation)
        return CommandCatalogData(
            status="completed",
            receipt=_resident_command_catalog(receipt),
        )

    async def review_command(
        self,
        request: CommandReviewToolRequest,
        *,
        cancellation: CancellationToken,
    ) -> CommandInvocationData:
        if self._commands is None:
            return CommandInvocationData(
                status="unavailable",
                message="The reviewed CARE command surface is unavailable.",
            )
        if self._command_runs is None:  # pragma: no cover - init invariant
            raise AssertionError("command run ledger is unavailable")
        unreconciled = self._command_runs.unreconciled(
            command=request.command,
            transcript=request.transcript,
        )
        if unreconciled is not None:
            challenge_id, state = unreconciled
            return _command_reconciliation_required(
                challenge_id,
                state=state,
                phase="review",
            )
        receipt = await self._commands.plan_or_execute(
            CommandReviewRequest(
                command=request.command,
                transcript=request.transcript,
            ),
            cancellation,
        )
        confirmation = None
        if receipt.confirmation is not None:
            if self._command_challenges is None:  # pragma: no cover - init invariant
                raise AssertionError("command challenge ledger is unavailable")
            confirmation = self._command_challenges.issue(
                receipt.confirmation,
                command=request.command,
                transcript=request.transcript,
            )
        return _command_invocation_data(receipt, confirmation=confirmation)

    async def confirm_command(
        self,
        request: CommandConfirmToolRequest,
        *,
        cancellation: CancellationToken,
    ) -> CommandInvocationData:
        if self._commands is None:
            return CommandInvocationData(
                status="unavailable",
                message="The reviewed CARE command surface is unavailable.",
            )
        if self._command_challenges is None:  # pragma: no cover - init invariant
            raise AssertionError("command challenge ledger is unavailable")
        if self._command_runs is None:  # pragma: no cover - init invariant
            raise AssertionError("command run ledger is unavailable")
        cancellation.raise_if_cancelled()
        unreconciled = self._command_runs.unreconciled(
            command=request.command,
            transcript=request.transcript,
        )
        if unreconciled is not None:
            challenge_id, state = unreconciled
            return _command_reconciliation_required(
                challenge_id,
                state=state,
                phase="confirm",
            )
        bridge_token = self._command_challenges.consume(
            request.challenge_id,
            command=request.command,
            transcript=request.transcript,
        )
        if bridge_token is None:
            return CommandInvocationData(
                status="completed",
                phase="confirm",
                outcome="rejected",
                status_code=409,
                payload={
                    "ok": False,
                    "executed": False,
                    "error": {
                        "code": "confirmation_invalid",
                        "message": (
                            "The command challenge is invalid, expired, replayed, "
                            "or bound to another command/transcript."
                        ),
                    },
                },
            )
        confirmed = ConfirmedCommandRequest(
            command=request.command,
            confirmation_token=bridge_token,
            transcript=request.transcript,
        )
        task = self._command_runs.register(
            request.challenge_id,
            command=request.command,
            transcript=request.transcript,
            task_factory=lambda: self._execute_confirmed_command(confirmed),
        )
        if task is None:
            return CommandInvocationData(
                status="completed",
                phase="confirm",
                outcome="rejected",
                status_code=503,
                payload={
                    "ok": False,
                    "executed": False,
                    "error": {
                        "code": "command_reconciliation_capacity",
                        "message": (
                            "The command was not dispatched because the bounded "
                            "reconciliation ledger is full."
                        ),
                    },
                },
            )
        # Once the synchronous effect is dispatched, transport cancellation
        # cannot prove that it stopped. Shield the worker, retain its receipt,
        # and let the caller query maestro_command_status before any retry.
        result = await asyncio.shield(task)
        self._command_runs.mark_delivered(request.challenge_id)
        return result

    async def _execute_confirmed_command(
        self,
        request: ConfirmedCommandRequest,
    ) -> CommandInvocationData:
        if self._commands is None:  # pragma: no cover - init invariant
            raise AssertionError("command service is unavailable")
        try:
            receipt = await self._commands.confirm_and_execute(
                request,
                CancellationToken(
                    reason="resident command worker stopped during host shutdown"
                ),
            )
        except Exception as exc:  # noqa: BLE001 - retain a stable safe receipt
            return CommandInvocationData(
                status="completed",
                phase="confirm",
                outcome="rejected",
                status_code=500,
                payload={
                    "ok": False,
                    "executed": False,
                    "error": {
                        "code": "command_worker_failed",
                        "message": (
                            "The command worker ended without a normal bridge receipt."
                        ),
                        "failure_type": type(exc).__name__[:160],
                    },
                },
            )
        return _command_invocation_data(receipt)

    async def get_command_status(
        self,
        request: CommandStatusToolRequest,
        *,
        cancellation: CancellationToken,
    ) -> CommandStatusData:
        cancellation.raise_if_cancelled()
        if self._commands is None or self._command_runs is None:
            return CommandStatusData(
                status="unavailable",
                message="The reviewed CARE command surface is unavailable.",
            )
        return self._command_runs.status(request.challenge_id)


def _command_invocation_data(
    receipt: CommandInvocationReceipt,
    *,
    confirmation: CommandConfirmationData | None = None,
) -> CommandInvocationData:
    if (receipt.confirmation is None) != (confirmation is None):
        raise ValueError("public command challenge does not match bridge receipt")
    return CommandInvocationData(
        status="completed",
        phase=receipt.phase,
        outcome=receipt.outcome,
        status_code=receipt.status_code,
        payload=receipt.payload,
        confirmation=confirmation,
    )


def _command_reconciliation_required(
    challenge_id: str,
    *,
    state: Literal["running", "completed", "failed", "cancelled"],
    phase: Literal["review", "confirm"],
) -> CommandInvocationData:
    return CommandInvocationData(
        status="completed",
        phase=phase,
        outcome="rejected",
        status_code=409,
        payload={
            "ok": False,
            "executed": False,
            "error": {
                "code": "command_reconciliation_required",
                "message": (
                    "Inspect maestro_command_status for the retained command "
                    "outcome before reviewing or dispatching this command again."
                ),
            },
            "reconciliation": {
                "challenge_id": challenge_id,
                "state": state,
            },
        },
    )


class HarnessMcpError(RuntimeError):
    """Safe, user-facing MCP boundary error."""


class McpSdkUnavailableError(HarnessMcpError):
    """The required official MCP SDK is missing from the installation."""


class UnknownHarnessToolError(HarnessMcpError):
    """The client requested a tool this adapter did not advertise."""


class InvalidHarnessToolArguments(HarnessMcpError):
    """A tool call failed strict Pydantic admission."""


class HarnessToolExecutionError(HarnessMcpError):
    """A backend failed; the original exception remains chained, not exposed."""


RequestModelT = TypeVar("RequestModelT", bound=StrictModel)
ResultModelT = TypeVar("ResultModelT", bound=StrictModel)


@dataclass(frozen=True)
class HarnessToolSpec:
    name: str
    description: str
    request_model: type[StrictModel]
    result_model: type[StrictModel]

    @property
    def input_schema(self) -> dict[str, Any]:
        return _deepseek_compatible_schema(self.request_model.model_json_schema())

    @property
    def output_schema(self) -> dict[str, Any]:
        return _deepseek_compatible_schema(self.result_model.model_json_schema())


_TOOL_SPECS = (
    HarnessToolSpec(
        name="maestro_capabilities",
        description=(
            "List the CARE capabilities available through this MCP session and "
            "which mutating operations remain gated by the native host."
        ),
        request_model=EmptyRequest,
        result_model=CapabilitiesResult,
    ),
    HarnessToolSpec(
        name="maestro_command_catalog",
        description=(
            "Return the complete reviewed CARE CLI/TUI command registry without "
            "executing a command. This read-only catalog identifies exact, "
            "adapted, unavailable, and confirmation-gated forms."
        ),
        request_model=EmptyRequest,
        result_model=CommandCatalogResult,
    ),
    HarnessToolSpec(
        name="maestro_command_review",
        description=(
            "Review one CARE command through the shell-free command bridge. "
            "Read-only commands may execute; an effectful command can only "
            "return a short-lived challenge id and cannot perform the effect "
            "in this tool. The underlying bearer token remains server-side."
        ),
        request_model=CommandReviewToolRequest,
        result_model=CommandInvocationResult,
    ),
    HarnessToolSpec(
        name="maestro_command_status",
        description=(
            "Reconcile an effectful CARE command after transport cancellation. "
            "The public challenge id is not execution authority; this read-only "
            "tool returns running state or the retained final receipt."
        ),
        request_model=CommandStatusToolRequest,
        result_model=CommandStatusResult,
    ),
    HarnessToolSpec(
        name="maestro_command_confirm",
        description=(
            "Consume the exact state-bound challenge returned by "
            "maestro_command_review and execute that same CARE command. This "
            "effectful tool is separately approval-gated by DeepSeek Harness."
        ),
        request_model=CommandConfirmToolRequest,
        result_model=CommandInvocationResult,
    ),
    HarnessToolSpec(
        name="maestro_validate_chain",
        description=(
            "Strictly parse a CARL chain and report declared or missing tools, MCP "
            "servers, and skills. This is read-only and performs no provider call."
        ),
        request_model=ValidateChainRequest,
        result_model=ValidateChainResult,
    ),
    HarnessToolSpec(
        name="maestro_memory_search",
        description="Search configured MAESTRO Memory without modifying it.",
        request_model=MemorySearchRequest,
        result_model=MemorySearchResult,
    ),
    HarnessToolSpec(
        name="maestro_memory_get_chain",
        description="Fetch one CARL chain from configured MAESTRO Memory without modifying it.",
        request_model=MemoryGetChainRequest,
        result_model=MemoryGetChainResult,
    ),
    HarnessToolSpec(
        name="maestro_chain_input_contract",
        description=(
            "Inspect the explicit runtime input JSON Schema of one exact CARL "
            "chain revision. This never guesses a contract from prompt text."
        ),
        request_model=ChainInputContractRequest,
        result_model=ChainInputContractResult,
    ),
    HarnessToolSpec(
        name="maestro_input_adaptation_inspect",
        description=(
            "Inspect a native-staged input artifact and target chain contract. "
            "Returns only JSON pointers, sizes, schemas, and digests; never values."
        ),
        request_model=InputAdaptationInspectRequest,
        result_model=InputAdaptationInspectResult,
    ),
    HarnessToolSpec(
        name="maestro_input_adaptation_preview",
        description=(
            "Verify a proposed lossless JSON-Pointer mapping against a pinned "
            "input artifact and exact chain revision. This is a non-executing preview."
        ),
        request_model=InputAdaptationPreviewRequest,
        result_model=InputAdaptationPreviewResult,
    ),
    HarnessToolSpec(
        name="maestro_generate_chain",
        description=(
            "Generate and strictly admit a CARL chain with MAGE, then stage it "
            "as a private artifact. This may call a paid provider and is subject "
            "to DeepSeek Harness one-shot approval. It does not save, run, deploy, "
            "or evolve the chain. A caller-generated idempotency key is required."
        ),
        request_model=ChainGenerationRequest,
        result_model=GenerateChainResult,
    ),
    HarnessToolSpec(
        name="maestro_stage_artifact",
        description=(
            "Privately stage a JSON input or strictly admitted CARL chain and "
            "return only its opaque id, digest, size, and JSON pointers. The "
            "payload is retained locally for a bounded TTL."
        ),
        request_model=StageArtifactRequest,
        result_model=StageArtifactResult,
    ),
    HarnessToolSpec(
        name="maestro_execute_chain",
        description=(
            "Execute an exact digest-pinned CARL chain with an exact staged "
            "input or a confirmed single-use lossless adaptation preview. This "
            "may call providers and requires DeepSeek Harness approval."
        ),
        request_model=ChainExecutionRequest,
        result_model=ExecuteChainResult,
    ),
    HarnessToolSpec(
        name="maestro_memory_write_chain",
        description=(
            "Strictly revalidate and save or version one digest-pinned staged "
            "CARL chain in MAESTRO Memory. This durable write requires DeepSeek "
            "Harness approval and a caller idempotency key."
        ),
        request_model=ChainMemoryWriteRequest,
        result_model=MemoryWriteResult,
    ),
    HarnessToolSpec(
        name="maestro_evolution_start",
        description=(
            "Strictly admit a digest-pinned staged CARL seed and start a "
            "MAESTRO evolution run. This may call providers, requires one-shot "
            "DeepSeek Harness approval, and uses process-scoped idempotency."
        ),
        request_model=EvolutionStartRequest,
        result_model=EvolutionStartResult,
    ),
    HarnessToolSpec(
        name="maestro_evolution_status",
        description=(
            "Read one bounded, value-redacted lifecycle snapshot for a "
            "MAESTRO evolution run. This does not mutate the run."
        ),
        request_model=EvolutionStatusRequest,
        result_model=EvolutionStatusResult,
    ),
    HarnessToolSpec(
        name="maestro_evolution_cancel",
        description=(
            "Request idempotent cancellation of a MAESTRO evolution run. "
            "This state-changing operation requires DeepSeek Harness approval."
        ),
        request_model=EvolutionCancelRequest,
        result_model=EvolutionCancelResult,
    ),
)


class CareMcpAdapter:
    """SDK-independent typed dispatcher used by the MCP transport and tests."""

    def __init__(
        self,
        backend: HarnessMcpBackend | None = None,
        *,
        request_id_factory: Callable[[], str] | None = None,
    ) -> None:
        self.backend = backend or CareReadOnlyMcpBackend()
        self._request_id_factory = request_id_factory or (lambda: str(uuid4()))
        self._specs = {spec.name: spec for spec in _TOOL_SPECS}

    def list_tools(self) -> tuple[HarnessToolSpec, ...]:
        return _TOOL_SPECS

    async def call_tool(self, name: str, arguments: Mapping[str, Any] | None) -> dict[str, Any]:
        spec = self._specs.get(name)
        if spec is None:
            raise UnknownHarnessToolError(f"Unknown MAESTRO MCP tool: {name}")

        raw_arguments = dict(arguments or {})
        _enforce_json_size(raw_arguments, MAX_TOOL_INPUT_BYTES, label="tool input")
        try:
            request = spec.request_model.model_validate(raw_arguments)
        except ValidationError as exc:
            details = "; ".join(
                f"{'.'.join(str(part) for part in item['loc'])}: {item['msg']}"
                for item in exc.errors(include_url=False)[:10]
            )
            raise InvalidHarnessToolArguments(
                redact_sensitive_text(details, max_chars=2_000)
            ) from exc

        request_id = self._request_id_factory()
        cancellation = CancellationToken(reason=f"MCP request {request_id} cancelled")
        try:
            result = await self._dispatch(name, request, request_id, cancellation)
        except asyncio.CancelledError:
            cancellation.cancel()
            raise
        except HarnessMcpError:
            raise
        except Exception as exc:
            # Provider/Memory exceptions can contain credentials or complete
            # request URLs.  Keep details only on the exception chain.
            raise HarnessToolExecutionError(
                f"{name} failed ({type(exc).__name__})"
            ) from exc

        admitted = spec.result_model.model_validate(result)
        payload = _redact_json(admitted.model_dump(mode="json"))
        _enforce_json_size(payload, MAX_TOOL_OUTPUT_BYTES, label="tool output")
        return payload

    async def _dispatch(
        self,
        name: str,
        request: StrictModel,
        request_id: str,
        cancellation: CancellationToken,
    ) -> StrictModel:
        if name == "maestro_capabilities":
            return CapabilitiesResult(
                request_id=request_id,
                capabilities=self._capabilities(),
            )
        if name == "maestro_command_catalog":
            operation = getattr(self.backend, "command_catalog", None)
            if not callable(operation):
                data = CommandCatalogData(
                    status="unavailable",
                    message="The reviewed CARE command surface is unavailable.",
                )
            else:
                data = await operation(cancellation=cancellation)
            return CommandCatalogResult(request_id=request_id, **data.model_dump())
        if name == "maestro_command_review":
            operation = getattr(self.backend, "review_command", None)
            if not callable(operation):
                data = CommandInvocationData(
                    status="unavailable",
                    message="The reviewed CARE command surface is unavailable.",
                )
            else:
                data = await operation(
                    CommandReviewToolRequest.model_validate(request),
                    cancellation=cancellation,
                )
            return CommandInvocationResult(request_id=request_id, **data.model_dump())
        if name == "maestro_command_status":
            operation = getattr(self.backend, "get_command_status", None)
            if not callable(operation):
                data = CommandStatusData(
                    status="unavailable",
                    message="The reviewed CARE command surface is unavailable.",
                )
            else:
                data = await operation(
                    CommandStatusToolRequest.model_validate(request),
                    cancellation=cancellation,
                )
            return CommandStatusResult(request_id=request_id, **data.model_dump())
        if name == "maestro_command_confirm":
            operation = getattr(self.backend, "confirm_command", None)
            if not callable(operation):
                data = CommandInvocationData(
                    status="unavailable",
                    message="The reviewed CARE command surface is unavailable.",
                )
            else:
                data = await operation(
                    CommandConfirmToolRequest.model_validate(request),
                    cancellation=cancellation,
                )
            return CommandInvocationResult(request_id=request_id, **data.model_dump())
        if name == "maestro_validate_chain":
            data = await self.backend.validate_chain(
                ValidateChainRequest.model_validate(request),
                cancellation=cancellation,
            )
            return ValidateChainResult(request_id=request_id, **data.model_dump())
        if name == "maestro_memory_search":
            data = await self.backend.search_memory(
                MemorySearchRequest.model_validate(request),
                cancellation=cancellation,
            )
            return MemorySearchResult(request_id=request_id, **data.model_dump())
        if name == "maestro_memory_get_chain":
            data = await self.backend.get_memory_chain(
                MemoryGetChainRequest.model_validate(request),
                cancellation=cancellation,
            )
            return MemoryGetChainResult(request_id=request_id, **data.model_dump())
        if name == "maestro_chain_input_contract":
            operation = getattr(self.backend, "inspect_chain_input_contract", None)
            if not callable(operation):
                data = ChainInputContractData(
                    status="unavailable",
                    message="Chain contract inspection is unavailable in this MCP backend.",
                )
            else:
                data = await operation(
                    ChainInputContractRequest.model_validate(request),
                    cancellation=cancellation,
                )
            return ChainInputContractResult(request_id=request_id, **data.model_dump())
        if name == "maestro_input_adaptation_inspect":
            operation = getattr(self.backend, "inspect_input_adaptation", None)
            if not callable(operation):
                data = InputAdaptationInspectData(
                    status="unavailable",
                    message="Input adaptation is unavailable in this MCP backend.",
                )
            else:
                data = await operation(
                    InputAdaptationInspectRequest.model_validate(request),
                    cancellation=cancellation,
                )
            return InputAdaptationInspectResult(
                request_id=request_id,
                **data.model_dump(),
            )
        if name == "maestro_input_adaptation_preview":
            operation = getattr(self.backend, "preview_input_adaptation", None)
            if not callable(operation):
                data = InputAdaptationPreviewData(
                    status="unavailable",
                    message="Input adaptation is unavailable in this MCP backend.",
                )
            else:
                data = await operation(
                    InputAdaptationPreviewRequest.model_validate(request),
                    cancellation=cancellation,
                )
            return InputAdaptationPreviewResult(
                request_id=request_id,
                **data.model_dump(),
            )
        if name == "maestro_generate_chain":
            operation = getattr(self.backend, "generate_chain", None)
            if not callable(operation):
                data = GenerateChainData(
                    status="unavailable",
                    message="MAGE generation is unavailable in this MCP backend.",
                )
            else:
                data = await operation(
                    ChainGenerationRequest.model_validate(request),
                    cancellation=cancellation,
                )
            return GenerateChainResult(request_id=request_id, **data.model_dump())
        if name == "maestro_stage_artifact":
            operation = getattr(self.backend, "stage_artifact", None)
            if not callable(operation):
                data = StageArtifactData(
                    status="unavailable",
                    kind=StageArtifactRequest.model_validate(request).kind,
                    message="Private artifact staging is unavailable in this backend.",
                )
            else:
                data = await operation(
                    StageArtifactRequest.model_validate(request),
                    cancellation=cancellation,
                )
            return StageArtifactResult(request_id=request_id, **data.model_dump())
        if name == "maestro_execute_chain":
            operation = getattr(self.backend, "execute_chain", None)
            if not callable(operation):
                data = ExecuteChainData(
                    status="unavailable",
                    message="CARL execution is unavailable in this backend.",
                )
            else:
                data = await operation(
                    ChainExecutionRequest.model_validate(request),
                    cancellation=cancellation,
                )
            return ExecuteChainResult(request_id=request_id, **data.model_dump())
        if name == "maestro_memory_write_chain":
            operation = getattr(self.backend, "write_chain_to_memory", None)
            if not callable(operation):
                data = MemoryWriteData(
                    status="unavailable",
                    message="Memory writes are unavailable in this backend.",
                )
            else:
                data = await operation(
                    ChainMemoryWriteRequest.model_validate(request),
                    cancellation=cancellation,
                )
            return MemoryWriteResult(request_id=request_id, **data.model_dump())
        if name == "maestro_evolution_start":
            operation = getattr(self.backend, "start_evolution", None)
            if not callable(operation):
                data = EvolutionStartData(
                    status="unavailable",
                    message="Evolution is unavailable in this backend.",
                )
            else:
                data = await operation(
                    EvolutionStartRequest.model_validate(request),
                    cancellation=cancellation,
                )
            return EvolutionStartResult(request_id=request_id, **data.model_dump())
        if name == "maestro_evolution_status":
            operation = getattr(self.backend, "get_evolution_status", None)
            if not callable(operation):
                data = EvolutionStatusData(
                    status="unavailable",
                    message="Evolution is unavailable in this backend.",
                )
            else:
                data = await operation(
                    EvolutionStatusRequest.model_validate(request),
                    cancellation=cancellation,
                )
            return EvolutionStatusResult(request_id=request_id, **data.model_dump())
        if name == "maestro_evolution_cancel":
            operation = getattr(self.backend, "cancel_evolution", None)
            if not callable(operation):
                data = EvolutionCancelData(
                    status="unavailable",
                    message="Evolution is unavailable in this backend.",
                )
            else:
                data = await operation(
                    EvolutionCancelRequest.model_validate(request),
                    cancellation=cancellation,
                )
            return EvolutionCancelResult(request_id=request_id, **data.model_dump())
        raise UnknownHarnessToolError(f"Unknown MAESTRO MCP tool: {name}")

    def _capabilities(self) -> list[CapabilityInfo]:
        memory_state: Literal["available", "unavailable"] = (
            "available" if self.backend.memory_available else "unavailable"
        )
        orchestration_available = bool(
            getattr(self.backend, "orchestration_available", False)
        )
        orchestration_state: Literal["available", "unavailable"] = (
            "available" if orchestration_available else "unavailable"
        )
        generation_available = bool(
            getattr(self.backend, "generation_available", False)
        )
        staging_available = bool(
            getattr(self.backend, "artifact_staging_available", False)
        )
        execution_available = bool(
            getattr(self.backend, "execution_available", False)
        )
        memory_write_available = bool(
            getattr(self.backend, "memory_write_available", False)
        )
        evolution_available = bool(
            getattr(self.backend, "evolution_available", False)
        )
        command_surface_available = bool(
            getattr(self.backend, "command_surface_available", False)
        )
        return [
            CapabilityInfo(
                name="validate_chain",
                availability="available",
                mutates_state=False,
                confirmation_required=False,
                cancellation="cooperative",
            ),
            CapabilityInfo(
                name="command_catalog_and_review",
                availability=(
                    "available" if command_surface_available else "unavailable"
                ),
                mutates_state=False,
                confirmation_required=False,
                cancellation="best_effort",
                note=(
                    "Review executes only commands classified as read-only; "
                    "effectful commands return a state-bound challenge."
                    if command_surface_available
                    else "Configure the reviewed CARE CommandBridge surface."
                ),
            ),
            CapabilityInfo(
                name="command_confirm",
                availability=(
                    "approval_gated" if command_surface_available else "unavailable"
                ),
                mutates_state=True,
                confirmation_required=True,
                cancellation="best_effort",
                note=(
                    "Synchronous handlers cannot be forcibly stopped after "
                    "dispatch; reconcile a cancelled call with command_status "
                    "before retrying."
                    if command_surface_available
                    else None
                ),
            ),
            CapabilityInfo(
                name="command_status",
                availability=(
                    "available" if command_surface_available else "unavailable"
                ),
                mutates_state=False,
                confirmation_required=False,
                cancellation="not_applicable",
                note=(
                    "Returns retained command state/results after an ambiguous "
                    "transport cancellation."
                    if command_surface_available
                    else None
                ),
            ),
            CapabilityInfo(
                name="memory_read",
                availability=memory_state,
                mutates_state=False,
                confirmation_required=False,
                cancellation="best_effort",
                note=(
                    None
                    if self.backend.memory_available
                    else "Inject a configured CareMemory into CareReadOnlyMcpBackend."
                ),
            ),
            CapabilityInfo(
                name="inspect_input_contract",
                availability=orchestration_state,
                mutates_state=False,
                confirmation_required=False,
                cancellation="best_effort",
                note=(
                    None
                    if orchestration_available
                    else "Configure the session-scoped CARE orchestration service."
                ),
            ),
            CapabilityInfo(
                name="preview_input_adaptation",
                availability=orchestration_state,
                mutates_state=False,
                confirmation_required=False,
                cancellation="best_effort",
                note=(
                    None
                    if orchestration_available
                    else "Input artifacts must be staged by the trusted native host."
                ),
            ),
            CapabilityInfo(
                name="stage_artifact",
                availability="approval_gated" if staging_available else "unavailable",
                mutates_state=True,
                confirmation_required=True,
                cancellation="best_effort",
            ),
            CapabilityInfo(
                name="generate_chain",
                availability="approval_gated" if generation_available else "unavailable",
                mutates_state=True,
                confirmation_required=True,
                cancellation="cooperative",
            ),
            CapabilityInfo(
                name="execute_chain",
                availability="approval_gated" if execution_available else "unavailable",
                mutates_state=True,
                confirmation_required=True,
                cancellation="cooperative",
            ),
            CapabilityInfo(
                name="memory_write",
                availability="approval_gated" if memory_write_available else "unavailable",
                mutates_state=True,
                confirmation_required=True,
                cancellation="best_effort",
            ),
            CapabilityInfo(
                name="deploy_chain",
                availability="unavailable",
                mutates_state=True,
                confirmation_required=True,
                cancellation="cooperative",
                note=(
                    "No typed deployment tool is implemented. The separately "
                    "reviewed /deploy command path is available through the "
                    "command bridge."
                    if command_surface_available
                    else "A typed deployment/audit operation is not implemented yet."
                ),
            ),
            CapabilityInfo(
                name="evolution_status",
                availability="available" if evolution_available else "unavailable",
                mutates_state=False,
                confirmation_required=False,
                cancellation="best_effort",
            ),
            CapabilityInfo(
                name="evolve_chain",
                availability=(
                    "approval_gated" if evolution_available else "unavailable"
                ),
                mutates_state=True,
                confirmation_required=True,
                cancellation="best_effort",
                note=(
                    "Status polling and cancellable DeepSeek Harness background "
                    "jobs are available."
                    if evolution_available
                    else "Configure the typed CARE Platform evolution operation."
                ),
            ),
            CapabilityInfo(
                name="cancel_evolution",
                availability=(
                    "approval_gated" if evolution_available else "unavailable"
                ),
                mutates_state=True,
                confirmation_required=True,
                cancellation="best_effort",
            ),
        ]


async def _run_sync_cancellable(
    operation: Callable[[], ResultModelT],
    cancellation: CancellationToken,
) -> ResultModelT:
    """Run a synchronous CARE seam without blocking the MCP event loop."""

    cancellation.raise_if_cancelled()
    try:
        result = await asyncio.to_thread(operation)
    except asyncio.CancelledError:
        cancellation.cancel()
        raise
    cancellation.raise_if_cancelled()
    return result


def _enforce_json_size(value: Any, maximum: int, *, label: str) -> None:
    try:
        encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError) as exc:
        raise InvalidHarnessToolArguments(f"{label} is not JSON serializable") from exc
    size = len(encoded.encode("utf-8"))
    if size > maximum:
        raise InvalidHarnessToolArguments(
            f"{label} exceeds the {maximum}-byte MCP boundary"
        )


def _redact_json(value: Any, *, field_name: str | None = None) -> Any:
    """Redact credential-shaped fields before data crosses into an MCP host."""

    if field_name is not None and _SECRET_FIELD.search(field_name):
        return "[redacted]"
    if isinstance(value, dict):
        return {str(key): _redact_json(item, field_name=str(key)) for key, item in value.items()}
    if isinstance(value, list):
        return [_redact_json(item) for item in value]
    if isinstance(value, str):
        return redact_sensitive_text(value)
    return value


_DEEPSEEK_SCHEMA_KEYS = frozenset(
    {
        "type",
        "oneOf",
        "properties",
        "required",
        "additionalProperties",
        "items",
        "enum",
        "const",
        "description",
        "title",
        "default",
        "examples",
    }
)


def _deepseek_compatible_schema(schema: Mapping[str, Any]) -> dict[str, Any]:
    """Project Draft 2020-12 Pydantic schemas onto DeepSeek's enforced subset.

    DeepSeek Harness' tool runtime intentionally rejects schemas containing
    unenforced keywords.  In particular it accepts ``oneOf`` but not
    Pydantic's ``anyOf`` spelling, local ``$ref``/``$defs``, or scalar bounds
    such as ``minLength``/``pattern``/``minimum``.  Runtime admission still
    uses the original Pydantic models, so dropping those hints does not weaken
    the CARE boundary; invalid calls return a typed MCP tool error.
    """

    definitions = schema.get("$defs")
    defs = definitions if isinstance(definitions, Mapping) else {}

    def visit(candidate: Any, resolving: frozenset[str] = frozenset()) -> dict[str, Any]:
        if not isinstance(candidate, Mapping):
            return {}

        ref = candidate.get("$ref")
        if isinstance(ref, str) and ref.startswith("#/$defs/"):
            name = ref.removeprefix("#/$defs/")
            target = defs.get(name)
            if name in resolving or not isinstance(target, Mapping):
                return {}
            resolved = visit(target, resolving | {name})
            # A local reference may carry sibling annotations.  Preserve only
            # the subset the Harness validator understands.
            for key in ("description", "title", "default", "examples"):
                if key in candidate:
                    resolved[key] = candidate[key]
            return resolved

        projected: dict[str, Any] = {}
        schema_type = candidate.get("type")
        if isinstance(schema_type, str):
            projected["type"] = schema_type

        raw_union = candidate.get("oneOf", candidate.get("anyOf"))
        if isinstance(raw_union, list):
            projected.pop("type", None)
            projected["oneOf"] = [visit(branch, resolving) for branch in raw_union]

        raw_properties = candidate.get("properties")
        if isinstance(raw_properties, Mapping):
            projected["properties"] = {
                str(key): visit(value, resolving)
                for key, value in raw_properties.items()
            }
            required = candidate.get("required")
            if isinstance(required, list):
                projected["required"] = [
                    str(item)
                    for item in required
                    if isinstance(item, str) and item in raw_properties
                ]

        additional = candidate.get("additionalProperties")
        if isinstance(additional, bool):
            projected["additionalProperties"] = additional
        elif isinstance(additional, Mapping):
            # DeepSeek's current subset cannot express typed maps.  Advertise
            # an open JSON object and retain exact Pydantic validation in CARE.
            projected["additionalProperties"] = True

        if "items" in candidate:
            projected["items"] = visit(candidate["items"], resolving)

        for key in ("enum", "const", "description", "title", "default", "examples"):
            if key in candidate:
                projected[key] = candidate[key]

        # Keep this assertion local so future edits cannot accidentally start
        # advertising a keyword DeepSeek accepts into its registry but does
        # not enforce at execution time.
        assert set(projected) <= _DEEPSEEK_SCHEMA_KEYS
        return projected

    return visit(schema)


def build_mcp_server(adapter: CareMcpAdapter | None = None) -> Any:
    """Build an official MCP Python 1.x low-level server lazily.

    The low-level API lets CARE publish exact Pydantic JSON Schemas with
    ``additionalProperties: false``.  It also returns canonical
    ``structuredContent`` plus a JSON text block, which is the shape consumed
    by DeepSeek Harness' MCP bridge.
    """

    try:
        import mcp.types as types
        from mcp.server.lowlevel import Server
    except ImportError as exc:  # pragma: no cover - environment-dependent
        raise McpSdkUnavailableError(
            "MAESTRO MCP requires official mcp>=1.28,<2; reinstall MAESTRO CARE."
        ) from exc

    domain = adapter or CareMcpAdapter()
    server = Server(
        SERVER_NAME,
        version=SERVER_VERSION,
        instructions=(
            "Typed MAESTRO CARE bridge. Read-only inspection is directly "
            "available; generation, staging, execution, Memory writes, evolution, "
            "and confirmed commands remain approval-gated by the native host. "
            "Typed deployment is not currently published."
        ),
    )

    @server.list_tools()
    async def list_tools() -> list[Any]:
        return [
            types.Tool(
                name=spec.name,
                description=spec.description,
                inputSchema=spec.input_schema,
                outputSchema=spec.output_schema,
            )
            for spec in domain.list_tools()
        ]

    @server.call_tool()
    async def call_tool(name: str, arguments: dict[str, Any]) -> Any:
        try:
            return await domain.call_tool(name, arguments)
        except asyncio.CancelledError:
            raise
        except HarnessMcpError as exc:
            return types.CallToolResult(
                content=[
                    types.TextContent(
                        type="text",
                        text=redact_sensitive_text(exc, max_chars=2_000),
                    )
                ],
                isError=True,
            )

    return server


async def run_stdio(adapter: CareMcpAdapter | None = None) -> None:
    """Serve one lifecycle-owned MCP connection over stdin/stdout."""

    try:
        from mcp.server.stdio import stdio_server
    except ImportError as exc:  # pragma: no cover - environment-dependent
        raise McpSdkUnavailableError(
            "MAESTRO MCP requires official mcp>=1.28,<2; reinstall MAESTRO CARE."
        ) from exc

    server = build_mcp_server(adapter)
    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            server.create_initialization_options(),
        )


def main() -> int:
    """Development entry point: ``python -m care.harness_mcp``.

    A plain source invocation remains read-only and unconfigured.  The native
    MAESTRO host opts into the production context with explicit session and
    artifact-root environment coordinates; that path opens the shared artifact
    store and, when enabled, constructs CARE Memory without performing network
    I/O during startup.
    """

    adapter: CareMcpAdapter | None = None
    try:
        from care.harness_mcp_runtime import (
            ARTIFACT_ROOT_ENV,
            SESSION_ID_ENV,
            HarnessMcpRuntimeContextError,
            build_harness_mcp_runtime_context,
        )

        runtime_requested = any(
            name in os.environ for name in (SESSION_ID_ENV, ARTIFACT_ROOT_ENV)
        )
        if runtime_requested:
            # This process is spawned invisibly by Harness and may be restarted
            # after a transport failure.  It must never surface one macOS ACL
            # dialog per configured secret (and then repeat the sequence on
            # reconnect).  Already-authorized Keychain reads still succeed;
            # reads that need UI fail closed as an unavailable credential.
            from care.runtime.keystore import (
                KeystoreError,
                disable_macos_keychain_interaction,
            )

            try:
                disable_macos_keychain_interaction()
            except KeystoreError:
                print(
                    "MAESTRO MCP startup failed "
                    "[keychain_interaction_guard_failed]: could not disable "
                    "background Keychain dialogs.",
                    file=sys.stderr,
                )
                return 2
            from care.runtime.credential_broker import (
                BROKER_BOOTSTRAP_FD_ENV,
                HarnessCredentialBrokerError,
                peek_harness_credential_bootstrap,
            )

            try:
                credential_broker = peek_harness_credential_bootstrap()
            except HarnessCredentialBrokerError:
                print(
                    "MAESTRO MCP startup failed "
                    "[credential_broker_bootstrap_failed]: private credential "
                    "handoff was invalid.",
                    file=sys.stderr,
                )
                return 2
            if (
                (os.environ.get(BROKER_BOOTSTRAP_FD_ENV)
                 or os.environ.get("MAESTRO_HARNESS_CREDENTIAL_BROKER_PORT"))
                and credential_broker is None
            ):
                print(
                    "MAESTRO MCP startup failed "
                    "[credential_broker_bootstrap_failed]: private credential "
                    "handoff was unavailable.",
                    file=sys.stderr,
                )
                return 2
            context = (
                build_harness_mcp_runtime_context()
                if credential_broker is None
                else build_harness_mcp_runtime_context(
                    credential_broker=credential_broker,
                )
            )
            adapter = CareMcpAdapter(context.backend)
        asyncio.run(run_stdio(adapter))
    except HarnessMcpRuntimeContextError as exc:
        # Stable/type-only runtime diagnostics are safe on stderr; stdout is
        # reserved for MCP framing. The builder never copies provider errors.
        print(f"MAESTRO MCP startup failed [{exc.code}]: {exc}", file=sys.stderr)
        return 2
    except McpSdkUnavailableError as exc:
        # stderr is safe; stdout belongs exclusively to the MCP wire.
        print(str(exc), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised by process smoke tests
    raise SystemExit(main())


__all__ = [
    "ChainInputContractData",
    "ChainInputContractRequest",
    "ChainInputContractResult",
    "CapabilitiesResult",
    "CareMcpAdapter",
    "CareReadOnlyMcpBackend",
    "HarnessMcpBackend",
    "HarnessMcpError",
    "HarnessToolExecutionError",
    "HarnessToolSpec",
    "GenerateChainData",
    "GenerateChainResult",
    "ExecuteChainData",
    "ExecuteChainResult",
    "EvolutionCancelData",
    "EvolutionCancelResult",
    "EvolutionStartData",
    "EvolutionStartResult",
    "EvolutionStatusData",
    "EvolutionStatusResult",
    "InputAdaptationInspectData",
    "InputAdaptationInspectRequest",
    "InputAdaptationInspectResult",
    "InputAdaptationPreviewData",
    "InputAdaptationPreviewRequest",
    "InputAdaptationPreviewResult",
    "InvalidHarnessToolArguments",
    "McpSdkUnavailableError",
    "MemoryGetChainData",
    "MemoryGetChainRequest",
    "MemoryGetChainResult",
    "MemorySearchData",
    "MemorySearchRequest",
    "MemorySearchResult",
    "MemoryWriteData",
    "MemoryWriteResult",
    "StageArtifactData",
    "StageArtifactRequest",
    "StageArtifactResult",
    "UnknownHarnessToolError",
    "ValidateChainData",
    "ValidateChainRequest",
    "ValidateChainResult",
    "build_mcp_server",
    "run_stdio",
]
