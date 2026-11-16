from __future__ import annotations

import json
import multiprocessing
from pathlib import Path
import stat
from typing import Any

import pytest

from care.harness_shared_artifacts import (
    AT_REST_PROTECTION,
    SharedArtifactStorageError,
    SharedArtifactUnavailableError,
    SharedArtifactValidationError,
    SharedJsonArtifactStore,
)


def _stage_in_process(
    root: str,
    session_id: str,
    worker_number: int,
    output: Any,
) -> None:
    store = SharedJsonArtifactStore(
        root=root,
        session_id=session_id,
        max_entries=16,
    )
    receipt = store.stage({"worker": worker_number})
    output.put(receipt.model_dump(mode="json"))


def test_store_is_shared_without_putting_values_in_receipts(tmp_path: Path) -> None:
    host = SharedJsonArtifactStore(
        root=tmp_path / "shared",
        session_id="session-1",
        id_factory=lambda: "artifact-1",
    )
    private_value = "private-customer-value"
    receipt = host.stage(
        {"customer": {"name": private_value}, "orders": [3, 5]},
    )

    subprocess_view = SharedJsonArtifactStore(
        root=tmp_path / "shared",
        session_id="session-1",
    )
    assert subprocess_view.inspect(receipt.artifact_id) == receipt
    assert subprocess_view.resolve(
        receipt.artifact_id,
        content_sha256=receipt.content_sha256,
    ) == {"customer": {"name": private_value}, "orders": [3, 5]}
    assert private_value not in receipt.model_dump_json()
    assert receipt.leaf_pointers == ("/customer/name", "/orders")


