from __future__ import annotations

import pytest

from care.harness_artifacts import (
    ArtifactUnavailableError,
    HarnessArtifactError,
    JsonArtifactStore,
)


def test_receipt_exposes_shape_and_hash_but_not_values() -> None:
    store = JsonArtifactStore(id_factory=lambda: "artifact-1")
    private_value = "private-customer-value"

    receipt = store.stage(
        {"customer": {"name": private_value}, "orders": [3, 5]},
    )

    assert receipt.artifact_id == "artifact-1"
    assert receipt.leaf_pointers == ("/customer/name", "/orders")
    assert private_value not in receipt.model_dump_json()
    assert (
        store.resolve(
            receipt.artifact_id,
            content_sha256=receipt.content_sha256,
        )["customer"]["name"]
        == private_value
    )


def test_payload_is_copied_on_ingress_and_egress() -> None:
    source = {"nested": {"value": 1}}
    store = JsonArtifactStore(id_factory=lambda: "artifact-1")
    receipt = store.stage(source)
    source["nested"]["value"] = 2

    first = store.resolve("artifact-1", content_sha256=receipt.content_sha256)
    first["nested"]["value"] = 3
    second = store.resolve("artifact-1", content_sha256=receipt.content_sha256)

    assert second == {"nested": {"value": 1}}


def test_wrong_digest_and_expiry_fail_closed() -> None:
    now = 10.0
    store = JsonArtifactStore(
        ttl_seconds=5,
        clock=lambda: now,
        id_factory=lambda: "artifact-1",
    )
    receipt = store.stage({"value": 1})

    with pytest.raises(ArtifactUnavailableError, match="changed"):
        store.resolve("artifact-1", content_sha256="0" * 64)

    now = 16.0
    with pytest.raises(ArtifactUnavailableError, match="missing or expired"):
        store.resolve("artifact-1", content_sha256=receipt.content_sha256)


def test_store_evicts_oldest_to_stay_bounded() -> None:
    ids = iter(("artifact-1", "artifact-2"))
    store = JsonArtifactStore(max_entries=1, id_factory=lambda: next(ids))
    first = store.stage({"value": 1})
    second = store.stage({"value": 2})

    with pytest.raises(ArtifactUnavailableError):
        store.inspect(first.artifact_id)
    assert store.inspect(second.artifact_id) == second


@pytest.mark.parametrize("payload", [[1, 2], "text", float("nan")])
def test_p0_only_accepts_object_root_finite_json(payload: object) -> None:
    store = JsonArtifactStore()

    with pytest.raises(HarnessArtifactError):
        store.stage(payload)


def test_discard_is_idempotent() -> None:
    store = JsonArtifactStore(id_factory=lambda: "artifact-1")
    store.stage({"value": 1})

    assert store.discard("artifact-1") is True
    assert store.discard("artifact-1") is False
