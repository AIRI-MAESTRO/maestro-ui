"""Provider-free tests for the resident harness Memory write seam."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
import threading
from types import SimpleNamespace
from typing import Any

from pydantic import ValidationError
import pytest

from care.harness_artifacts import JsonArtifactStore
from care.harness_memory_write import (
    ChainMemoryWriteOperationError,
    ChainMemoryWriteRequest,
    HarnessChainMemoryWriteOperation,
)
from care.memory import CareMemory
from care.preflight import PreflightResult


CHAIN = {
    "task_description": "private generated task",
    "steps": [
        {
            "number": 1,
            "title": "private generated title",
            "step_type": "llm",
            "aim": "private generated aim",
        }
    ],
}


class FakeArtifacts:
    def __init__(self, payload: Mapping[str, Any] = CHAIN) -> None:
        self.payload = dict(payload)
        self.calls: list[tuple[str, str]] = []

    def resolve(
        self,
        artifact_id: str,
        *,
        content_sha256: str,
    ) -> dict[str, Any]:
        self.calls.append((artifact_id, content_sha256))
        return dict(self.payload)


class FakeMemory:
    def __init__(self, *, entity_id: str = "chain-entity-1") -> None:
        self.entity_id = entity_id
        self.calls: list[tuple[Any, dict[str, Any]]] = []

    def save_chain(self, chain: Any, *, name: str, **kwargs: Any) -> str:
        self.calls.append((chain, {"name": name, **kwargs}))
        return self.entity_id


def request(
    key: str = "memory-write-0001",
    **overrides: Any,
) -> ChainMemoryWriteRequest:
    values: dict[str, Any] = {
        "idempotency_key": key,
        "artifact_id": "artifact-0001",
        "content_sha256": "a" * 64,
        "name": "Private generated chain",
        "query": "private user task",
        "domain": "research",
        "tags": ("generated", "domain:research"),
    }
    values.update(overrides)
    return ChainMemoryWriteRequest(**values)


def admitted_preflight(chain: Any = None, **overrides: Any) -> PreflightResult:
    values: dict[str, Any] = {
        "parsed": True,
        "chain": chain if chain is not None else object(),
        "required_tools": ("web_search",),
        "missing_tools": ("web_search",),
    }
    values.update(overrides)
    return PreflightResult(**values)


def operation(
    *,
    memory: Any | None = None,
    artifacts: Any | None = None,
    validator: Any | None = None,
    clock: Any | None = None,
    ttl: float = 60.0,
    max_entries: int = 16,
) -> HarnessChainMemoryWriteOperation:
    kwargs: dict[str, Any] = {
        "memory": memory or FakeMemory(),
        "artifacts": artifacts or FakeArtifacts(),
        "chain_validator": validator
        or (lambda *_args, **_kwargs: admitted_preflight()),
        "idempotency_ttl_seconds": ttl,
        "max_idempotency_entries": max_entries,
    }
    if clock is not None:
        kwargs["clock"] = clock
    return HarnessChainMemoryWriteOperation(**kwargs)


def test_request_is_strict_requires_key_and_rejects_approved() -> None:
    with pytest.raises(ValidationError):
        ChainMemoryWriteRequest(
            idempotency_key="short",
            artifact_id="artifact-1",
            content_sha256="a" * 64,
            name="chain",
        )
    with pytest.raises(ValidationError):
        ChainMemoryWriteRequest(
            idempotency_key="memory-write-0001",
            artifact_id="artifact-1",
            content_sha256="a" * 64,
            name="chain",
            approved=True,
        )
    admitted = request(tags=["json", "array"])
    assert admitted.tags == ("json", "array")
    with pytest.raises(ValidationError):
        request(tags=["not", 3])


def test_request_rejects_ambiguous_or_unsafe_metadata() -> None:
    with pytest.raises(ValidationError):
        request(name="   ")
    with pytest.raises(ValidationError):
        request(tags=("duplicate", "duplicate"))
    with pytest.raises(ValidationError):
        request(change_summary="revision without entity")
    with pytest.raises(ValidationError):
        request(entity_id="chain\x00secret")


@pytest.mark.asyncio
async def test_create_resolves_digest_strictly_revalidates_and_saves_typed_chain(
) -> None:
    typed_chain = object()
    artifacts = FakeArtifacts()
    memory = FakeMemory()
    captured: dict[str, Any] = {}

    def validator(payload: Any, **kwargs: Any) -> PreflightResult:
        captured["payload"] = payload
        captured["kwargs"] = kwargs
        return admitted_preflight(typed_chain)

    op = operation(memory=memory, artifacts=artifacts, validator=validator)
    receipt = await op.save(request())

    assert artifacts.calls == [("artifact-0001", "a" * 64)]
    assert captured == {
        "payload": CHAIN,
        "kwargs": {"strict_references": True},
    }
    assert len(memory.calls) == 1
    saved_chain, kwargs = memory.calls[0]
    assert saved_chain is typed_chain
    assert kwargs == {
        "name": "Private generated chain",
        "query": "private user task",
        "description": None,
        "domain": "research",
        "tags": ["generated", "domain:research"],
        "when_to_use": None,
        "author": None,
        "entity_id": None,
        "channel": "latest",
        "parent_version_id": None,
        "change_summary": None,
    }
    assert receipt.status == "saved"
    assert receipt.operation == "created"
    assert receipt.entity_id == "chain-entity-1"
    assert receipt.replayed is False
    assert receipt.idempotency_scope == "process_ttl_only"
    public = receipt.model_dump_json()
    assert "private generated" not in public
    assert "private user task" not in public
    assert "domain:research" not in public


@pytest.mark.asyncio
async def test_version_write_forwards_explicit_lineage() -> None:
    memory = FakeMemory(entity_id="existing-chain")
    op = operation(memory=memory)

    receipt = await op.save(
        request(
            entity_id="existing-chain",
            channel="stable",
            parent_version_id="version-7",
            change_summary="improve strict validation",
        )
    )

    assert receipt.operation == "versioned"
    assert receipt.entity_id == "existing-chain"
    assert receipt.channel == "stable"
    assert memory.calls[0][1]["entity_id"] == "existing-chain"
    assert memory.calls[0][1]["parent_version_id"] == "version-7"
    assert memory.calls[0][1]["change_summary"] == "improve strict validation"


@pytest.mark.asyncio
async def test_default_validator_and_public_care_memory_save_chain_surface() -> None:
    store = JsonArtifactStore(id_factory=lambda: "artifact-real")
    staged = store.stage(CHAIN)
    captured: dict[str, Any] = {}

    class FakeGigaEvoClient:
        def save_chain(self, chain: Any, **kwargs: Any) -> Any:
            captured["chain"] = chain
            captured["kwargs"] = kwargs
            return SimpleNamespace(entity_id="chain-entity-1")

    memory = CareMemory(FakeGigaEvoClient())  # type: ignore[arg-type]
    op = HarnessChainMemoryWriteOperation(memory=memory, artifacts=store)

    receipt = await op.save(
        request(
            artifact_id=staged.artifact_id,
            content_sha256=staged.content_sha256,
        )
    )

    assert captured["chain"]["version"] == "1.0"
    assert captured["chain"]["metadata"]["task_description"] == (
        "private user task"
    )
    assert captured["kwargs"]["name"] == "Private generated chain"
    assert receipt.entity_id == "chain-entity-1"


@pytest.mark.asyncio
async def test_parse_failure_never_reaches_memory_and_is_value_redacted() -> None:
    memory = FakeMemory()

    def validator(*_args: Any, **_kwargs: Any) -> PreflightResult:
        return PreflightResult(
            parsed=False,
            parse_errors=("private generated title cannot be parsed",),
        )

    with pytest.raises(ChainMemoryWriteOperationError) as caught:
        await operation(memory=memory, validator=validator).save(request())

    assert caught.value.failure.code == "chain_admission_failed"
    assert "private generated title" not in str(caught.value)
    assert memory.calls == []


@pytest.mark.asyncio
async def test_preflight_exception_diagnostic_fails_admission() -> None:
    memory = FakeMemory()
    op = operation(
        memory=memory,
        validator=lambda *_args, **_kwargs: admitted_preflight(
            parse_errors=("preflight raised: private detail",)
        ),
    )

    with pytest.raises(ChainMemoryWriteOperationError) as caught:
        await op.save(request())
    assert caught.value.failure.code == "chain_admission_failed"
    assert memory.calls == []


@pytest.mark.asyncio
async def test_artifact_failure_is_redacted_and_cached() -> None:
    calls = 0

    class PrivateArtifactFailure(RuntimeError):
        pass

    class FailingArtifacts:
        def resolve(self, *_args: Any, **_kwargs: Any) -> dict[str, Any]:
            nonlocal calls
            calls += 1
            raise PrivateArtifactFailure("/private/path with private payload")

    op = operation(artifacts=FailingArtifacts())
    for _ in range(2):
        with pytest.raises(ChainMemoryWriteOperationError) as caught:
            await op.save(request())
        assert caught.value.failure.code == "artifact_unavailable"
        assert caught.value.failure.failure_type == "PrivateArtifactFailure"
        assert "/private/path" not in str(caught.value)
    assert calls == 1


@pytest.mark.asyncio
async def test_memory_failure_is_redacted_and_cached_for_uncertain_commit() -> None:
    calls = 0

    class PrivateMemoryFailure(RuntimeError):
        pass

    class FailingMemory:
        def save_chain(self, *_args: Any, **_kwargs: Any) -> str:
            nonlocal calls
            calls += 1
            raise PrivateMemoryFailure("token=secret task=private")

    op = operation(memory=FailingMemory())
    for _ in range(2):
        with pytest.raises(ChainMemoryWriteOperationError) as caught:
            await op.save(request())
        assert caught.value.failure.code == "memory_save_failed"
        assert caught.value.failure.failure_type == "PrivateMemoryFailure"
        assert "token=secret" not in str(caught.value)
    assert calls == 1


@pytest.mark.asyncio
async def test_invalid_memory_entity_id_is_rejected_and_cached() -> None:
    memory = FakeMemory(entity_id="different-entity")
    op = operation(memory=memory)
    version_request = request(entity_id="expected-entity")

    for _ in range(2):
        with pytest.raises(ChainMemoryWriteOperationError) as caught:
            await op.save(version_request)
        assert caught.value.failure.code == "invalid_memory_result"
    assert len(memory.calls) == 1


@pytest.mark.asyncio
async def test_sequential_duplicate_replays_without_second_write() -> None:
    memory = FakeMemory()
    artifacts = FakeArtifacts()
    op = operation(memory=memory, artifacts=artifacts)

    first = await op.save(request())
    second = await op.save(request())

    assert len(memory.calls) == 1
    assert len(artifacts.calls) == 1
    assert first.replayed is False
    assert second.replayed is True
    assert second.entity_id == first.entity_id


@pytest.mark.asyncio
async def test_same_key_with_different_request_is_rejected() -> None:
    memory = FakeMemory()
    op = operation(memory=memory)
    await op.save(request())

    with pytest.raises(ChainMemoryWriteOperationError) as caught:
        await op.save(request(name="different private name"))

    assert caught.value.failure.code == "idempotency_conflict"
    assert len(memory.calls) == 1


@pytest.mark.asyncio
async def test_concurrent_duplicates_coalesce_onto_one_memory_call() -> None:
    started = threading.Event()
    release = threading.Event()

    class BlockingMemory(FakeMemory):
        def save_chain(self, chain: Any, *, name: str, **kwargs: Any) -> str:
            started.set()
            assert release.wait(timeout=5)
            return super().save_chain(chain, name=name, **kwargs)

    memory = BlockingMemory()
    op = operation(memory=memory)
    first_task = asyncio.create_task(op.save(request()))
    assert await asyncio.to_thread(started.wait, 5)
    second_task = asyncio.create_task(op.save(request()))
    await asyncio.sleep(0)
    release.set()
    first, second = await asyncio.gather(first_task, second_task)

    assert len(memory.calls) == 1
    assert first.replayed is False
    assert second.replayed is True
    assert first.entity_id == second.entity_id


@pytest.mark.asyncio
async def test_cancelled_waiter_does_not_cancel_uncertain_memory_write() -> None:
    started = threading.Event()
    release = threading.Event()

    class BlockingMemory(FakeMemory):
        def save_chain(self, chain: Any, *, name: str, **kwargs: Any) -> str:
            started.set()
            assert release.wait(timeout=5)
            return super().save_chain(chain, name=name, **kwargs)

    memory = BlockingMemory()
    op = operation(memory=memory)
    waiter = asyncio.create_task(op.save(request()))
    assert await asyncio.to_thread(started.wait, 5)
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter

    replay = asyncio.create_task(op.save(request()))
    release.set()
    receipt = await replay
    assert receipt.replayed is True
    assert len(memory.calls) == 1


@pytest.mark.asyncio
async def test_expired_entry_allows_new_write_after_ttl() -> None:
    now = [10.0]
    memory = FakeMemory()
    op = operation(memory=memory, clock=lambda: now[0], ttl=5.0)

    await op.save(request())
    await op.save(request())
    assert len(memory.calls) == 1

    now[0] = 16.0
    renewed = await op.save(request())
    assert renewed.replayed is False
    assert len(memory.calls) == 2


@pytest.mark.asyncio
async def test_bounded_ledger_rejects_new_key_and_preserves_replay() -> None:
    memory = FakeMemory()
    op = operation(memory=memory, max_entries=1)
    first = await op.save(request("memory-write-0001"))

    with pytest.raises(ChainMemoryWriteOperationError) as caught:
        await op.save(request("memory-write-0002"))
    assert caught.value.failure.code == "idempotency_ledger_full"

    replay = await op.save(request("memory-write-0001"))
    assert replay.entity_id == first.entity_id
    assert len(memory.calls) == 1


def test_constructor_validates_dependencies_and_ledger_bounds() -> None:
    with pytest.raises(TypeError):
        HarnessChainMemoryWriteOperation(memory=object(), artifacts=FakeArtifacts())
    with pytest.raises(TypeError):
        HarnessChainMemoryWriteOperation(memory=FakeMemory(), artifacts=object())
    with pytest.raises(ValueError):
        operation(ttl=0)
    with pytest.raises(ValueError):
        operation(max_entries=0)
