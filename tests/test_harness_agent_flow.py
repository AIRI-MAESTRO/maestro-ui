"""Provider-free generate -> stage -> execute flow through typed MCP."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from care.config import CareConfig, MageConfig
from care.harness_artifacts import JsonArtifactStore
from care.harness_contracts import AdaptationPreviewStore
from care.harness_execution import HarnessChainExecutionOperation
from care.harness_generation import HarnessChainGenerationOperation
from care.harness_mcp import CareMcpAdapter, CareReadOnlyMcpBackend


GENERATED_CHAIN = {
    "task_description": "Greet one person",
    "steps": [
        {
            "number": 1,
            "title": "Greet",
            "step_type": "llm",
            "aim": "Return a greeting for the supplied name",
        }
    ],
}
INPUT_SCHEMA = {
    "type": "object",
    "properties": {"name": {"type": "string"}},
    "required": ["name"],
    "additionalProperties": False,
}


@pytest.mark.asyncio
async def test_agent_can_generate_contract_stage_input_and_execute_exact_chain() -> None:
    ids = iter(f"flow-artifact-{index}" for index in range(1, 20))
    store = JsonArtifactStore(id_factory=lambda: next(ids))
    config = CareConfig(
        mage=MageConfig(
            api_key="unused-provider-key",
            base_url="https://unused.invalid/v1",
            model="unused-model",
        )
    )

    async def generate(*_args: Any, **_kwargs: Any) -> Any:
        return SimpleNamespace(chain_dict=GENERATED_CHAIN)

    generation = HarnessChainGenerationOperation(
        config=config,
        artifacts=store,
        generator_builder=lambda *_args, **_kwargs: object(),
        generation_runner=generate,
    )
    observed: dict[str, Any] = {}

    async def execute(_chain: Any, context: Any) -> Any:
        observed["input"] = context.memory["input"]
        return SimpleNamespace(
            success=True,
            final_output="Hello Ada",
            step_results=[object()],
        )

    execution = HarnessChainExecutionOperation(
        config=config,
        artifacts=store,
        previews=AdaptationPreviewStore(),
        api_builder=lambda _mage: object(),
        chain_executor=execute,
    )
    adapter = CareMcpAdapter(
        CareReadOnlyMcpBackend(
            generation=generation,
            artifact_stager=store,
            execution=execution,
        ),
        request_id_factory=lambda: "agent-flow-request",
    )

    generated = await adapter.call_tool(
        "maestro_generate_chain",
        {
            "idempotency_key": "agent-flow-generate-0001",
            "query": "Build a greeting chain",
            "safe_task": "Build a greeting chain",
            "runtime_input_schema": INPUT_SCHEMA,
        },
    )
    chain_ref = generated["receipt"]["artifact"]
    chain = store.resolve(
        chain_ref["artifact_id"],
        content_sha256=chain_ref["content_sha256"],
    )
    assert chain["metadata"]["care"]["runtime_input_schema"]["required"] == [
        "name"
    ]

    staged = await adapter.call_tool(
        "maestro_stage_artifact",
        {"kind": "input", "payload": {"name": "Ada"}},
    )
    input_ref = staged["receipt"]
    executed = await adapter.call_tool(
        "maestro_execute_chain",
        {
            "idempotency_key": "agent-flow-execute-0001",
            "chain_id": "generated-greeting",
            "task": "Greet the user",
            "chain": {
                "artifact_id": chain_ref["artifact_id"],
                "content_sha256": chain_ref["content_sha256"],
            },
            "input": {
                "kind": "artifact",
                "artifact_id": input_ref["artifact_id"],
                "content_sha256": input_ref["content_sha256"],
            },
        },
    )

    assert observed["input"] == {"name": "Ada"}
    assert executed["status"] == "completed"
    assert executed["receipt"]["success"] is True
    assert executed["receipt"]["final_output"] == "Hello Ada"
    assert "unused-provider-key" not in str(executed)
