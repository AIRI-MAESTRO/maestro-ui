"""Hermetic tests for the native Production transaction service."""

from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

import pytest

from care.config import StagePolicy
from care.desktop_production import (
    BaselineReceipt,
    DesktopProductionService,
    EvolutionReceipt,
    ProductionCode,
    ProductionPolicies,
    ProductionRequest,
    ProductionStage,
    ProductionStageState,
    ProductionTransactionState,
)
from care.runtime.cancellation import CancellationToken


ALL_AUTO = ProductionPolicies(
    generate=StagePolicy.AUTO,
    save=StagePolicy.AUTO,
    run=StagePolicy.AUTO,
    baseline=StagePolicy.AUTO,
    evolve=StagePolicy.AUTO,
)


@dataclass
class _TypedChain:
    content: dict[str, Any]


class _Memory:
    def __init__(self, calls: list[Any], *, rows: list[Any] | None = None) -> None:
        self.calls = calls
        self.rows = [] if rows is None else rows
        self.saved: dict[str, Any] | None = None
        self.chain_id = "chain-1"
        self.lookup_error: Exception | None = None
        self.on_save: Any = None

    def list_entities(self, **kwargs: Any) -> list[Any]:
        self.calls.append(("lookup", kwargs))
        if self.lookup_error is not None:
            raise self.lookup_error
        return self.rows

    def save_chain(self, chain: Any, **kwargs: Any) -> str:
        self.calls.append(("save", chain, kwargs))
        self.saved = kwargs
        if self.on_save is not None:
            self.on_save()
        return self.chain_id


def _request(*, force: bool = False, secret: str = "TOP_SECRET") -> ProductionRequest:
    prefix = "[FORCE] " if force else ""
    return ProductionRequest(
        private_task=f"{prefix}Analyze <file>{secret}</file>",
        safe_task=f"{prefix}Analyze [attached file: report.csv]",
        context_files=(
            {
                "path": "/private/customer/report.csv",
                "sha256": "a" * 64,
                "size_bytes": 17,
                "mime_type": "text/csv",
                "ignored_body": secret,
            },
        ),
        user_context=f"private instructions {secret}",
        recalled_context=f"private recall {secret}",
    )


def _generated(*, secret: str = "TOP_SECRET") -> SimpleNamespace:
    return SimpleNamespace(
        chain_dict={
            "name": "agent",
            "steps": [{"number": 1, "step_type": "llm", "title": "Answer"}],
        },
        metadata={
            "mode": "deep",
            "model": "vendor/model v1",
            "domain": "analysis",
            "api_key": secret,
            "nested": {"access_token": secret, "safe": "retained"},
        },
    )


def _validator(calls: list[Any]):
    def validate(chain: dict[str, Any]) -> _TypedChain:
        calls.append(("validate", chain))
        return _TypedChain(chain)

    return validate


def _recorder(calls: list[Any]):
    def record(_memory: Any, request: Any) -> BaselineReceipt:
        calls.append(("record", request))
        return BaselineReceipt("card-1", "success", agent_recorded=True)

    return record


