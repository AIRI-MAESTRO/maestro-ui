"""Provider-free tests for the resident harness evolution boundary."""

from __future__ import annotations

import asyncio
import threading
from types import SimpleNamespace
from typing import Any

from pydantic import ValidationError
import pytest

from care.harness_artifacts import JsonArtifactStore
from care.harness_contracts import canonical_sha256
from care.harness_evolution import (
    EvolutionCancelRequest,
    EvolutionChainArtifact,
    EvolutionOperationError,
    EvolutionStartRequest,
    EvolutionStatusRequest,
    HarnessEvolutionOperation,
)
from care.preflight import PreflightResult
from care.runtime.cancellation import CancelledByUserError, CancellationToken


CHAIN = {
    "task_description": "private evolution seed",
    "steps": [
        {
            "number": 1,
            "title": "private title",
            "step_type": "llm",
            "aim": "private aim",
        }
    ],
}


class FakePlatform:
    def __init__(self) -> None:
        self.start_calls: list[dict[str, Any]] = []
        self.status_calls: list[str] = []
        self.cancel_calls: list[str] = []
        self.start_result: Any = SimpleNamespace(
            evolution_id="exp_0001",
            base_chain_id="chain-entity-1",
            status="queued",
        )
        self.status_result: Any = {
            "status": "running",
            "generation": 2,
            "max_iterations": 5,
            "best_fitness": 0.75,
            "individuals": [{"private": "value"}, {"private": "other"}],
        }
        self.cancel_result: Any = {"status": "stopping", "secret": "private"}

    def start_evolution(self, **kwargs: Any) -> Any:
        self.start_calls.append(kwargs)
        return self.start_result

    def get_evolution(self, evolution_id: str) -> dict[str, Any]:
        self.status_calls.append(evolution_id)
        return self.status_result

    def cancel(self, evolution_id: str) -> dict[str, Any]:
        self.cancel_calls.append(evolution_id)
        return self.cancel_result


def artifact_store() -> tuple[JsonArtifactStore, EvolutionChainArtifact]:
    store = JsonArtifactStore(id_factory=lambda: "artifact-seed-1")
    staged = store.stage(CHAIN)
    return store, EvolutionChainArtifact(
        artifact_id=staged.artifact_id,
        content_sha256=staged.content_sha256,
    )


def request(
    reference: EvolutionChainArtifact,
    *,
    key: str = "evolution-start-0001",
    **overrides: Any,
) -> EvolutionStartRequest:
    values: dict[str, Any] = {
        "idempotency_key": key,
        "base_chain_id": "chain-entity-1",
        "chain": reference,
        "evolution_mode": "full_chain",
        "max_iterations": 5,
        "population_size": 8,
        "validation_criteria": "Prefer exact private answers",
        "target_column": "expected",
        "validation_type": "Continuous (0..1)",
        "continuous_metric": "ROUGE-L",
        "binary_method": "equality",
        "regexp_pattern": "",
        "name": "Private evolution",
        "tags": ("harness", "private"),
        "mutation_max_tokens": 4096,
        "validation_max_tokens": 1024,
    }
    values.update(overrides)
    return EvolutionStartRequest(**values)


def admitted_preflight(chain: Any = None, **overrides: Any) -> PreflightResult:
    values: dict[str, Any] = {
        "parsed": True,
        "chain": chain if chain is not None else object(),
        "preflight_available": True,
    }
    values.update(overrides)
    return PreflightResult(**values)


def operation(
    *,
    platform: Any | None = None,
    artifacts: Any | None = None,
    validator: Any | None = None,
    clock: Any | None = None,
    ttl: float = 60.0,
    max_entries: int = 16,
) -> HarnessEvolutionOperation:
    if artifacts is None:
        artifacts, _reference = artifact_store()
    kwargs: dict[str, Any] = {
        "platform": platform or FakePlatform(),
        "artifacts": artifacts,
        "chain_validator": validator
        or (lambda *_args, **_kwargs: admitted_preflight()),
        "idempotency_ttl_seconds": ttl,
        "max_idempotency_entries": max_entries,
    }
    if clock is not None:
        kwargs["clock"] = clock
    return HarnessEvolutionOperation(**kwargs)


