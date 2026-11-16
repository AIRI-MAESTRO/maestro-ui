from __future__ import annotations

import json

import pytest

from care.harness_contracts import (
    AdaptationPreviewStore,
    InvalidInputContractError,
    JsonPointerBinding,
    MissingInputContractError,
    PreviewUnavailableError,
    chain_input_contract,
)


def _chain(schema: dict | None = None) -> dict:
    care: dict = {}
    if schema is not None:
        care["runtime_input_schema"] = schema
    return {
        "format_version": 1,
        "steps": [{"number": 1, "title": "Use runtime input"}],
        "metadata": {"care": care},
    }


def _schema() -> dict:
    return {
        "type": "object",
        "properties": {
            "customer": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "orders": {"type": "array", "items": {"type": "integer"}},
                },
                "required": ["name", "orders"],
                "additionalProperties": False,
            },
        },
        "required": ["customer"],
        "additionalProperties": False,
    }


def test_chain_input_contract_requires_an_explicit_schema() -> None:
    with pytest.raises(MissingInputContractError):
        chain_input_contract("chain-1", _chain())


def test_chain_input_contract_normalizes_dialect_and_binds_revision() -> None:
    chain = _chain(_schema())

    contract = chain_input_contract("chain-1", chain)

    assert contract.chain_id == "chain-1"
    assert contract.schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    assert len(contract.chain_digest) == 64
    assert len(contract.schema_digest) == 64
    detached = contract.schema
    detached["properties"] = {}
    assert contract.schema["properties"] != {}


def test_chain_input_contract_rejects_non_object_p0_schema() -> None:
    with pytest.raises(InvalidInputContractError, match="object-root"):
        chain_input_contract("chain-1", _chain({"type": "string"}))


def test_lossless_preview_maps_names_and_keeps_values_private() -> None:
    contract = chain_input_contract("chain-1", _chain(_schema()))
    store = AdaptationPreviewStore(id_factory=lambda: "preview-1")
    secret_name = "private-customer-name"
    source = {"client_name": secret_name, "order_ids": [3, 5]}

    preview = store.preview(
        contract,
        source,
        [
            JsonPointerBinding(source="/client_name", target="/customer/name"),
            JsonPointerBinding(source="/order_ids", target="/customer/orders"),
        ],
    )

    assert preview.status == "ready"
    assert preview.preview_id == "preview-1"
    assert preview.issues == ()
    assert secret_name not in preview.model_dump_json()

    payload = store.consume(
        "preview-1",
        chain_digest=contract.chain_digest,
        schema_digest=contract.schema_digest,
        source_digest=preview.source_digest,
        confirmation_digest=preview.confirmation_digest or "",
    )
    assert payload == {
        "customer": {"name": secret_name, "orders": [3, 5]},
    }
    with pytest.raises(PreviewUnavailableError, match="already used"):
        store.consume(
            "preview-1",
            chain_digest=contract.chain_digest,
            schema_digest=contract.schema_digest,
            source_digest=preview.source_digest,
            confirmation_digest=preview.confirmation_digest or "",
        )


def test_preview_refuses_implicit_drop_and_is_not_consumable() -> None:
    contract = chain_input_contract("chain-1", _chain(_schema()))
    store = AdaptationPreviewStore()

    preview = store.preview(
        contract,
        {
            "client_name": "Ada",
            "order_ids": [1],
            "unused_private_field": "must not be silently dropped",
        },
        [
            JsonPointerBinding(source="/client_name", target="/customer/name"),
            JsonPointerBinding(source="/order_ids", target="/customer/orders"),
        ],
    )

    assert preview.status == "needs_input"
    assert preview.preview_id is None
    assert [(issue.code, issue.pointer) for issue in preview.issues] == [
        ("unmapped_source", "/unused_private_field"),
    ]


def test_preview_schema_failure_reports_no_rejected_value() -> None:
    contract = chain_input_contract("chain-1", _chain(_schema()))
    store = AdaptationPreviewStore()
    rejected_value = "not-an-integer-array"

    preview = store.preview(
        contract,
        {"client_name": "Ada", "order_ids": rejected_value},
        [
            JsonPointerBinding(source="/client_name", target="/customer/name"),
            JsonPointerBinding(source="/order_ids", target="/customer/orders"),
        ],
    )

    assert preview.status == "needs_input"
    assert any(
        issue.code == "schema_validation"
        and issue.pointer == "/customer/orders"
        and issue.keyword == "type"
        for issue in preview.issues
    )
    assert rejected_value not in preview.model_dump_json()