@pytest.mark.asyncio
async def test_full_transaction_preserves_privacy_order_and_snapshot_identity() -> None:
    calls: list[Any] = []
    confirmations: list[Any] = []
    memory = _Memory(calls)

    async def generate(request: Any, token: CancellationToken) -> Any:
        calls.append(("generate", request, token))
        assert request.deployable is True
        assert request.private_task.endswith("TOP_SECRET</file>")
        assert request.context_files[0]["path"].startswith("/private/")
        assert "ignored_body" not in request.context_files[0]
        return _generated()

    async def run_baseline(request: Any, _token: CancellationToken) -> Any:
        calls.append(("run", request))
        assert "TOP_SECRET" in request.private_task
        # Mutating the execution snapshot must not change the Platform seed.
        request.chain_dict["steps"][0]["title"] = "MUTATED"
        request.typed_chain.content["name"] = "MUTATED_TYPED"
        return {"success": True, "answer": "ok"}

    def evolve(request: Any) -> EvolutionReceipt:
        calls.append(("evolve", request))
        assert request.base_chain_content["name"] == "agent"
        assert request.base_chain_content["steps"][0]["title"] == "Answer"
        return EvolutionReceipt("evo-1", "queued")

    async def confirm(request: Any) -> bool:
        confirmations.append(request)
        return True

    service = DesktopProductionService(
        memory=memory,
        generate_deployable=generate,
        run_baseline=run_baseline,
        validate_chain=_validator(calls),
        record_baseline=_recorder(calls),
        start_evolution=evolve,
        # Exercise the intended run=ASK confirmation gate.
        policies=ProductionPolicies(),
        confirm=confirm,
        state_sha256=lambda _stage: "b" * 64,
    )

    result = await service.execute(_request())

    assert result.state is ProductionTransactionState.COMPLETED
    assert result.chain_id == "chain-1"
    assert result.baseline == BaselineReceipt("card-1", "success", True)
    assert result.evolution == EvolutionReceipt("evo-1", "queued")
    assert result.revision_pointer is not None
    assert result.revision_pointer.entity_id == "chain-1"
    assert result.revision_pointer.channel == "latest"
    assert result.revision_pointer.version_id is None
    assert [item.stage for item in result.stages] == list(ProductionStage)
    assert all(
        result.stage(stage).state is ProductionStageState.DONE
        for stage in ProductionStage
    )
    assert [item.stage for item in confirmations] == [ProductionStage.RUN]
    assert len(confirmations[0].binding_sha256) == 64
    assert "TOP_SECRET" not in repr(confirmations[0])

    # Typed validation precedes the first Memory lookup/write.  A second parse
    # creates the isolated baseline snapshot.
    names = [item[0] for item in calls]
    assert names.index("validate") < names.index("lookup") < names.index("save")
    assert names.count("validate") == 2
    assert memory.saved is not None
    assert memory.saved["query"] == "Analyze [attached file: report.csv]"
    assert memory.saved["context_files"] == [
        {
            "path": "report.csv",
            "sha256": "a" * 64,
            "size_bytes": 17,
            "mime_type": "text/csv",
        }
    ]
    assert memory.saved["mage_metadata"]["nested"] == {"safe": "retained"}
    assert "api_key" not in memory.saved["mage_metadata"]
    assert memory.saved["tags"] == [
        "source:chat-prod",
        memory.saved["tags"][1],
        "mage:deep",
        "mage-model:vendor-model-v1",
    ]
    assert memory.saved["tags"][1].startswith("task-hash:")

    serialized_events = repr(result.events)
    assert "TOP_SECRET" not in serialized_events
    assert "/private/customer" not in serialized_events


@pytest.mark.asyncio
async def test_typed_validation_failure_never_touches_memory() -> None:
    calls: list[Any] = []
    memory = _Memory(calls)

    def reject(_chain: dict[str, Any]) -> Any:
        calls.append(("validate",))
        raise ValueError("prompt TOP_SECRET caused dependency failure")

    service = DesktopProductionService(
        memory=memory,
        generate_deployable=lambda _request, _token: _generated(),
        run_baseline=lambda _request, _token: {"success": True},
        validate_chain=reject,
        policies=ALL_AUTO,
    )

    result = await service.execute(_request())

    assert result.state is ProductionTransactionState.FAILED
    validation = result.stage(ProductionStage.VALIDATE)
    assert validation.state is ProductionStageState.FAILED
    assert validation.code is ProductionCode.VALIDATION_FAILED
    assert "TOP_SECRET" not in repr(result)
    assert [item[0] for item in calls] == ["validate"]


@pytest.mark.asyncio
async def test_dedup_lookup_failure_is_fail_closed() -> None:
    calls: list[Any] = []
    memory = _Memory(calls)
    memory.lookup_error = RuntimeError("remote echoed TOP_SECRET")
    baseline_calls = 0

    async def baseline(_request: Any, _token: CancellationToken) -> Any:
        nonlocal baseline_calls
        baseline_calls += 1
        return {"success": True}

    service = DesktopProductionService(
        memory=memory,
        generate_deployable=lambda _request, _token: _generated(),
        run_baseline=baseline,
        validate_chain=_validator(calls),
        policies=ALL_AUTO,
    )

    result = await service.execute(_request())

    assert result.state is ProductionTransactionState.FAILED
    assert result.stage(ProductionStage.SAVE).code is ProductionCode.DEDUP_LOOKUP_FAILED
    assert "save" not in [item[0] for item in calls]
    assert baseline_calls == 0
    assert "TOP_SECRET" not in repr(result)


@pytest.mark.asyncio
async def test_duplicate_is_skipped_without_repointing_followups() -> None:
    calls: list[Any] = []
    memory = _Memory(
        calls,
        rows=[{"entity_id": "existing-1", "name": "Existing"}],
    )
    service = DesktopProductionService(
        memory=memory,
        generate_deployable=lambda _request, _token: _generated(),
        run_baseline=lambda _request, _token: pytest.fail("must not run"),
        validate_chain=_validator(calls),
        policies=ALL_AUTO,
    )

    result = await service.execute(_request())

    assert result.state is ProductionTransactionState.SKIPPED
    assert result.stage(ProductionStage.SAVE).code is ProductionCode.DUPLICATE
    assert result.duplicate is not None
    assert result.duplicate.entity_id == "existing-1"
    assert result.chain_id is None
    assert result.revision_pointer is None