def test_request_is_strict_and_has_no_approval_path_or_provider_escape() -> None:
    _store, reference = artifact_store()
    base = request(reference).model_dump(mode="json")

    for forbidden in (
        {"approved": True},
        {"test_data_path": "/tmp/private.jsonl"},
        {"extras": {"llm_model": "untrusted"}},
        {"model": "untrusted"},
    ):
        with pytest.raises(ValidationError):
            EvolutionStartRequest.model_validate({**base, **forbidden})

    from_json = request(reference, tags=["json", "array"])
    assert from_json.tags == ("json", "array")
    with pytest.raises(ValidationError):
        request(reference, tags=["not", 3])
    with pytest.raises(ValidationError):
        request(
            reference,
            validation_type="Binary (0/1)",
            binary_method="regexp",
            regexp_pattern="",
        )
    with pytest.raises(ValidationError):
        request(reference, target_column="../../private")


@pytest.mark.asyncio
async def test_start_resolves_digest_admits_and_uses_public_platform_surface() -> None:
    store, reference = artifact_store()
    platform = FakePlatform()
    captured: dict[str, Any] = {}

    def validator(payload: Any, **kwargs: Any) -> PreflightResult:
        captured["payload"] = payload
        captured["kwargs"] = kwargs
        return admitted_preflight()

    receipt = await operation(
        platform=platform,
        artifacts=store,
        validator=validator,
    ).start(request(reference))

    assert captured == {
        "payload": CHAIN,
        "kwargs": {"strict_references": True},
    }
    assert platform.start_calls == [
        {
            "base_chain_id": "chain-entity-1",
            "base_chain_content": CHAIN,
            "evolution_mode": "full_chain",
            "max_iterations": 5,
            "population_size": 8,
            "validation_criteria": "Prefer exact private answers",
            "target_column": "expected",
            "validation_type": "Continuous (0..1)",
            "continuous_metric": "ROUGE-L",
            "binary_method": "equality",
            "regexp_pattern": "",
            "name": "Private evolution",
            "tags": ["harness", "private"],
            "mutation_max_tokens": 4096,
            "validation_max_tokens": 1024,
        }
    ]
    assert receipt.status == "started"
    assert receipt.evolution_id == "exp_0001"
    assert receipt.base_chain_id == "chain-entity-1"
    assert receipt.chain_digest == reference.content_sha256
    assert receipt.evolution_status == "queued"
    assert receipt.cancellation_dispatched is None
    assert receipt.replayed is False
    public = receipt.model_dump_json()
    assert "private evolution seed" not in public
    assert "Prefer exact private answers" not in public


@pytest.mark.asyncio
async def test_default_strict_carl_admission_accepts_staged_chain() -> None:
    store, reference = artifact_store()
    platform = FakePlatform()
    op = HarnessEvolutionOperation(platform=platform, artifacts=store)

    receipt = await op.start(request(reference))

    assert receipt.evolution_id == "exp_0001"
    assert len(platform.start_calls) == 1


@pytest.mark.asyncio
async def test_artifact_digest_is_rechecked_before_admission() -> None:
    platform = FakePlatform()

    class LyingArtifacts:
        def resolve(self, *_args: Any, **_kwargs: Any) -> dict[str, Any]:
            return CHAIN

    reference = EvolutionChainArtifact(
        artifact_id="artifact-seed-1",
        content_sha256="a" * 64,
    )
    with pytest.raises(EvolutionOperationError) as caught:
        await operation(platform=platform, artifacts=LyingArtifacts()).start(
            request(reference)
        )

    assert caught.value.failure.code == "artifact_unavailable"
    assert platform.start_calls == []
    assert canonical_sha256(CHAIN) != "a" * 64


