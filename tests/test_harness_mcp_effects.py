"""Provider-free contract tests for effectful MAESTRO MCP tools."""

from __future__ import annotations

from typing import Any

import pytest

from care.harness_artifacts import JsonArtifactReceipt
from care.harness_execution import (
    ChainExecutionReceipt,
    ChainExecutionRequest,
    ExecutionOutputArtifact,
)
from care.harness_mcp import (
    CareMcpAdapter,
    CareReadOnlyMcpBackend,
    HarnessToolExecutionError,
    InvalidHarnessToolArguments,
)
from care.harness_memory_write import (
    ChainMemoryWriteReceipt,
    ChainMemoryWriteRequest,
)
from care.runtime.cancellation import CancellationToken


CHAIN_DIGEST = "a" * 64
INPUT_DIGEST = "b" * 64
OUTPUT_DIGEST = "c" * 64

STAGE_ARGUMENTS = {
    "kind": "input",
    "payload": {"name": "Ada"},
}
EXECUTE_ARGUMENTS = {
    "idempotency_key": "execute-0001",
    "chain_id": "chain-1",
    "task": "Run the exact chain",
    "chain": {
        "artifact_id": "chain-artifact",
        "content_sha256": CHAIN_DIGEST,
    },
    "input": {
        "kind": "artifact",
        "artifact_id": "input-artifact",
        "content_sha256": INPUT_DIGEST,
    },
}
MEMORY_WRITE_ARGUMENTS = {
    "idempotency_key": "memory-write-0001",
    "artifact_id": "chain-artifact",
    "content_sha256": CHAIN_DIGEST,
    "name": "Generated chain",
    "tags": ["generated"],
}


def _effect_specs() -> dict[str, Any]:
    selected = {
        "maestro_stage_artifact",
        "maestro_execute_chain",
        "maestro_memory_write_chain",
    }
    return {
        spec.name: spec
        for spec in CareMcpAdapter().list_tools()
        if spec.name in selected
    }


def _contains_key(value: Any, key: str) -> bool:
    if isinstance(value, dict):
        return key in value or any(_contains_key(item, key) for item in value.values())
    if isinstance(value, list):
        return any(_contains_key(item, key) for item in value)
    return False


def test_effectful_tool_schemas_are_strict_and_expose_no_approval_field() -> None:
    specs = _effect_specs()

    assert set(specs) == {
        "maestro_stage_artifact",
        "maestro_execute_chain",
        "maestro_memory_write_chain",
    }
    for spec in specs.values():
        assert spec.input_schema["type"] == "object"
        assert spec.input_schema["additionalProperties"] is False
        assert spec.output_schema["type"] == "object"
        assert spec.output_schema["additionalProperties"] is False
        assert not _contains_key(spec.input_schema, "approved")

    execute = specs["maestro_execute_chain"].input_schema
    assert execute["properties"]["chain"]["additionalProperties"] is False
    assert {
        branch["properties"]["kind"]["const"]
        for branch in execute["properties"]["input"]["oneOf"]
    } == {"artifact", "adaptation_preview"}
    assert all(
        branch["additionalProperties"] is False
        for branch in execute["properties"]["input"]["oneOf"]
    )


class RecordingStager:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def stage(self, payload: dict[str, Any]) -> JsonArtifactReceipt:
        self.calls.append(payload)
        return JsonArtifactReceipt(
            artifact_id="staged-artifact",
            content_sha256=OUTPUT_DIGEST,
            size_bytes=14,
            leaf_pointers=("/name",),
            expires_at_unix=2_000_000_000.0,
        )


class RecordingExecution:
    def __init__(self) -> None:
        self.calls: list[tuple[ChainExecutionRequest, CancellationToken]] = []

    async def execute(
        self,
        request: ChainExecutionRequest,
        *,
        cancellation: CancellationToken,
    ) -> ChainExecutionReceipt:
        self.calls.append((request, cancellation))
        return ChainExecutionReceipt(
            idempotency_key=request.idempotency_key,
            request_fingerprint="d" * 64,
            chain_digest=request.chain.content_sha256,
            input_digest=INPUT_DIGEST,
            success=True,
            final_output="completed through fake executor",
            step_count=2,
            output_artifact=ExecutionOutputArtifact(
                artifact_id="execution-output",
                content_sha256=OUTPUT_DIGEST,
                size_bytes=42,
                expires_at_unix=2_000_000_000.0,
                session_id="session-1",
            ),
        )


class RecordingMemoryWrite:
    def __init__(self) -> None:
        self.calls: list[ChainMemoryWriteRequest] = []

    async def save(
        self,
        request: ChainMemoryWriteRequest,
    ) -> ChainMemoryWriteReceipt:
        self.calls.append(request)
        return ChainMemoryWriteReceipt(
            operation="created",
            idempotency_key=request.idempotency_key,
            request_fingerprint="e" * 64,
            entity_id="memory-chain-1",
            channel=request.channel,
            artifact_id=request.artifact_id,
            content_sha256=request.content_sha256,
        )