def test_storage_permissions_and_plaintext_boundary_are_explicit(
    tmp_path: Path,
) -> None:
    root = tmp_path / "shared"
    store = SharedJsonArtifactStore(root=root, session_id="session-1")
    store.stage({"value": "sensitive"})

    assert stat.S_IMODE(root.stat().st_mode) == 0o700
    assert stat.S_IMODE((root / "sessions").stat().st_mode) == 0o700
    assert stat.S_IMODE(store.database_path.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(store.database_path.stat().st_mode) == 0o600
    assert store.at_rest_protection == AT_REST_PROTECTION
    assert store.at_rest_protection == "permissions-only-plaintext"


@pytest.mark.parametrize(
    "payload",
    (
        [1, 2],
        "text",
        {1: "non-string-key"},
        {"tuple": (1, 2)},
        {"not_finite": float("nan")},
    ),
)
def test_only_canonical_finite_object_root_json_is_accepted(
    tmp_path: Path,
    payload: object,
) -> None:
    store = SharedJsonArtifactStore(root=tmp_path / "shared", session_id="session-1")

    with pytest.raises(SharedArtifactValidationError):
        store.stage(payload)  # type: ignore[arg-type]


def test_non_finite_expiration_is_rejected_without_writing(tmp_path: Path) -> None:
    store = SharedJsonArtifactStore(
        root=tmp_path / "shared",
        session_id="session-1",
        ttl_seconds=1e308,
        clock=lambda: 1e308,
    )

    with pytest.raises(SharedArtifactValidationError, match="expiration"):
        store.stage({"value": 1})

    assert store.stats().entry_count == 0


def test_ingress_and_egress_are_detached_and_digest_pinned(tmp_path: Path) -> None:
    source = {"nested": {"value": 1}}
    store = SharedJsonArtifactStore(
        root=tmp_path / "shared",
        session_id="session-1",
        id_factory=lambda: "artifact-1",
    )
    receipt = store.stage(source)
    source["nested"]["value"] = 2

    with pytest.raises(SharedArtifactUnavailableError, match="changed"):
        store.resolve(receipt.artifact_id, content_sha256="0" * 64)

    first = store.resolve(
        receipt.artifact_id,
        content_sha256=receipt.content_sha256,
    )
    first["nested"]["value"] = 3
    assert store.resolve(
        receipt.artifact_id,
        content_sha256=receipt.content_sha256,
    ) == {"nested": {"value": 1}}


def test_ttl_cleanup_and_discard_are_session_scoped(tmp_path: Path) -> None:
    now = 10.0
    root = tmp_path / "shared"
    first = SharedJsonArtifactStore(
        root=root,
        session_id="session-1",
        ttl_seconds=5,
        clock=lambda: now,
        id_factory=lambda: "same-id",
    )
    second = SharedJsonArtifactStore(
        root=root,
        session_id="session-2",
        ttl_seconds=5,
        clock=lambda: now,
        id_factory=lambda: "same-id",
    )
    first_receipt = first.stage({"owner": "first"})
    second_receipt = second.stage({"owner": "second"})
    assert first.database_path != second.database_path

    assert first.discard(first_receipt.artifact_id) is True
    assert first.discard(first_receipt.artifact_id) is False
    assert second.stats().entry_count == 1

    now = 16.0
    cleanup = second.cleanup()
    assert cleanup.removed_entries == 1
    assert cleanup.removed_bytes == second_receipt.size_bytes
    assert cleanup.remaining_entries == 0
    with pytest.raises(SharedArtifactUnavailableError, match="missing or expired"):
        second.resolve(
            second_receipt.artifact_id,
            content_sha256=second_receipt.content_sha256,
        )


def test_entry_and_total_byte_bounds_evict_oldest_atomically(tmp_path: Path) -> None:
    now = 10.0
    ids = iter(("artifact-1", "artifact-2", "artifact-3"))
    store = SharedJsonArtifactStore(
        root=tmp_path / "shared",
        session_id="session-1",
        max_entries=2,
        max_payload_bytes=1024,
        max_total_bytes=1024,
        clock=lambda: now,
        id_factory=lambda: next(ids),
    )
    first = store.stage({"payload": "a" * 600})
    now = 11.0
    second = store.stage({"payload": "b" * 600})
    assert store.stats().entry_count == 1
    with pytest.raises(SharedArtifactUnavailableError):
        store.inspect(first.artifact_id)
    assert store.inspect(second.artifact_id) == second

    now = 12.0
    third = store.stage({"payload": "small"})
    assert store.stats().entry_count == 2
    assert store.inspect(third.artifact_id) == third


def test_multiple_processes_can_stage_into_one_session(tmp_path: Path) -> None:
    context = multiprocessing.get_context("spawn")
    output = context.Queue()
    processes = [
        context.Process(
            target=_stage_in_process,
            args=(str(tmp_path / "shared"), "session-1", number, output),
        )
        for number in range(4)
    ]

    for process in processes:
        process.start()
    receipts = [output.get(timeout=15) for _process in processes]
    for process in processes:
        process.join(timeout=15)
        assert process.exitcode == 0

    reader = SharedJsonArtifactStore(
        root=tmp_path / "shared",
        session_id="session-1",
        max_entries=16,
    )
    assert reader.stats().entry_count == 4
    resolved_workers = {
        reader.resolve(
            receipt["artifact_id"],
            content_sha256=receipt["content_sha256"],
        )["worker"]
        for receipt in receipts
    }
    assert resolved_workers == {0, 1, 2, 3}


def test_clear_returns_only_counts_and_removes_all_session_rows(
    tmp_path: Path,
) -> None:
    store = SharedJsonArtifactStore(root=tmp_path / "shared", session_id="session-1")
    first = store.stage({"secret": "one"})
    second = store.stage({"secret": "two"})

    receipt = store.clear()

    encoded = receipt.model_dump_json()
    assert receipt.removed_entries == 2
    assert receipt.removed_bytes == first.size_bytes + second.size_bytes
    assert "one" not in encoded
    assert "two" not in encoded
    assert store.stats().entry_count == 0


def test_session_id_is_validated_before_building_a_path(tmp_path: Path) -> None:
    with pytest.raises(SharedArtifactValidationError):
        SharedJsonArtifactStore(root=tmp_path / "shared", session_id="../escape")


def test_symlink_root_is_refused(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(target, target_is_directory=True)

    with pytest.raises(SharedArtifactStorageError, match="symlink"):
        SharedJsonArtifactStore(root=alias, session_id="session-1")


def test_receipts_are_json_serializable_without_private_payloads(
    tmp_path: Path,
) -> None:
    store = SharedJsonArtifactStore(root=tmp_path / "shared", session_id="session-1")
    secret = "not-for-agent-log"
    staged = store.stage({"secret": secret})

    encoded = json.dumps(staged.model_dump(mode="json"), sort_keys=True)

    assert secret not in encoded


def test_default_artifact_ids_always_satisfy_the_public_grammar(
    tmp_path: Path,
) -> None:
    store = SharedJsonArtifactStore(
        root=tmp_path / "shared",
        session_id="session-default-ids",
        max_entries=256,
    )

    ids = [store.stage({"index": index}).artifact_id for index in range(128)]

    assert len(set(ids)) == 128
    assert all(value[0].isalnum() for value in ids)
    assert all(value.isalnum() for value in ids)