@pytest.mark.asyncio
async def test_admission_failure_is_redacted_and_never_starts_platform() -> None:
    store, reference = artifact_store()
    platform = FakePlatform()

    def validator(*_args: Any, **_kwargs: Any) -> PreflightResult:
        return PreflightResult(
            parsed=False,
            parse_errors=("private seed cannot be parsed",),
        )

    with pytest.raises(EvolutionOperationError) as caught:
        await operation(
            platform=platform,
            artifacts=store,
            validator=validator,
        ).start(request(reference))

    assert caught.value.failure.code == "chain_admission_failed"
    assert "private seed" not in str(caught.value)
    assert platform.start_calls == []


@pytest.mark.asyncio
async def test_platform_failure_is_redacted_and_cached_as_uncertain_effect() -> None:
    store, reference = artifact_store()
    calls = 0

    class PrivatePlatformFailure(RuntimeError):
        pass

    class FailingPlatform(FakePlatform):
        def start_evolution(self, **kwargs: Any) -> Any:
            nonlocal calls
            calls += 1
            raise PrivatePlatformFailure("token=secret; private seed")

    op = operation(platform=FailingPlatform(), artifacts=store)
    for _attempt in range(2):
        with pytest.raises(EvolutionOperationError) as caught:
            await op.start(request(reference))
        assert caught.value.failure.code == "evolution_start_failed"
        assert caught.value.failure.failure_type == "PrivatePlatformFailure"
        assert "token=secret" not in str(caught.value)
    assert calls == 1


@pytest.mark.asyncio
async def test_start_replay_conflict_and_concurrent_coalescing() -> None:
    store, reference = artifact_store()
    started = threading.Event()
    release = threading.Event()

    class BlockingPlatform(FakePlatform):
        def start_evolution(self, **kwargs: Any) -> Any:
            self.start_calls.append(kwargs)
            started.set()
            assert release.wait(timeout=5)
            return self.start_result

    platform = BlockingPlatform()
    op = operation(platform=platform, artifacts=store)
    first_task = asyncio.create_task(op.start(request(reference)))
    assert await asyncio.to_thread(started.wait, 5)
    second_task = asyncio.create_task(op.start(request(reference)))
    await asyncio.sleep(0)
    release.set()
    first, second = await asyncio.gather(first_task, second_task)

    assert len(platform.start_calls) == 1
    assert first.replayed is False
    assert second.replayed is True
    replay = await op.start(request(reference))
    assert replay.replayed is True
    with pytest.raises(EvolutionOperationError) as caught:
        await op.start(request(reference, max_iterations=6))
    assert caught.value.failure.code == "idempotency_conflict"


@pytest.mark.asyncio
async def test_pre_cancel_stops_before_artifact_or_platform_side_effect() -> None:
    store, reference = artifact_store()
    platform = FakePlatform()
    token = CancellationToken()
    token.cancel()

    with pytest.raises(CancelledByUserError):
        await operation(platform=platform, artifacts=store).start(
            request(reference),
            cancellation=token,
        )
    assert platform.start_calls == []


@pytest.mark.asyncio
async def test_cancel_during_submit_returns_handle_and_dispatches_remote_cancel() -> None:
    store, reference = artifact_store()
    started = threading.Event()
    release = threading.Event()

    class BlockingPlatform(FakePlatform):
        def start_evolution(self, **kwargs: Any) -> Any:
            self.start_calls.append(kwargs)
            started.set()
            assert release.wait(timeout=5)
            return self.start_result

    platform = BlockingPlatform()
    token = CancellationToken()
    op = operation(platform=platform, artifacts=store)
    task = asyncio.create_task(
        op.start(request(reference), cancellation=token)
    )
    assert await asyncio.to_thread(started.wait, 5)
    token.cancel()
    await asyncio.sleep(0)
    release.set()
    receipt = await task

    assert receipt.status == "cancel_requested"
    assert receipt.evolution_id == "exp_0001"
    assert receipt.cancellation_dispatched is True
    assert receipt.evolution_status == "stopping"
    assert platform.cancel_calls == ["exp_0001"]