@pytest.mark.asyncio
async def test_force_bypasses_dedup_and_marker_is_not_persisted() -> None:
    calls: list[Any] = []
    memory = _Memory(calls, rows=[{"entity_id": "existing-1"}])
    recorded: list[Any] = []

    def record(_memory: Any, request: Any) -> BaselineReceipt:
        recorded.append(request)
        return BaselineReceipt("card-1", "success")

    service = DesktopProductionService(
        memory=memory,
        generate_deployable=lambda _request, _token: _generated(),
        run_baseline=lambda _request, _token: {"success": True},
        validate_chain=_validator(calls),
        record_baseline=record,
        policies=ALL_AUTO,
    )

    result = await service.execute(_request(force=True))

    assert result.state is ProductionTransactionState.COMPLETED
    assert not any(item[0] == "lookup" for item in calls)
    assert memory.saved is not None
    assert memory.saved["query"] == "Analyze [attached file: report.csv]"
    assert recorded[0].safe_task == "Analyze [attached file: report.csv]"


@pytest.mark.asyncio
async def test_declined_run_keeps_saved_revision_pointer_and_stops_baseline() -> None:
    calls: list[Any] = []
    memory = _Memory(calls)

    async def decline(request: Any) -> bool:
        assert request.stage is ProductionStage.RUN
        return False

    service = DesktopProductionService(
        memory=memory,
        generate_deployable=lambda _request, _token: _generated(),
        run_baseline=lambda _request, _token: pytest.fail("must not run"),
        validate_chain=_validator(calls),
        policies=ProductionPolicies(),
        confirm=decline,
        state_sha256=lambda _stage: "c" * 64,
    )

    result = await service.execute(_request())

    assert result.state is ProductionTransactionState.PARTIAL
    assert result.committed is True
    assert result.revision_pointer is not None
    assert result.stage(ProductionStage.RUN).code is ProductionCode.DECLINED
    assert result.stage(ProductionStage.BASELINE).code is ProductionCode.DEPENDENCY


@pytest.mark.asyncio
async def test_confirmation_rechecks_provider_tool_state() -> None:
    calls: list[Any] = []
    memory = _Memory(calls)
    states = iter(("d" * 64, "e" * 64))

    service = DesktopProductionService(
        memory=memory,
        generate_deployable=lambda _request, _token: _generated(),
        run_baseline=lambda _request, _token: pytest.fail("must not run"),
        validate_chain=_validator(calls),
        policies=ProductionPolicies(),
        confirm=lambda _request: True,
        state_sha256=lambda _stage: next(states),
    )

    result = await service.execute(_request())

    assert result.state is ProductionTransactionState.PARTIAL
    run = result.stage(ProductionStage.RUN)
    assert run.state is ProductionStageState.FAILED
    assert run.code is ProductionCode.CONFIRMATION_STATE_CHANGED
    assert result.revision_pointer is not None


@pytest.mark.asyncio
async def test_ask_without_bound_confirmer_fails_closed_after_save() -> None:
    calls: list[Any] = []
    memory = _Memory(calls)
    service = DesktopProductionService(
        memory=memory,
        generate_deployable=lambda _request, _token: _generated(),
        run_baseline=lambda _request, _token: pytest.fail("must not run"),
        validate_chain=_validator(calls),
        policies=ProductionPolicies(),
    )

    result = await service.execute(_request())

    assert result.state is ProductionTransactionState.PARTIAL
    assert result.stage(ProductionStage.RUN).code is ProductionCode.CONFIRMATION_UNAVAILABLE
    assert result.chain_id == "chain-1"


@pytest.mark.asyncio
async def test_cancellation_before_generation_has_no_effects() -> None:
    calls: list[Any] = []
    memory = _Memory(calls)
    token = CancellationToken()
    token.cancel()
    service = DesktopProductionService(
        memory=memory,
        generate_deployable=lambda _request, _token: pytest.fail("must not generate"),
        run_baseline=lambda _request, _token: pytest.fail("must not run"),
        validate_chain=_validator(calls),
        policies=ALL_AUTO,
    )

    result = await service.execute(_request(), cancellation=token)

    assert result.state is ProductionTransactionState.CANCELLED
    assert result.stage(ProductionStage.GENERATE).state is ProductionStageState.CANCELLED
    assert result.chain_id is None
    assert not calls


