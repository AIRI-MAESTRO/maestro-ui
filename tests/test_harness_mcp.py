from __future__ import annotations

import asyncio
from typing import Any

import pytest

from care.harness_mcp import (
    CareMcpAdapter,
    CareReadOnlyMcpBackend,
    HarnessToolExecutionError,
    InvalidHarnessToolArguments,
    MemoryGetChainData,
    MemoryGetChainRequest,
    MemorySearchData,
    MemorySearchRequest,
    UnknownHarnessToolError,
    ValidateChainData,
    ValidateChainRequest,
)
from care.harness_artifacts import JsonArtifactStore
from care.harness_contracts import AdaptationPreviewStore
from care.harness_generation import ChainGenerationReceipt
from care.harness_service import HarnessOrchestrationService
from care.runtime.cancellation import CancellationToken


class FakeBackend:
    def __init__(self) -> None:
        self.memory_available = True
        self.last_token: CancellationToken | None = None

    async def validate_chain(
        self,
        request: ValidateChainRequest,
        *,
        cancellation: CancellationToken,
    ) -> ValidateChainData:
        self.last_token = cancellation
        return ValidateChainData(
            parsed=True,
            ok=True,
            required_tools=["web_search"],
            preflight_available=True,
        )

    async def search_memory(
        self,
        request: MemorySearchRequest,
        *,
        cancellation: CancellationToken,
    ) -> MemorySearchData:
        self.last_token = cancellation
        return MemorySearchData(
            status="completed",
            hits=[
                {
                    "entity_id": "chain-1",
                    "score": 0.9,
                    "api_key": "sk-should-not-cross-mcp",
                    "provider_api_key": "another-secret",
                }
            ],
        )

    async def get_memory_chain(
        self,
        request: MemoryGetChainRequest,
        *,
        cancellation: CancellationToken,
    ) -> MemoryGetChainData:
        self.last_token = cancellation
        return MemoryGetChainData(
            status="completed",
            entity_id=request.entity_id,
            channel=request.channel,
            chain={"version": "1.0", "steps": []},
        )


def _adapter(backend: Any | None = None) -> CareMcpAdapter:
    return CareMcpAdapter(
        backend=backend or FakeBackend(),
        request_id_factory=lambda: "request-1",
    )


def test_tool_schemas_are_strict_and_publish_structured_outputs() -> None:
    specs = _adapter().list_tools()

    assert [spec.name for spec in specs] == [
        "maestro_capabilities",
        "maestro_command_catalog",
        "maestro_command_review",
        "maestro_command_status",
        "maestro_command_confirm",
        "maestro_validate_chain",
        "maestro_memory_search",
        "maestro_memory_get_chain",
        "maestro_chain_input_contract",
        "maestro_input_adaptation_inspect",
        "maestro_input_adaptation_preview",
        "maestro_generate_chain",
        "maestro_stage_artifact",
        "maestro_execute_chain",
        "maestro_memory_write_chain",
        "maestro_evolution_start",
        "maestro_evolution_status",
        "maestro_evolution_cancel",
    ]
    for spec in specs:
        assert spec.input_schema["type"] == "object"
        assert spec.input_schema["additionalProperties"] is False
        assert spec.output_schema["type"] == "object"
        assert spec.output_schema["additionalProperties"] is False