@pytest.mark.asyncio
async def test_cancel_during_submit_reports_failed_best_effort_without_hiding_id() -> None:
    store, reference = artifact_store()
    started = threading.Event()
    release = threading.Event()

    class CancelFailingPlatform(FakePlatform):
        def start_evolution(self, **kwargs: Any) -> Any:
            self.start_calls.append(kwargs)
            started.set()
            assert release.wait(timeout=5)
            return self.start_result

        def cancel(self, evolution_id: str) -> dict[str, Any]:
            self.cancel_calls.append(evolution_id)
            raise RuntimeError("private remote failure")

    platform = CancelFailingPlatform()
    token = CancellationToken()
    task = asyncio.create_task(
        operation(platform=platform, artifacts=store).start(
            request(reference),
            cancellation=token,
        )
    )
    assert await asyncio.to_thread(started.wait, 5)
    token.cancel()
    await asyncio.sleep(0)
    release.set()
    receipt = await task

    assert receipt.status == "cancel_requested"
    assert receipt.evolution_id == "exp_0001"
    assert receipt.cancellation_dispatched is False
    assert "private remote failure" not in receipt.model_dump_json()


@pytest.mark.asyncio
async def test_cancelled_transport_waiter_does_not_duplicate_remote_start() -> None:
    store, reference = artifact_store()
    started = threading.Event()
    release = threading.Event()

    class BlockingPlatform(FakePlatform):
        def start_evolution(self, **kwargs: Any) -> Any:
            self.start_calls.append(kwargs)
            started.set()
            assert release.wait(timeout=5)
            return self.start_result

    platform = BlockingPlatform()
    op = operation(platform=platform, artifacts=store)
    waiter = asyncio.create_task(op.start(request(reference)))
    assert await asyncio.to_thread(started.wait, 5)
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter

    replay = asyncio.create_task(op.start(request(reference)))
    release.set()
    receipt = await replay
    assert receipt.replayed is True
    assert len(platform.start_calls) == 1


@pytest.mark.asyncio
async def test_status_projects_bounded_fields_and_drops_raw_values() -> None:
    platform = FakePlatform()
    platform.status_result = {
        "status": "completed",
        "generation": 4,
        "max_iterations": 5,
        "best_fitness": 0.875,
        "individuals": [
            {"chain": "private winner"},
            {"chain": "private runner-up"},
        ],
        "results": {"secret": "provider-key"},
        "error_message": "",
    }
    receipt = await operation(platform=platform).status(
        EvolutionStatusRequest(evolution_id="exp_0001")
    )

    assert receipt.status == "completed"
    assert receipt.terminal is True
    assert receipt.generation == 4
    assert receipt.max_iterations == 5
    assert receipt.best_fitness == pytest.approx(0.875)
    assert receipt.individual_count == 2
    assert receipt.error_present is False
    public = receipt.model_dump_json()
    assert "private winner" not in public
    assert "provider-key" not in public
    assert platform.status_calls == ["exp_0001"]


@pytest.mark.asyncio
async def test_status_normalizes_aliases_and_redacts_failures() -> None:
    platform = FakePlatform()
    platform.status_result = {
        "status": "error",
        "generation": True,
        "best_fitness": float("nan"),
        "error_message": "token=private",
    }
    receipt = await operation(platform=platform).status(
        EvolutionStatusRequest(evolution_id="evo-0001")
    )
    assert receipt.status == "failed"
    assert receipt.terminal is True
    assert receipt.generation is None
    assert receipt.best_fitness is None
    assert receipt.error_present is True
    assert "token=private" not in receipt.model_dump_json()

    class PrivateStatusFailure(RuntimeError):
        pass

    def fail(_evolution_id: str) -> dict[str, Any]:
        raise PrivateStatusFailure("secret status response")

    platform.get_evolution = fail  # type: ignore[method-assign]
    with pytest.raises(EvolutionOperationError) as caught:
        await operation(platform=platform).status(
            EvolutionStatusRequest(evolution_id="evo-0001")
        )
    assert caught.value.failure.code == "evolution_status_failed"
    assert caught.value.failure.failure_type == "PrivateStatusFailure"
    assert "secret status" not in str(caught.value)


