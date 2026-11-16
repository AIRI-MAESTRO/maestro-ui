"""Focused, provider-free tests for the resident harness generation seam."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from types import SimpleNamespace
from typing import Any

from pydantic import ValidationError
import pytest

from care.config import CareConfig, MageConfig
from care.harness_generation import (
    ChainGenerationOperationError,
    ChainGenerationRequest,
    HarnessChainGenerationOperation,
)
from care.preflight import PreflightResult
from care.runtime.cancellation import CancelledByUserError, CancellationToken


CHAIN = {
    "task_description": "private generated task",
    "steps": [
        {
            "number": 1,
            "title": "private generated title",
            "step_type": "llm",
            "aim": "private generated aim",
        },
        {
            "number": 2,
            "title": "search",
            "step_type": "web_search",
            "aim": "find sources",
        },
    ],
}


class FakeArtifactStore:
    def __init__(self) -> None:
        self.payloads: list[dict[str, Any]] = []

    def stage(self, payload: Mapping[str, Any]) -> Any:
        self.payloads.append(dict(payload))
        index = len(self.payloads)
        return SimpleNamespace(
            artifact_id=f"artifact-{index}",
            content_sha256=f"{index:064x}",
            size_bytes=321,
            expires_at_unix=2_000_000_000.0,
        )


def request(
    key: str = "request-0001",
    *,
    query: str = "Build a research chain",
    runtime_input_schema: dict[str, Any] | None = None,
) -> ChainGenerationRequest:
    return ChainGenerationRequest(
        idempotency_key=key,
        query=query,
        safe_task="Build a research chain",
        mode="deep",
        deployable=True,
        runtime_input_schema=runtime_input_schema,
    )


def admitted_preflight(**overrides: Any) -> PreflightResult:
    values: dict[str, Any] = {
        "parsed": True,
        "required_tools": ("web_search",),
        "required_mcp_servers": ("memory",),
        "required_skills": (),
        "missing_tools": (),
        "missing_mcp_servers": ("memory",),
        "preflight_available": True,
    }
    values.update(overrides)
    return PreflightResult(**values)


def operation(
    *,
    store: Any | None = None,
    builder: Any | None = None,
    runner: Any | None = None,
    validator: Any | None = None,
    clock: Any | None = None,
    ttl: float = 60.0,
    max_entries: int = 16,
) -> HarnessChainGenerationOperation:
    kwargs: dict[str, Any] = {
        "config": CareConfig(mage=MageConfig(api_key="secret-provider-key")),
        "artifacts": store or FakeArtifactStore(),
        "generator_builder": builder or (lambda *_args, **_kwargs: object()),
        "generation_runner": runner or _successful_runner,
        "chain_validator": validator or (lambda *_args, **_kwargs: admitted_preflight()),
        "idempotency_ttl_seconds": ttl,
        "max_idempotency_entries": max_entries,
    }
    if clock is not None:
        kwargs["clock"] = clock
    return HarnessChainGenerationOperation(**kwargs)


async def _successful_runner(*_args: Any, **_kwargs: Any) -> Any:
    return SimpleNamespace(chain_dict=CHAIN)


def test_request_is_strict_and_requires_safe_non_blank_text() -> None:
    with pytest.raises(ValidationError):
        ChainGenerationRequest(
            idempotency_key="short",
            query="task",
            safe_task="task",
        )
    with pytest.raises(ValidationError):
        ChainGenerationRequest(
            idempotency_key="request-0001",
            query="   ",
            safe_task="task",
        )
    with pytest.raises(ValidationError):
        ChainGenerationRequest(
            idempotency_key="request-0001",
            query="task",
            safe_task="task",
            approved=True,
        )
    with pytest.raises(ValidationError):
        request(runtime_input_schema={"type": "string"})


@pytest.mark.asyncio
async def test_success_uses_public_contract_and_returns_no_raw_chain() -> None:
    captured: dict[str, Any] = {}
    store = FakeArtifactStore()

    def builder(config: CareConfig, **kwargs: Any) -> object:
        captured["config"] = config
        captured["builder"] = kwargs
        return object()

    async def runner(generator: object, query: str, **kwargs: Any) -> Any:
        captured["generator"] = generator
        captured["query"] = query
        captured["runner"] = kwargs
        return SimpleNamespace(chain_dict=CHAIN)

    def validator(payload: Any, **kwargs: Any) -> PreflightResult:
        captured["validated"] = payload
        captured["validator"] = kwargs
        return admitted_preflight()

    op = operation(store=store, builder=builder, runner=runner, validator=validator)
    receipt = await op.generate(request())

    assert captured["builder"] == {
        "progress": None,
        "mode": "deep",
        "deployable": True,
    }
    assert captured["query"] == "Build a research chain"
    assert captured["runner"]["safe_task"] == "Build a research chain"
    assert isinstance(captured["runner"]["cancel"], asyncio.Event)
    assert captured["validator"] == {"strict_references": True}
    assert store.payloads == [CHAIN]
    assert receipt.artifact.artifact_id == "artifact-1"
    assert receipt.summary.step_count == 2
    assert [(item.step_type, item.count) for item in receipt.summary.step_types] == [
        ("llm", 1),
        ("web_search", 1),
    ]
    assert receipt.summary.execution_ready is False
    assert receipt.summary.runtime_input_contract is False
    assert receipt.idempotency_scope == "process_ttl_only"
    public = receipt.model_dump_json()
    assert "private generated" not in public
    assert "chain_dict" not in public
    assert "secret-provider-key" not in public


@pytest.mark.asyncio
async def test_requested_runtime_contract_is_admitted_and_attached_before_stage() -> None:
    store = FakeArtifactStore()
    observed: dict[str, Any] = {}
    schema = {
        "type": "object",
        "properties": {"city": {"type": "string"}},
        "required": ["city"],
        "additionalProperties": False,
    }

    def validator(payload: Any, **_kwargs: Any) -> PreflightResult:
        observed["validated"] = payload
        return admitted_preflight()

    receipt = await operation(store=store, validator=validator).generate(
        request(runtime_input_schema=schema)
    )

    staged_schema = store.payloads[0]["metadata"]["care"][
        "runtime_input_schema"
    ]
    assert observed["validated"] == store.payloads[0]
    assert staged_schema["type"] == "object"
    assert staged_schema["required"] == ["city"]
    assert staged_schema["$schema"].endswith("draft/2020-12/schema")
    assert receipt.summary.runtime_input_contract is True


@pytest.mark.asyncio
async def test_default_public_runner_builds_typed_mage_generation_request() -> None:
    from mmar_mage.schemas import GenerationRequest

    captured: dict[str, Any] = {}
    valid_chain = {
        "task_description": "demo",
        "steps": [
            {
                "number": 1,
                "title": "first",
                "step_type": "llm",
                "aim": "say hi",
            }
        ],
    }

    class FakeGenerator:
        async def generate(
            self,
            query: GenerationRequest,
            *,
            cancel: asyncio.Event | None = None,
            capabilities: Any = None,
        ) -> Any:
            captured["query"] = query
            captured["cancel"] = cancel
            captured["capabilities"] = capabilities
            return SimpleNamespace(chain_dict=valid_chain)

    op = HarnessChainGenerationOperation(
        config=CareConfig(mage=MageConfig(api_key="unused-test-key")),
        artifacts=FakeArtifactStore(),
        generator_builder=lambda *_args, **_kwargs: FakeGenerator(),
    )
    receipt = await op.generate(request())

    assert isinstance(captured["query"], GenerationRequest)
    assert captured["query"].current_task == "Build a research chain"
    assert isinstance(captured["cancel"], asyncio.Event)
    assert receipt.status == "ready"


@pytest.mark.asyncio
async def test_mapping_result_and_mapping_artifact_receipt_are_supported() -> None:
    class MappingStore:
        def stage(self, _payload: Mapping[str, Any]) -> dict[str, Any]:
            return {
                "session_id": "session-1",
                "artifact_id": "artifact-map",
                "content_sha256": "a" * 64,
                "size_bytes": 50,
                "expires_at_unix": 2_000_000_000.0,
                "ignored": "not projected",
            }

    async def runner(*_args: Any, **_kwargs: Any) -> Any:
        return {"chain_dict": CHAIN, "metadata": {"private": "value"}}

    receipt = await operation(store=MappingStore(), runner=runner).generate(request())
    assert receipt.artifact.session_id == "session-1"
    assert receipt.artifact.artifact_id == "artifact-map"


@pytest.mark.asyncio
async def test_invalid_generated_chain_never_reaches_artifact_store() -> None:
    store = FakeArtifactStore()

    def validator(*_args: Any, **_kwargs: Any) -> PreflightResult:
        return PreflightResult(
            parsed=False,
            parse_errors=("private chain value must not escape",),
        )

    op = operation(store=store, validator=validator)
    with pytest.raises(ChainGenerationOperationError) as caught:
        await op.generate(request())
    assert caught.value.failure.code == "chain_admission_failed"
    assert "private chain value" not in str(caught.value)
    assert store.payloads == []


@pytest.mark.asyncio
async def test_result_without_chain_dict_fails_closed() -> None:
    store = FakeArtifactStore()

    async def runner(*_args: Any, **_kwargs: Any) -> Any:
        return {"metadata": {"secret": "must not escape"}}

    with pytest.raises(ChainGenerationOperationError) as caught:
        await operation(store=store, runner=runner).generate(request())
    assert caught.value.failure.code == "invalid_generation_result"
    assert "must not escape" not in str(caught.value)
    assert store.payloads == []


@pytest.mark.asyncio
async def test_provider_failure_is_type_only_and_cached() -> None:
    calls = 0

    class PrivateProviderFailure(RuntimeError):
        pass

    async def runner(*_args: Any, **_kwargs: Any) -> Any:
        nonlocal calls
        calls += 1
        raise PrivateProviderFailure("sk-live-secret at https://private.invalid")

    op = operation(runner=runner)
    for _ in range(2):
        with pytest.raises(ChainGenerationOperationError) as caught:
            await op.generate(request())
        assert caught.value.failure.code == "provider_failed"
        assert caught.value.failure.failure_type == "PrivateProviderFailure"
        assert "sk-live-secret" not in str(caught.value)
        assert "private.invalid" not in str(caught.value)
    assert calls == 1


@pytest.mark.asyncio
async def test_sequential_duplicate_replays_without_provider_or_store_call() -> None:
    calls = 0
    store = FakeArtifactStore()

    async def runner(*_args: Any, **_kwargs: Any) -> Any:
        nonlocal calls
        calls += 1
        return SimpleNamespace(chain_dict=CHAIN)

    op = operation(store=store, runner=runner)
    first = await op.generate(request())
    second = await op.generate(request())

    assert calls == 1
    assert len(store.payloads) == 1
    assert first.replayed is False
    assert second.replayed is True
    assert second.artifact == first.artifact


@pytest.mark.asyncio
async def test_same_key_with_different_request_is_rejected() -> None:
    calls = 0

    async def runner(*_args: Any, **_kwargs: Any) -> Any:
        nonlocal calls
        calls += 1
        return SimpleNamespace(chain_dict=CHAIN)

    op = operation(runner=runner)
    await op.generate(request())
    with pytest.raises(ChainGenerationOperationError) as caught:
        await op.generate(request(query="A different private task"))
    assert caught.value.failure.code == "idempotency_conflict"
    assert calls == 1


@pytest.mark.asyncio
async def test_concurrent_duplicates_coalesce_onto_one_provider_task() -> None:
    started = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def runner(*_args: Any, **_kwargs: Any) -> Any:
        nonlocal calls
        calls += 1
        started.set()
        await release.wait()
        return SimpleNamespace(chain_dict=CHAIN)

    op = operation(runner=runner)
    first_task = asyncio.create_task(op.generate(request()))
    await started.wait()
    second_task = asyncio.create_task(op.generate(request()))
    await asyncio.sleep(0)
    release.set()
    first, second = await asyncio.gather(first_task, second_task)

    assert calls == 1
    assert first.artifact == second.artifact
    assert first.replayed is False
    assert second.replayed is True


@pytest.mark.asyncio
async def test_cancellation_token_sets_mage_event_and_terminal_is_replayed() -> None:
    observed_cancel = asyncio.Event()
    calls = 0

    async def runner(*_args: Any, **kwargs: Any) -> Any:
        nonlocal calls
        calls += 1
        cancel = kwargs["cancel"]
        await cancel.wait()
        observed_cancel.set()
        await asyncio.sleep(3600)

    token = CancellationToken()
    op = operation(runner=runner)
    pending = asyncio.create_task(op.generate(request(), cancellation=token))
    await asyncio.sleep(0)
    token.cancel()
    with pytest.raises(CancelledByUserError):
        await pending
    assert observed_cancel.is_set()

    with pytest.raises(CancelledByUserError):
        await op.generate(request())
    assert calls == 1


@pytest.mark.asyncio
async def test_expired_ledger_entry_allows_new_generation_after_ttl() -> None:
    now = [10.0]
    calls = 0

    async def runner(*_args: Any, **_kwargs: Any) -> Any:
        nonlocal calls
        calls += 1
        return SimpleNamespace(chain_dict=CHAIN)

    op = operation(runner=runner, clock=lambda: now[0], ttl=5.0)
    await op.generate(request())
    await op.generate(request())
    assert calls == 1

    now[0] = 16.0
    renewed = await op.generate(request())
    assert calls == 2
    assert renewed.replayed is False


@pytest.mark.asyncio
async def test_bounded_ledger_rejects_new_key_without_evicting_live_receipt() -> None:
    calls = 0

    async def runner(*_args: Any, **_kwargs: Any) -> Any:
        nonlocal calls
        calls += 1
        return SimpleNamespace(chain_dict=CHAIN)

    op = operation(runner=runner, max_entries=1)
    first = await op.generate(request("request-0001"))
    with pytest.raises(ChainGenerationOperationError) as caught:
        await op.generate(request("request-0002"))
    assert caught.value.failure.code == "idempotency_ledger_full"

    replay = await op.generate(request("request-0001"))
    assert replay.artifact == first.artifact
    assert calls == 1


@pytest.mark.asyncio
async def test_artifact_failure_is_redacted_and_cached() -> None:
    calls = 0

    class FailingStore:
        def stage(self, _payload: Mapping[str, Any]) -> Any:
            raise RuntimeError("/private/path and private chain data")

    async def runner(*_args: Any, **_kwargs: Any) -> Any:
        nonlocal calls
        calls += 1
        return SimpleNamespace(chain_dict=CHAIN)

    op = operation(store=FailingStore(), runner=runner)
    for _ in range(2):
        with pytest.raises(ChainGenerationOperationError) as caught:
            await op.generate(request())
        assert caught.value.failure.code == "artifact_stage_failed"
        assert caught.value.failure.failure_type == "RuntimeError"
        assert "/private/path" not in str(caught.value)
    assert calls == 1


def test_constructor_validates_ledger_bounds() -> None:
    with pytest.raises(ValueError):
        operation(ttl=0)
    with pytest.raises(ValueError):
        operation(max_entries=0)
