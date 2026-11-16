"""Provider-free MCP coverage for typed MAESTRO evolution tools."""

from __future__ import annotations

from typing import Any

import pytest

from care.harness_evolution import (
    EvolutionCancelReceipt,
    EvolutionCancelRequest,
    EvolutionStartReceipt,
    EvolutionStartRequest,
    EvolutionStatusReceipt,
    EvolutionStatusRequest,
)
from care.harness_mcp import (
    CareMcpAdapter,
    CareReadOnlyMcpBackend,
    InvalidHarnessToolArguments,
)
from care.runtime.cancellation import CancellationToken


CHAIN_DIGEST = "a" * 64
START_ARGUMENTS = {
    "idempotency_key": "evolution-start-0001",
    "base_chain_id": "chain-1",
    "chain": {
        "artifact_id": "seed-artifact",
        "content_sha256": CHAIN_DIGEST,
    },
}
STATUS_ARGUMENTS = {"evolution_id": "evo-1"}
CANCEL_ARGUMENTS = {
    "idempotency_key": "evolution-cancel-0001",
    "evolution_id": "evo-1",
}


def _contains_key(value: Any, key: str) -> bool:
    if isinstance(value, dict):
        return key in value or any(_contains_key(item, key) for item in value.values())
    if isinstance(value, list):
        return any(_contains_key(item, key) for item in value)
    return False


class RecordingEvolution:
    def __init__(self) -> None:
        self.calls: list[tuple[str, object, CancellationToken]] = []

    async def start(
        self,
        request: EvolutionStartRequest,
        *,
        cancellation: CancellationToken,
    ) -> EvolutionStartReceipt:
        self.calls.append(("start", request, cancellation))
        return EvolutionStartReceipt(
            status="started",
            idempotency_key=request.idempotency_key,
            request_fingerprint="b" * 64,
            evolution_id="evo-1",
            base_chain_id=request.base_chain_id,
            chain_digest=request.chain.content_sha256,
            evolution_status="queued",
        )

    async def status(
        self,
        request: EvolutionStatusRequest,
        *,
        cancellation: CancellationToken,
    ) -> EvolutionStatusReceipt:
        self.calls.append(("status", request, cancellation))
        return EvolutionStatusReceipt(
            evolution_id=request.evolution_id,
            status="running",
            terminal=False,
            generation=2,
            max_iterations=5,
            best_fitness=0.75,
        )

    async def cancel(
        self,
        request: EvolutionCancelRequest,
        *,
        cancellation: CancellationToken,
    ) -> EvolutionCancelReceipt:
        self.calls.append(("cancel", request, cancellation))
        return EvolutionCancelReceipt(
            idempotency_key=request.idempotency_key,
            request_fingerprint="c" * 64,
            evolution_id=request.evolution_id,
            evolution_status="stopping",
        )


def test_evolution_schemas_are_strict_and_have_no_model_approval_field() -> None:
    specs = {
        spec.name: spec
        for spec in CareMcpAdapter().list_tools()
        if spec.name.startswith("maestro_evolution_")
    }

    assert set(specs) == {
        "maestro_evolution_cancel",
        "maestro_evolution_start",
        "maestro_evolution_status",
    }
    for spec in specs.values():
        assert spec.input_schema["additionalProperties"] is False
        assert spec.output_schema["additionalProperties"] is False
        assert not _contains_key(spec.input_schema, "approved")


@pytest.mark.asyncio
async def test_evolution_tools_dispatch_typed_requests_and_receipts() -> None:
    evolution = RecordingEvolution()
    adapter = CareMcpAdapter(
        CareReadOnlyMcpBackend(evolution=evolution),  # type: ignore[arg-type]
        request_id_factory=lambda: "evolution-request-1",
    )

    started = await adapter.call_tool("maestro_evolution_start", START_ARGUMENTS)
    status = await adapter.call_tool("maestro_evolution_status", STATUS_ARGUMENTS)
    cancelled = await adapter.call_tool(
        "maestro_evolution_cancel",
        CANCEL_ARGUMENTS,
    )
    capabilities = await adapter.call_tool("maestro_capabilities", {})

    assert started["receipt"]["evolution_id"] == "evo-1"
    assert status["receipt"]["status"] == "running"
    assert status["receipt"]["best_fitness"] == pytest.approx(0.75)
    assert cancelled["receipt"]["evolution_status"] == "stopping"
    assert [item[0] for item in evolution.calls] == ["start", "status", "cancel"]
    assert all(item[2].is_cancelled is False for item in evolution.calls)
    by_name = {item["name"]: item for item in capabilities["capabilities"]}
    assert by_name["evolution_status"]["availability"] == "available"
    assert by_name["evolve_chain"]["availability"] == "approval_gated"
    assert by_name["cancel_evolution"]["availability"] == "approval_gated"


@pytest.mark.asyncio
async def test_evolution_tools_are_unavailable_without_platform_operation() -> None:
    adapter = CareMcpAdapter(CareReadOnlyMcpBackend())

    started = await adapter.call_tool("maestro_evolution_start", START_ARGUMENTS)
    status = await adapter.call_tool("maestro_evolution_status", STATUS_ARGUMENTS)
    cancelled = await adapter.call_tool(
        "maestro_evolution_cancel",
        CANCEL_ARGUMENTS,
    )

    assert started["status"] == "unavailable"
    assert status["status"] == "unavailable"
    assert cancelled["status"] == "unavailable"


@pytest.mark.asyncio
async def test_evolution_effects_reject_model_supplied_approval() -> None:
    evolution = RecordingEvolution()
    adapter = CareMcpAdapter(
        CareReadOnlyMcpBackend(evolution=evolution),  # type: ignore[arg-type]
    )

    for tool, arguments in (
        ("maestro_evolution_start", START_ARGUMENTS),
        ("maestro_evolution_cancel", CANCEL_ARGUMENTS),
    ):
        with pytest.raises(InvalidHarnessToolArguments, match="approved"):
            await adapter.call_tool(tool, {**arguments, "approved": True})

    assert evolution.calls == []
