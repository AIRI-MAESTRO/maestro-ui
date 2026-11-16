"""Provider-free tests for the resident harness CARL execution seam."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

from pydantic import ValidationError
import pytest

from care.config import CareConfig, MageConfig
from care.harness_artifacts import JsonArtifactStore
from care.harness_contracts import (
    AdaptationPreviewStore,
    JsonPointerBinding,
    PreviewUnavailableError,
    chain_input_contract,
)
from care.harness_execution import (
    AdaptationPreviewInput,
    ChainArtifactReference,
    ChainExecutionOperationError,
    ChainExecutionRequest,
    ExactInputArtifact,
    HarnessChainExecutionOperation,
)
from care.preflight import PreflightResult
from care.runtime.cancellation import CancelledByUserError, CancellationToken


SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "properties": {"name": {"type": "string"}},
    "required": ["name"],
    "additionalProperties": False,
}
CHAIN = {
    "task_description": "private chain task",
    "metadata": {"care": {"runtime_input_schema": SCHEMA}},
    "steps": [
        {
            "number": 1,
            "title": "answer",
            "step_type": "llm",
            "aim": "answer with the provided name",
        }
    ],
}


class FakeContext:
    def __init__(self) -> None:
        self.cancel_calls = 0

    def cancel(self) -> None:
        self.cancel_calls += 1


class Result:
    def __init__(
        self,
        *,
        success: bool = True,
        final_output: str = "Hello Ada",
        steps: int = 1,
    ) -> None:
        self.success = success
        self.final_output = final_output
        self.step_results = [object() for _ in range(steps)]


def artifact_store() -> JsonArtifactStore:
    ids = iter(f"artifact-{index}" for index in range(1, 100))
    return JsonArtifactStore(id_factory=lambda: next(ids))


def exact_request(
    store: JsonArtifactStore,
    *,
    key: str = "execute-0001",
    payload: dict[str, Any] | None = None,
) -> ChainExecutionRequest:
    chain = store.stage(CHAIN)
    input_artifact = store.stage(payload or {"name": "Ada"})
    return ChainExecutionRequest(
        idempotency_key=key,
        chain_id="chain-1",
        task="Greet the user",
        chain=ChainArtifactReference(
            artifact_id=chain.artifact_id,
            content_sha256=chain.content_sha256,
        ),
        input=ExactInputArtifact(
            artifact_id=input_artifact.artifact_id,
            content_sha256=input_artifact.content_sha256,
        ),
    )


def operation(
    *,
    store: JsonArtifactStore,
    previews: AdaptationPreviewStore | None = None,
    api_builder: Any | None = None,
    context_builder: Any | None = None,
    validator: Any | None = None,
    executor: Any | None = None,
) -> HarnessChainExecutionOperation:
    parsed_chain = SimpleNamespace(steps=[object()])

    def default_validator(
        _payload: Any,
        *,
        context: Any,
        strict_references: bool,
    ) -> PreflightResult:
        assert strict_references is True
        return PreflightResult(
            parsed=True,
            chain=parsed_chain,
            preflight_available=True,
        )

    async def default_executor(_chain: Any, _context: Any) -> Result:
        return Result()

    return HarnessChainExecutionOperation(
        config=CareConfig(
            mage=MageConfig(
                api_key="unused-provider-secret",
                base_url="https://provider.invalid/v1",
                model="unused-model",
            )
        ),
        artifacts=store,
        previews=previews or AdaptationPreviewStore(),
        api_builder=api_builder or (lambda _mage: object()),
        context_builder=context_builder
        or (lambda **_kwargs: FakeContext()),
        chain_validator=validator or default_validator,
        chain_executor=executor or default_executor,
    )


def test_request_is_strict_and_exposes_no_path_or_approval_escape_hatch() -> None:
    store = artifact_store()
    request = exact_request(store)

    for extra in (
        {"approved": True},
        {"tools_path": "/tmp/untrusted.py"},
        {"chain_path": "/tmp/chain.json"},
    ):
        with pytest.raises(ValidationError):
            ChainExecutionRequest.model_validate(
                {**request.model_dump(mode="json"), **extra}
            )

    with pytest.raises(ValidationError):
        ChainExecutionRequest.model_validate(
            {
                **request.model_dump(mode="json"),
                "input": {
                    "kind": "artifact",
                    "artifact_id": "../input",
                    "content_sha256": "a" * 64,
                },
            }
        )


@pytest.mark.asyncio
async def test_exact_artifacts_are_digest_pinned_revalidated_and_executed() -> None:
    store = artifact_store()
    request = exact_request(store)
    calls: dict[str, Any] = {"validation": []}
    context = FakeContext()
    admitted_chain = SimpleNamespace(steps=[object()])

    def api_builder(mage: MageConfig) -> object:
        calls["mage"] = mage
        return object()

    def context_builder(**kwargs: Any) -> FakeContext:
        calls["context"] = kwargs
        return context

    def validator(payload: Any, **kwargs: Any) -> PreflightResult:
        calls["validation"].append((payload, kwargs))
        return PreflightResult(
            parsed=True,
            chain=admitted_chain,
            preflight_available=True,
        )

    async def executor(chain: Any, selected_context: Any) -> Result:
        calls["execution"] = (chain, selected_context)
        return Result(final_output="private final answer", steps=2)

    op = operation(
        store=store,
        api_builder=api_builder,
        context_builder=context_builder,
        validator=validator,
        executor=executor,
    )
    receipt = await op.execute(request)

    assert len(calls["validation"]) == 2
    assert calls["validation"][0][1] == {
        "context": None,
        "strict_references": True,
    }
    assert calls["validation"][1][1] == {
        "context": context,
        "strict_references": True,
    }
    assert calls["context"]["query"] == "Greet the user"
    assert calls["context"]["files"] == {"name": "Ada"}
    assert calls["execution"] == (admitted_chain, context)
    assert receipt.status == "completed"
    assert receipt.success is True
    assert receipt.final_output == "private final answer"
    assert receipt.step_count == 2
    assert receipt.chain_digest == request.chain.content_sha256
    assert receipt.input_digest == request.input.content_sha256
    retained = store.resolve(
        receipt.output_artifact.artifact_id,
        content_sha256=receipt.output_artifact.content_sha256,
    )
    assert retained == {
        "kind": "maestro.chain_execution_result.v1",
        "success": True,
        "final_output": "private final answer",
        "step_count": 2,
    }
    assert "unused-provider-secret" not in receipt.model_dump_json()
    with pytest.raises(ValidationError):
        receipt.success = False


@pytest.mark.asyncio
async def test_public_care_context_and_preflight_seams_work_without_provider_io() -> None:
    """Use the real CARE/CARL assembly while replacing only the network edge."""

    store = artifact_store()
    request = exact_request(store)
    captured: dict[str, Any] = {}

    async def executor(chain: Any, context: Any) -> Result:
        captured["chain_type"] = type(chain).__name__
        captured["context_type"] = type(context).__name__
        captured["input"] = context.memory["input"]
        return Result()

    op = HarnessChainExecutionOperation(
        config=CareConfig(
            mage=MageConfig(
                api_key="unused-test-key",
                base_url="https://unused.invalid/v1",
                model="unused-model",
            )
        ),
        artifacts=store,
        previews=AdaptationPreviewStore(),
        api_builder=lambda _mage: object(),
        chain_executor=executor,
    )
    receipt = await op.execute(request)

    assert receipt.success is True
    assert captured == {
        "chain_type": "ReasoningChain",
        "context_type": "ReasoningContext",
        "input": {"name": "Ada"},
    }


@pytest.mark.asyncio
async def test_direct_input_must_satisfy_exact_declared_schema() -> None:
    store = artifact_store()
    request = exact_request(store, payload={"name": 42})
    api_calls = 0

    def api_builder(_mage: MageConfig) -> object:
        nonlocal api_calls
        api_calls += 1
        return object()

    with pytest.raises(ChainExecutionOperationError) as caught:
        await operation(store=store, api_builder=api_builder).execute(request)
    assert caught.value.failure.code == "input_contract_rejected"
    assert api_calls == 0
    assert "42" not in str(caught.value)


@pytest.mark.asyncio
async def test_chain_artifact_digest_is_enforced_before_runtime_build() -> None:
    store = artifact_store()
    request = exact_request(store).model_copy(
        update={
            "chain": ChainArtifactReference(
                artifact_id="artifact-1",
                content_sha256="f" * 64,
            )
        }
    )
    with pytest.raises(ChainExecutionOperationError) as caught:
        await operation(store=store).execute(request)
    assert caught.value.failure.code == "artifact_unavailable"


@pytest.mark.asyncio
async def test_single_use_adaptation_preview_is_bound_consumed_and_replayable() -> None:
    store = artifact_store()
    chain_artifact = store.stage(CHAIN)
    contract = chain_input_contract("chain-1", CHAIN)
    # URL-safe random preview ids may legitimately begin with an underscore.
    previews = AdaptationPreviewStore(id_factory=lambda: "_preview-1")
    preview = previews.preview(
        contract,
        {"legacy_name": "Ada"},
        (JsonPointerBinding(source="/legacy_name", target="/name"),),
    )
    assert preview.status == "ready"
    assert preview.preview_id is not None
    assert preview.confirmation_digest is not None

    request = ChainExecutionRequest(
        idempotency_key="preview-0001",
        chain_id="chain-1",
        task="Greet the user",
        chain=ChainArtifactReference(
            artifact_id=chain_artifact.artifact_id,
            content_sha256=chain_artifact.content_sha256,
        ),
        input=AdaptationPreviewInput(
            preview_id=preview.preview_id,
            chain_digest=preview.chain_digest,
            schema_digest=preview.schema_digest,
            source_digest=preview.source_digest,
            adapted_digest=preview.adapted_digest,
            confirmation_digest=preview.confirmation_digest,
        ),
    )
    captured: dict[str, Any] = {}

    def context_builder(**kwargs: Any) -> FakeContext:
        captured["files"] = kwargs["files"]
        return FakeContext()

    op = operation(
        store=store,
        previews=previews,
        context_builder=context_builder,
    )
    first = await op.execute(request)
    replay = await op.execute(request)

    assert captured["files"] == {"name": "Ada"}
    assert first.input_digest == preview.adapted_digest
    assert first.replayed is False
    assert replay.replayed is True
    with pytest.raises(PreviewUnavailableError):
        previews.consume(
            preview.preview_id,
            chain_digest=preview.chain_digest,
            schema_digest=preview.schema_digest,
            source_digest=preview.source_digest,
            confirmation_digest=preview.confirmation_digest,
        )
    with pytest.raises(ChainExecutionOperationError) as caught:
        await op.execute(
            request.model_copy(update={"idempotency_key": "preview-0002"})
        )
    assert caught.value.failure.code == "preview_unavailable"


@pytest.mark.asyncio
async def test_context_preflight_must_be_available_and_have_no_missing_capability() -> None:
    store = artifact_store()
    request = exact_request(store)
    validations = 0
    execute_calls = 0

    def validator(_payload: Any, *, context: Any, **_kwargs: Any) -> PreflightResult:
        nonlocal validations
        validations += 1
        return PreflightResult(
            parsed=True,
            chain=SimpleNamespace(steps=[]),
            missing_tools=("private_tool_name",) if context is not None else (),
            preflight_available=True,
        )

    async def executor(_chain: Any, _context: Any) -> Result:
        nonlocal execute_calls
        execute_calls += 1
        return Result()

    with pytest.raises(ChainExecutionOperationError) as caught:
        await operation(
            store=store,
            validator=validator,
            executor=executor,
        ).execute(request)
    assert caught.value.failure.code == "chain_admission_failed"
    assert "private_tool_name" not in str(caught.value)
    assert validations == 2
    assert execute_calls == 0


@pytest.mark.asyncio
async def test_concurrent_duplicate_requests_coalesce_onto_one_run() -> None:
    store = artifact_store()
    request = exact_request(store)
    started = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def executor(_chain: Any, _context: Any) -> Result:
        nonlocal calls
        calls += 1
        started.set()
        await release.wait()
        return Result()

    op = operation(store=store, executor=executor)
    first_task = asyncio.create_task(op.execute(request))
    await started.wait()
    second_task = asyncio.create_task(op.execute(request))
    await asyncio.sleep(0)
    release.set()
    first, second = await asyncio.gather(first_task, second_task)

    assert calls == 1
    assert first.replayed is False
    assert second.replayed is True
    assert first.output_artifact == second.output_artifact


@pytest.mark.asyncio
async def test_same_key_with_changed_request_is_rejected_without_second_run() -> None:
    store = artifact_store()
    request = exact_request(store)
    calls = 0

    async def executor(_chain: Any, _context: Any) -> Result:
        nonlocal calls
        calls += 1
        return Result()

    op = operation(store=store, executor=executor)
    await op.execute(request)
    with pytest.raises(ChainExecutionOperationError) as caught:
        await op.execute(request.model_copy(update={"task": "Different task"}))
    assert caught.value.failure.code == "idempotency_conflict"
    assert calls == 1


@pytest.mark.asyncio
async def test_provider_configuration_failure_is_redacted_and_cached() -> None:
    store = artifact_store()
    request = exact_request(store)
    calls = 0

    class PrivateConfigurationFailure(RuntimeError):
        pass

    def api_builder(_mage: MageConfig) -> object:
        nonlocal calls
        calls += 1
        raise PrivateConfigurationFailure(
            "sk-private at https://provider.internal/v1"
        )

    op = operation(store=store, api_builder=api_builder)
    for _attempt in range(2):
        with pytest.raises(ChainExecutionOperationError) as caught:
            await op.execute(request)
        assert caught.value.failure.code == "provider_configuration_failed"
        assert caught.value.failure.failure_type == "PrivateConfigurationFailure"
        assert "sk-private" not in str(caught.value)
        assert "provider.internal" not in str(caught.value)
    assert calls == 1


@pytest.mark.asyncio
async def test_runtime_initialization_failure_is_distinct_and_redacted() -> None:
    store = artifact_store()
    request = exact_request(store)

    class PrivateToolSetupFailure(RuntimeError):
        pass

    def context_builder(**_kwargs: Any) -> FakeContext:
        raise PrivateToolSetupFailure("/private/tool/path with sk-secret")

    with pytest.raises(ChainExecutionOperationError) as caught:
        await operation(store=store, context_builder=context_builder).execute(request)
    assert caught.value.failure.code == "runtime_initialization_failed"
    assert caught.value.failure.failure_type == "PrivateToolSetupFailure"
    assert "private/tool" not in str(caught.value)
    assert "sk-secret" not in str(caught.value)


@pytest.mark.asyncio
async def test_cancellation_propagates_to_context_and_is_terminally_cached() -> None:
    store = artifact_store()
    request = exact_request(store)
    context = FakeContext()
    started = asyncio.Event()
    execution_cancelled = asyncio.Event()
    calls = 0

    async def executor(_chain: Any, _context: Any) -> Result:
        nonlocal calls
        calls += 1
        started.set()
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            execution_cancelled.set()
            raise

    token = CancellationToken()
    op = operation(
        store=store,
        context_builder=lambda **_kwargs: context,
        executor=executor,
    )
    task = asyncio.create_task(op.execute(request, cancellation=token))
    await started.wait()
    token.cancel()
    with pytest.raises(CancelledByUserError):
        await task

    assert execution_cancelled.is_set()
    assert context.cancel_calls == 1
    assert calls == 1
    with pytest.raises(CancelledByUserError):
        await op.execute(request)
    assert calls == 1


@pytest.mark.asyncio
async def test_execution_failure_is_type_only_and_terminally_cached() -> None:
    store = artifact_store()
    request = exact_request(store)
    calls = 0

    class PrivateExecutionFailure(RuntimeError):
        pass

    async def executor(_chain: Any, _context: Any) -> Result:
        nonlocal calls
        calls += 1
        raise PrivateExecutionFailure("private result and credential")

    op = operation(store=store, executor=executor)
    for _attempt in range(2):
        with pytest.raises(ChainExecutionOperationError) as caught:
            await op.execute(request)
        assert caught.value.failure.code == "execution_failed"
        assert caught.value.failure.failure_type == "PrivateExecutionFailure"
        assert "private result" not in str(caught.value)
    assert calls == 1