@pytest.mark.asyncio
async def test_effectful_tools_dispatch_typed_requests_and_return_typed_receipts() -> None:
    stager = RecordingStager()
    execution = RecordingExecution()
    memory_write = RecordingMemoryWrite()
    adapter = CareMcpAdapter(
        CareReadOnlyMcpBackend(
            artifact_stager=stager,
            execution=execution,  # type: ignore[arg-type]
            memory_write=memory_write,  # type: ignore[arg-type]
        ),
        request_id_factory=lambda: "effect-request-1",
    )

    staged = await adapter.call_tool("maestro_stage_artifact", STAGE_ARGUMENTS)
    executed = await adapter.call_tool("maestro_execute_chain", EXECUTE_ARGUMENTS)
    written = await adapter.call_tool(
        "maestro_memory_write_chain",
        MEMORY_WRITE_ARGUMENTS,
    )

    assert staged == {
        "status": "completed",
        "kind": "input",
        "receipt": {
            "artifact_id": "staged-artifact",
            "content_sha256": OUTPUT_DIGEST,
            "size_bytes": 14,
            "leaf_pointers": ["/name"],
            "expires_at_unix": 2_000_000_000.0,
        },
        "message": None,
        "request_id": "effect-request-1",
    }
    assert executed["status"] == "completed"
    assert executed["receipt"]["status"] == "completed"
    assert executed["receipt"]["chain_digest"] == CHAIN_DIGEST
    assert executed["receipt"]["output_artifact"]["artifact_id"] == (
        "execution-output"
    )
    assert written["status"] == "completed"
    assert written["receipt"]["status"] == "saved"
    assert written["receipt"]["operation"] == "created"
    assert written["receipt"]["entity_id"] == "memory-chain-1"

    assert stager.calls == [{"name": "Ada"}]
    assert len(execution.calls) == 1
    assert isinstance(execution.calls[0][0], ChainExecutionRequest)
    assert execution.calls[0][1].is_cancelled is False
    assert len(memory_write.calls) == 1
    assert isinstance(memory_write.calls[0], ChainMemoryWriteRequest)


@pytest.mark.asyncio
async def test_effectful_tools_reject_model_supplied_approval_before_dispatch() -> None:
    stager = RecordingStager()
    execution = RecordingExecution()
    memory_write = RecordingMemoryWrite()
    adapter = CareMcpAdapter(
        CareReadOnlyMcpBackend(
            artifact_stager=stager,
            execution=execution,  # type: ignore[arg-type]
            memory_write=memory_write,  # type: ignore[arg-type]
        )
    )

    for tool, arguments in (
        ("maestro_stage_artifact", STAGE_ARGUMENTS),
        ("maestro_execute_chain", EXECUTE_ARGUMENTS),
        ("maestro_memory_write_chain", MEMORY_WRITE_ARGUMENTS),
    ):
        with pytest.raises(InvalidHarnessToolArguments, match="approved"):
            await adapter.call_tool(tool, {**arguments, "approved": True})

    assert stager.calls == []
    assert execution.calls == []
    assert memory_write.calls == []


@pytest.mark.asyncio
async def test_effectful_tools_report_unavailable_without_configured_operations() -> None:
    adapter = CareMcpAdapter(
        CareReadOnlyMcpBackend(),
        request_id_factory=lambda: "unavailable-request",
    )

    staged = await adapter.call_tool("maestro_stage_artifact", STAGE_ARGUMENTS)
    executed = await adapter.call_tool("maestro_execute_chain", EXECUTE_ARGUMENTS)
    written = await adapter.call_tool(
        "maestro_memory_write_chain",
        MEMORY_WRITE_ARGUMENTS,
    )

    assert staged["status"] == "unavailable"
    assert staged["kind"] == "input"
    assert staged["receipt"] is None
    assert executed["status"] == "unavailable"
    assert executed["receipt"] is None
    assert written["status"] == "unavailable"
    assert written["receipt"] is None


@pytest.mark.asyncio
async def test_chain_staging_fails_strict_admission_before_artifact_write() -> None:
    stager = RecordingStager()
    adapter = CareMcpAdapter(CareReadOnlyMcpBackend(artifact_stager=stager))

    with pytest.raises(HarnessToolExecutionError) as caught:
        await adapter.call_tool(
            "maestro_stage_artifact",
            {
                "kind": "chain",
                "payload": {"steps": "not-a-list"},
            },
        )

    assert str(caught.value) == "maestro_stage_artifact failed (ValueError)"
    assert stager.calls == []
