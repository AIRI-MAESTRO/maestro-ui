from __future__ import annotations

import json

import pytest

from care.harness_artifacts import ArtifactUnavailableError, JsonArtifactStore
from care.harness_contracts import AdaptationPreviewStore, JsonPointerBinding
from care.harness_service import HarnessOrchestrationService, HarnessServiceError
from care.runtime.cancellation import CancellationToken, CancelledByUserError


def _schema() -> dict:
    return {
        "type": "object",
        "properties": {"city": {"type": "string"}},
        "required": ["city"],
        "additionalProperties": False,
    }


def _chain(schema: dict | None = None) -> dict:
    care = {} if schema is None else {"runtime_input_schema": schema}
    return {
        "format_version": 1,
        "steps": [{"number": 1, "title": "Use city"}],
        "metadata": {"care": care},
    }


def _service(chains: dict[str, dict]) -> HarnessOrchestrationService:
    return HarnessOrchestrationService(
        chain_loader=lambda chain_id, _channel: chains[chain_id],
        artifacts=JsonArtifactStore(id_factory=lambda: "artifact-1"),
        previews=AdaptationPreviewStore(id_factory=lambda: "preview-1"),
    )


def test_inspection_and_preview_never_expose_source_values() -> None:
    service = _service({"chain-1": _chain(_schema())})
    private_value = "private-moscow-value"
    source = service.stage_input({"town": private_value})

    inspection = service.inspect_adaptation("chain-1", source.artifact_id)
    result = service.preview_adaptation(
        "chain-1",
        source.artifact_id,
        artifact_sha256=source.content_sha256,
        bindings=[JsonPointerBinding(source="/town", target="/city")],
    )

    assert inspection.status == "planning_required"
    assert inspection.source.leaf_pointers == ("/town",)
    assert result.status == "ready"
    assert result.preview is not None
    assert result.preview.confirmation_digest
    assert private_value not in inspection.model_dump_json()
    assert private_value not in result.model_dump_json()
    json.loads(result.model_dump_json())


def test_missing_contract_is_explicit_and_does_not_guess() -> None:
    service = _service({"legacy": _chain()})
    source = service.stage_input({"town": "Moscow"})

    result = service.preview_adaptation(
        "legacy",
        source.artifact_id,
        artifact_sha256=source.content_sha256,
        bindings=[JsonPointerBinding(source="/town", target="/city")],
    )

    assert result.status == "contract_unavailable"
    assert result.preview is None
    assert "explicit runtime input contract" in (
        result.inspection.chain.diagnostic or ""
    )


def test_schema_coercion_and_hidden_drop_are_refused() -> None:
    service = _service({"chain-1": _chain(_schema())})
    source = service.stage_input({"town": 7, "hidden": "keep me"})

    result = service.preview_adaptation(
        "chain-1",
        source.artifact_id,
        artifact_sha256=source.content_sha256,
        bindings=[JsonPointerBinding(source="/town", target="/city")],
    )

    assert result.status == "needs_input"
    assert result.preview is not None
    codes = {issue.code for issue in result.preview.issues}
    assert codes == {"schema_validation", "unmapped_source"}


def test_artifact_digest_is_pinned() -> None:
    service = _service({"chain-1": _chain(_schema())})
    source = service.stage_input({"town": "Moscow"})

    with pytest.raises(ArtifactUnavailableError, match="changed"):
        service.preview_adaptation(
            "chain-1",
            source.artifact_id,
            artifact_sha256="0" * 64,
            bindings=[JsonPointerBinding(source="/town", target="/city")],
        )


def test_chain_moving_between_inspection_and_preview_fails_closed() -> None:
    first = _chain(_schema())
    second = {**_chain(_schema()), "revision_marker": "changed"}
    calls = 0

    def load(_chain_id: str, _channel: str) -> dict:
        nonlocal calls
        calls += 1
        return first if calls == 1 else second

    service = HarnessOrchestrationService(
        chain_loader=load,
        artifacts=JsonArtifactStore(id_factory=lambda: "artifact-1"),
    )
    source = service.stage_input({"town": "Moscow"})

    with pytest.raises(HarnessServiceError, match="changed during"):
        service.preview_adaptation(
            "chain-1",
            source.artifact_id,
            artifact_sha256=source.content_sha256,
            bindings=[JsonPointerBinding(source="/town", target="/city")],
        )


def test_cancellation_is_checked_before_chain_lookup() -> None:
    called = False

    def load(_chain_id: str, _channel: str) -> dict:
        nonlocal called
        called = True
        return _chain(_schema())

    service = HarnessOrchestrationService(chain_loader=load)
    token = CancellationToken(reason="steered away")
    token.cancel()

    with pytest.raises(CancelledByUserError, match="steered away"):
        service.inspect_contract("chain-1", cancellation=token)
    assert called is False