def test_advertised_schemas_match_deepseek_harness_enforced_subset() -> None:
    allowed = {
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

    def inspect_node(node: dict[str, Any]) -> None:
        assert set(node) <= allowed
        for child in node.get("properties", {}).values():
            inspect_node(child)
        if isinstance(node.get("items"), dict):
            inspect_node(node["items"])
        for branch in node.get("oneOf", []):
            inspect_node(branch)

    for spec in _adapter().list_tools():
        inspect_node(spec.input_schema)
        inspect_node(spec.output_schema)

    search_schema = next(
        spec.input_schema
        for spec in _adapter().list_tools()
        if spec.name == "maestro_memory_search"
    )
    assert "minLength" not in search_schema["properties"]["query"]
    assert "$defs" not in str(
        next(
            spec.output_schema
            for spec in _adapter().list_tools()
            if spec.name == "maestro_capabilities"
        )
    )


@pytest.mark.asyncio
async def test_capability_report_is_honest_about_gated_actions() -> None:
    result = await _adapter().call_tool("maestro_capabilities", {})

    by_name = {item["name"]: item for item in result["capabilities"]}
    assert result["protocol_version"] == "2025-11-25"
    assert by_name["validate_chain"]["availability"] == "available"
    assert by_name["memory_read"]["availability"] == "available"
    assert by_name["execute_chain"]["availability"] == "unavailable"
    assert by_name["execute_chain"]["confirmation_required"] is True


@pytest.mark.asyncio
async def test_validation_returns_typed_structured_result() -> None:
    backend = FakeBackend()
    result = await _adapter(backend).call_tool(
        "maestro_validate_chain",
        {"chain": {"version": "1.0", "steps": []}},
    )

    assert result == {
        "parsed": True,
        "ok": True,
        "parse_errors": [],
        "required_tools": ["web_search"],
        "required_mcp_servers": [],
        "required_skills": [],
        "missing_tools": [],
        "missing_mcp_servers": [],
        "missing_skills": [],
        "preflight_available": True,
        "status": "completed",
        "request_id": "request-1",
    }
    assert backend.last_token is not None
    assert backend.last_token.is_cancelled is False


@pytest.mark.asyncio
async def test_memory_output_redacts_secret_shaped_fields() -> None:
    result = await _adapter().call_tool(
        "maestro_memory_search",
        {"query": "weather", "top_k": 3},
    )

    assert result["status"] == "completed"
    assert result["hits"][0]["api_key"] == "[redacted]"
    assert result["hits"][0]["provider_api_key"] == "[redacted]"
    assert "sk-should-not-cross-mcp" not in str(result)


@pytest.mark.asyncio
async def test_default_backend_does_not_implicitly_connect_to_memory() -> None:
    adapter = _adapter(CareReadOnlyMcpBackend())

    capabilities = await adapter.call_tool("maestro_capabilities", {})
    result = await adapter.call_tool("maestro_memory_search", {"query": "x"})

    memory = next(
        item for item in capabilities["capabilities"] if item["name"] == "memory_read"
    )
    assert memory["availability"] == "unavailable"
    assert result["status"] == "unavailable"
    assert result["hits"] == []


@pytest.mark.asyncio
async def test_contract_and_lossless_preview_cross_mcp_without_source_values() -> None:
    private_value = "private-moscow-value"
    chain = {
        "version": "1.0",
        "steps": [],
        "metadata": {
            "care": {
                "runtime_input_schema": {
                    "type": "object",
                    "properties": {"city": {"type": "string"}},
                    "required": ["city"],
                    "additionalProperties": False,
                }
            }
        },
    }
    service = HarnessOrchestrationService(
        chain_loader=lambda _chain_id, _channel: chain,
        artifacts=JsonArtifactStore(id_factory=lambda: "artifact-1"),
        previews=AdaptationPreviewStore(id_factory=lambda: "preview-1"),
    )
    source = service.stage_input({"town": private_value})
    adapter = _adapter(CareReadOnlyMcpBackend(orchestration=service))

    contract = await adapter.call_tool(
        "maestro_chain_input_contract",
        {"chain_id": "chain-1"},
    )
    inspection = await adapter.call_tool(
        "maestro_input_adaptation_inspect",
        {"chain_id": "chain-1", "artifact_id": source.artifact_id},
    )
    preview = await adapter.call_tool(
        "maestro_input_adaptation_preview",
        {
            "chain_id": "chain-1",
            "artifact_id": source.artifact_id,
            "artifact_sha256": source.content_sha256,
            "bindings": [{"source": "/town", "target": "/city"}],
        },
    )

    assert contract["inspection"]["status"] == "ready"
    assert inspection["inspection"]["source"]["leaf_pointers"] == ["/town"]
    assert preview["result"]["status"] == "ready"
    assert preview["result"]["preview"]["preview_id"] == "preview-1"
    assert private_value not in str(contract)
    assert private_value not in str(inspection)
    assert private_value not in str(preview)


@pytest.mark.asyncio
async def test_generation_is_configured_but_remains_host_approval_gated() -> None:
    class Generation:
        def __init__(self) -> None:
            self.calls: list[tuple[object, CancellationToken]] = []

        async def generate(self, request, *, cancellation):
            self.calls.append((request, cancellation))
            return ChainGenerationReceipt.model_validate(
                {
                    "idempotency_key": request.idempotency_key,
                    "request_fingerprint": "a" * 64,
                    "artifact": {
                        "artifact_id": "artifact-generated",
                        "content_sha256": "b" * 64,
                        "size_bytes": 128,
                        "expires_at_unix": 2_000_000_000.0,
                    },
                    "summary": {
                        "step_count": 1,
                        "step_types": ({"step_type": "llm", "count": 1},),
                        "required_tool_count": 0,
                        "required_mcp_server_count": 0,
                        "required_skill_count": 0,
                        "missing_capability_count": 0,
                        "execution_ready": True,
                        "preflight_available": True,
                    },
                }
            )

    generation = Generation()
    adapter = _adapter(CareReadOnlyMcpBackend(generation=generation))

    capabilities = await adapter.call_tool("maestro_capabilities", {})
    generated = await adapter.call_tool(
        "maestro_generate_chain",
        {
            "idempotency_key": "generate-0001",
            "query": "Build a chain",
            "safe_task": "Build a chain",
            "mode": "fast",
        },
    )

    state = next(
        item
        for item in capabilities["capabilities"]
        if item["name"] == "generate_chain"
    )
    assert state["availability"] == "approval_gated"
    assert state["confirmation_required"] is True
    assert generated["status"] == "completed"
    assert generated["receipt"]["artifact"]["artifact_id"] == "artifact-generated"
    assert len(generation.calls) == 1
    assert generation.calls[0][1].is_cancelled is False


@pytest.mark.asyncio
async def test_unknown_and_extra_arguments_fail_before_backend_call() -> None:
    backend = FakeBackend()
    adapter = _adapter(backend)

    with pytest.raises(UnknownHarnessToolError):
        await adapter.call_tool("not_a_tool", {})
    with pytest.raises(InvalidHarnessToolArguments, match="Extra inputs"):
        await adapter.call_tool(
            "maestro_validate_chain",
            {"chain": {}, "surprise": True},
        )

    assert backend.last_token is None


class BlockingBackend(FakeBackend):
    def __init__(self) -> None:
        super().__init__()
        self.started = asyncio.Event()

    async def validate_chain(
        self,
        request: ValidateChainRequest,
        *,
        cancellation: CancellationToken,
    ) -> ValidateChainData:
        self.last_token = cancellation
        self.started.set()
        await asyncio.Future()
        raise AssertionError("unreachable")


@pytest.mark.asyncio
async def test_asyncio_cancellation_flips_the_care_token_and_propagates() -> None:
    backend = BlockingBackend()
    task = asyncio.create_task(
        _adapter(backend).call_tool("maestro_validate_chain", {"chain": {}})
    )
    await backend.started.wait()

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert backend.last_token is not None
    assert backend.last_token.is_cancelled is True


class FailingBackend(FakeBackend):
    async def validate_chain(
        self,
        request: ValidateChainRequest,
        *,
        cancellation: CancellationToken,
    ) -> ValidateChainData:
        raise RuntimeError("api_key=sk-very-secret")


@pytest.mark.asyncio
async def test_backend_failure_does_not_expose_exception_text() -> None:
    with pytest.raises(HarnessToolExecutionError) as captured:
        await _adapter(FailingBackend()).call_tool(
            "maestro_validate_chain",
            {"chain": {}},
        )

    assert "RuntimeError" in str(captured.value)
    assert "sk-very-secret" not in str(captured.value)


@pytest.mark.asyncio
async def test_oversized_json_is_rejected_at_the_boundary() -> None:
    with pytest.raises(InvalidHarnessToolArguments, match="exceeds"):
        await _adapter().call_tool(
            "maestro_validate_chain",
            {"chain": {"blob": "x" * 2_000_001}},
        )