@pytest.mark.asyncio
async def test_explicit_cancel_is_idempotent_typed_and_value_free() -> None:
    platform = FakePlatform()
    op = operation(platform=platform)
    request_value = EvolutionCancelRequest(
        idempotency_key="evolution-cancel-0001",
        evolution_id="exp_0001",
    )

    first = await op.cancel(request_value)
    replay = await op.cancel(request_value)
    assert first.status == "cancel_requested"
    assert first.cancellation_dispatched is True
    assert first.evolution_status == "stopping"
    assert replay.replayed is True
    assert platform.cancel_calls == ["exp_0001"]
    assert "private" not in first.model_dump_json()

    with pytest.raises(EvolutionOperationError) as caught:
        await op.cancel(
            EvolutionCancelRequest(
                idempotency_key="evolution-cancel-0001",
                evolution_id="exp_0002",
            )
        )
    assert caught.value.failure.code == "idempotency_conflict"


@pytest.mark.asyncio
async def test_explicit_cancel_failure_is_redacted_and_cached() -> None:
    calls = 0

    class PrivateCancelFailure(RuntimeError):
        pass

    class FailingPlatform(FakePlatform):
        def cancel(self, evolution_id: str) -> dict[str, Any]:
            nonlocal calls
            calls += 1
            raise PrivateCancelFailure("secret cancellation response")

    op = operation(platform=FailingPlatform())
    cancel_request = EvolutionCancelRequest(
        idempotency_key="evolution-cancel-0001",
        evolution_id="exp_0001",
    )
    for _attempt in range(2):
        with pytest.raises(EvolutionOperationError) as caught:
            await op.cancel(cancel_request)
        assert caught.value.failure.code == "evolution_cancel_failed"
        assert caught.value.failure.failure_type == "PrivateCancelFailure"
        assert "secret cancellation" not in str(caught.value)
    assert calls == 1


@pytest.mark.asyncio
async def test_ttl_expiry_and_shared_ledger_capacity_are_enforced() -> None:
    store, reference = artifact_store()
    now = [10.0]
    platform = FakePlatform()
    op = operation(
        platform=platform,
        artifacts=store,
        clock=lambda: now[0],
        ttl=5.0,
        max_entries=1,
    )
    await op.start(request(reference))
    with pytest.raises(EvolutionOperationError) as caught:
        await op.cancel(
            EvolutionCancelRequest(
                idempotency_key="evolution-cancel-0001",
                evolution_id="exp_0001",
            )
        )
    assert caught.value.failure.code == "idempotency_ledger_full"

    now[0] = 16.0
    renewed = await op.start(request(reference))
    assert renewed.replayed is False
    assert len(platform.start_calls) == 2


def test_constructor_and_request_boundaries_are_validated() -> None:
    store, reference = artifact_store()
    with pytest.raises(TypeError):
        HarnessEvolutionOperation(platform=object(), artifacts=store)
    with pytest.raises(TypeError):
        HarnessEvolutionOperation(platform=FakePlatform(), artifacts=object())
    with pytest.raises(ValueError):
        operation(ttl=0)
    with pytest.raises(ValueError):
        operation(max_entries=0)
    with pytest.raises(ValidationError):
        EvolutionStatusRequest(evolution_id="../private")
    with pytest.raises(ValidationError):
        EvolutionCancelRequest(
            idempotency_key="short",
            evolution_id="exp_0001",
        )
    with pytest.raises(ValidationError):
        request(reference, approved=True)