def test_preview_rejects_overlapping_source_moves() -> None:
    contract = chain_input_contract("chain-1", _chain(_schema()))
    store = AdaptationPreviewStore()

    preview = store.preview(
        contract,
        {"source": {"name": "Ada", "orders": [1]}},
        [
            JsonPointerBinding(source="/source", target="/customer"),
            JsonPointerBinding(source="/source/name", target="/customer/name"),
        ],
    )

    assert preview.status == "needs_input"
    assert {issue.code for issue in preview.issues} == {
        "overlapping_source",
        "overlapping_target",
    }


def test_preview_remains_available_after_wrong_revision_check() -> None:
    contract = chain_input_contract("chain-1", _chain(_schema()))
    store = AdaptationPreviewStore(id_factory=lambda: "preview-1")
    preview = store.preview(
        contract,
        {"name": "Ada", "orders": [1]},
        [
            JsonPointerBinding(source="/name", target="/customer/name"),
            JsonPointerBinding(source="/orders", target="/customer/orders"),
        ],
    )
    assert preview.status == "ready"

    with pytest.raises(PreviewUnavailableError, match="chain changed"):
        store.consume(
            "preview-1",
            chain_digest="0" * 64,
            schema_digest=contract.schema_digest,
            source_digest=preview.source_digest,
            confirmation_digest=preview.confirmation_digest or "",
        )

    assert (
        store.consume(
            "preview-1",
            chain_digest=contract.chain_digest,
            schema_digest=contract.schema_digest,
            source_digest=preview.source_digest,
            confirmation_digest=preview.confirmation_digest or "",
        )["customer"]["name"]
        == "Ada"
    )


def test_preview_expires_and_drops_private_payload() -> None:
    now = 10.0
    store = AdaptationPreviewStore(
        ttl_seconds=5,
        clock=lambda: now,
        id_factory=lambda: "preview-1",
    )
    contract = chain_input_contract("chain-1", _chain(_schema()))
    preview = store.preview(
        contract,
        {"name": "Ada", "orders": [1]},
        [
            JsonPointerBinding(source="/name", target="/customer/name"),
            JsonPointerBinding(source="/orders", target="/customer/orders"),
        ],
    )
    assert preview.expires_at_unix == 15.0

    now = 16.0
    with pytest.raises(PreviewUnavailableError, match="expired"):
        store.consume(
            "preview-1",
            chain_digest=contract.chain_digest,
            schema_digest=contract.schema_digest,
            source_digest=preview.source_digest,
            confirmation_digest=preview.confirmation_digest or "",
        )


def test_wrong_confirmation_binding_does_not_consume_preview() -> None:
    contract = chain_input_contract("chain-1", _chain(_schema()))
    store = AdaptationPreviewStore(id_factory=lambda: "preview-1")
    preview = store.preview(
        contract,
        {"name": "Ada", "orders": [1]},
        [
            JsonPointerBinding(source="/name", target="/customer/name"),
            JsonPointerBinding(source="/orders", target="/customer/orders"),
        ],
    )

    with pytest.raises(PreviewUnavailableError, match="binding is invalid"):
        store.consume(
            "preview-1",
            chain_digest=contract.chain_digest,
            schema_digest=contract.schema_digest,
            source_digest=preview.source_digest,
            confirmation_digest="0" * 64,
        )

    assert (
        store.consume(
            "preview-1",
            chain_digest=contract.chain_digest,
            schema_digest=contract.schema_digest,
            source_digest=preview.source_digest,
            confirmation_digest=preview.confirmation_digest or "",
        )["customer"]["name"]
        == "Ada"
    )


def test_preview_receipt_is_plain_json_serializable() -> None:
    contract = chain_input_contract("chain-1", _chain(_schema()))
    store = AdaptationPreviewStore(id_factory=lambda: "preview-1")
    preview = store.preview(
        contract,
        {"name": "Ada", "orders": [1]},
        [
            JsonPointerBinding(source="/name", target="/customer/name"),
            JsonPointerBinding(source="/orders", target="/customer/orders"),
        ],
    )

    assert json.loads(preview.model_dump_json())["status"] == "ready"