@pytest.mark.asyncio
async def test_cancellation_during_sync_save_preserves_commit_and_pointer() -> None:
    calls: list[Any] = []
    memory = _Memory(calls)
    token = CancellationToken()
    memory.on_save = token.cancel
    service = DesktopProductionService(
        memory=memory,
        generate_deployable=lambda _request, _token: _generated(),
        run_baseline=lambda _request, _token: pytest.fail("must not run"),
        validate_chain=_validator(calls),
        policies=ALL_AUTO,
    )

    result = await service.execute(_request(), cancellation=token)

    assert result.state is ProductionTransactionState.CANCELLED
    assert result.stage(ProductionStage.SAVE).state is ProductionStageState.DONE
    assert result.stage(ProductionStage.RUN).state is ProductionStageState.CANCELLED
    assert result.chain_id == "chain-1"
    assert result.revision_pointer is not None


@pytest.mark.asyncio
@pytest.mark.parametrize("run_result", [None, {"success": False}])
async def test_failed_baseline_is_not_recorded_or_evolved(run_result: Any) -> None:
    calls: list[Any] = []
    memory = _Memory(calls)
    service = DesktopProductionService(
        memory=memory,
        generate_deployable=lambda _request, _token: _generated(),
        run_baseline=lambda _request, _token: run_result,
        validate_chain=_validator(calls),
        record_baseline=lambda _memory, _request: pytest.fail("must not record"),
        start_evolution=lambda _request: pytest.fail("must not evolve"),
        policies=ALL_AUTO,
    )

    result = await service.execute(_request())

    assert result.state is ProductionTransactionState.PARTIAL
    assert result.stage(ProductionStage.RUN).code is ProductionCode.RUN_FAILED
    assert result.stage(ProductionStage.BASELINE).code is ProductionCode.DEPENDENCY
    assert result.stage(ProductionStage.EVOLVE).code is ProductionCode.DEPENDENCY
    assert result.revision_pointer is not None


@pytest.mark.asyncio
async def test_missing_platform_is_truthfully_skipped_and_transaction_completes() -> None:
    calls: list[Any] = []
    memory = _Memory(calls)
    service = DesktopProductionService(
        memory=memory,
        generate_deployable=lambda _request, _token: _generated(),
        run_baseline=lambda _request, _token: {"success": True},
        validate_chain=_validator(calls),
        record_baseline=_recorder(calls),
        start_evolution=None,
        policies=ALL_AUTO,
    )

    result = await service.execute(_request())

    assert result.state is ProductionTransactionState.COMPLETED
    evolve = result.stage(ProductionStage.EVOLVE)
    assert evolve.state is ProductionStageState.SKIPPED
    assert evolve.code is ProductionCode.PLATFORM_UNAVAILABLE


@pytest.mark.asyncio
async def test_platform_failure_is_partial_without_rollback() -> None:
    calls: list[Any] = []
    memory = _Memory(calls)

    def fail_platform(_request: Any) -> Any:
        raise RuntimeError("Platform echoed TOP_SECRET")

    service = DesktopProductionService(
        memory=memory,
        generate_deployable=lambda _request, _token: _generated(),
        run_baseline=lambda _request, _token: {"success": True},
        validate_chain=_validator(calls),
        record_baseline=_recorder(calls),
        start_evolution=fail_platform,
        policies=ALL_AUTO,
    )

    result = await service.execute(_request())

    assert result.state is ProductionTransactionState.PARTIAL
    assert result.chain_id == "chain-1"
    assert result.baseline is not None
    assert result.stage(ProductionStage.EVOLVE).code is ProductionCode.EVOLUTION_FAILED
    assert "TOP_SECRET" not in repr(result)


def test_request_repr_hides_private_and_persistence_fields() -> None:
    request = _request(secret="NEVER_PRINT_ME")

    assert "NEVER_PRINT_ME" not in repr(request)
    assert "/private/customer" not in repr(request)


@pytest.mark.asyncio
async def test_invalid_context_identity_is_rejected_before_callbacks() -> None:
    calls: list[Any] = []
    memory = _Memory(calls)
    bad = ProductionRequest(
        private_task="private",
        safe_task="safe",
        context_files=(
            {"path": "/tmp/x", "sha256": "bad", "size_bytes": 1},
        ),
    )
    service = DesktopProductionService(
        memory=memory,
        generate_deployable=lambda _request, _token: pytest.fail("must not generate"),
        run_baseline=lambda _request, _token: pytest.fail("must not run"),
        validate_chain=_validator(calls),
        policies=ALL_AUTO,
    )

    with pytest.raises(ValueError, match="lowercase sha256"):
        await service.execute(bad)

    assert not calls
