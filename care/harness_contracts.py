"""Typed runtime-input contracts and lossless adaptation previews.

The resident chat agent may propose how a user payload maps onto a saved CARL
chain, but it must not silently rewrite the arguments at execution time.  This
module turns that proposal into a state-bound, single-use preview receipt.  Raw
values stay in the private preview store and never appear in the receipt.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from functools import wraps
import hashlib
import hmac
import json
import math
import secrets
import threading
import time
from typing import Any, Literal

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError
from pydantic import BaseModel, ConfigDict, Field, field_validator


JSON_SCHEMA_DIALECT = "https://json-schema.org/draft/2020-12/schema"
CARE_METADATA_KEY = "care"
RUNTIME_INPUT_SCHEMA_KEY = "runtime_input_schema"
ADAPTER_VERSION = "care.lossless-json-pointer.v1"
LOSSLESS_POLICY_DIGEST = hashlib.sha256(
    b'{"arrays":"atomic","loss":"deny","operations":["move"]}'
).hexdigest()


class HarnessContractError(RuntimeError):
    """Base error for a refused harness input-contract operation."""


class MissingInputContractError(HarnessContractError):
    """Raised when a chain has no explicit runtime input contract."""


class InvalidInputContractError(HarnessContractError):
    """Raised when a chain's declared JSON Schema is invalid or unsupported."""


class PreviewUnavailableError(HarnessContractError):
    """Raised when an adaptation preview cannot be consumed."""


def _canonical_json(value: Any) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError) as exc:
        raise InvalidInputContractError(
            "value must be finite, lossless JSON",
        ) from exc


def canonical_sha256(value: Any) -> str:
    """Return a stable SHA-256 digest for one finite JSON value."""

    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _json_copy(value: Any) -> Any:
    return json.loads(_canonical_json(value))


class ChainInputContract(BaseModel):
    """Immutable identity and JSON Schema for one saved chain revision."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    chain_id: str
    chain_digest: str
    canonical_schema: str
    schema_digest: str

    @property
    def schema(self) -> dict[str, Any]:
        """Return a detached copy of the declared JSON Schema."""

        value = json.loads(self.canonical_schema)
        assert isinstance(value, dict)
        return value


def chain_input_contract(
    chain_id: str,
    chain: Mapping[str, Any],
) -> ChainInputContract:
    """Read and validate ``metadata.care.runtime_input_schema``.

    P0 adaptation deliberately requires an object-root Draft 2020-12 schema.
    It never infers a contract from prompt text or variable-looking strings.
    """

    metadata = chain.get("metadata")
    care_metadata = (
        metadata.get(CARE_METADATA_KEY) if isinstance(metadata, Mapping) else None
    )
    raw_schema = (
        care_metadata.get(RUNTIME_INPUT_SCHEMA_KEY)
        if isinstance(care_metadata, Mapping)
        else None
    )
    if raw_schema is None:
        raise MissingInputContractError(
            "chain has no metadata.care.runtime_input_schema",
        )
    if not isinstance(raw_schema, Mapping):
        raise InvalidInputContractError(
            "metadata.care.runtime_input_schema must be a JSON object",
        )

    schema = _json_copy(dict(raw_schema))
    declared_dialect = schema.get("$schema")
    if declared_dialect is None:
        schema["$schema"] = JSON_SCHEMA_DIALECT
    elif declared_dialect != JSON_SCHEMA_DIALECT:
        raise InvalidInputContractError(
            f"unsupported JSON Schema dialect: {declared_dialect!r}",
        )
    if schema.get("type") != "object":
        raise InvalidInputContractError(
            "P0 chain adaptation requires an object-root input schema",
        )
    if not isinstance(schema.get("properties"), Mapping):
        raise InvalidInputContractError(
            "object-root input schema must declare properties",
        )
    try:
        Draft202012Validator.check_schema(schema)
    except SchemaError as exc:
        raise InvalidInputContractError("invalid runtime input schema") from exc

    chain_value = _json_copy(dict(chain))
    schema_json = _canonical_json(schema)
    return ChainInputContract(
        chain_id=chain_id,
        chain_digest=canonical_sha256(chain_value),
        canonical_schema=schema_json,
        schema_digest=hashlib.sha256(schema_json.encode("utf-8")).hexdigest(),
    )


def _validate_pointer(value: str) -> str:
    if value and not value.startswith("/"):
        raise ValueError("JSON Pointer must be empty or start with '/'")
    for segment in value.split("/")[1:]:
        index = 0
        while index < len(segment):
            if segment[index] == "~":
                if index + 1 >= len(segment) or segment[index + 1] not in "01":
                    raise ValueError("JSON Pointer contains an invalid '~' escape")
                index += 2
            else:
                index += 1
    return value


def _pointer_segments(pointer: str) -> tuple[str, ...]:
    _validate_pointer(pointer)
    if pointer == "":
        return ()
    return tuple(
        segment.replace("~1", "/").replace("~0", "~")
        for segment in pointer[1:].split("/")
    )


def _encode_pointer(segments: Sequence[str]) -> str:
    if not segments:
        return ""
    return "/" + "/".join(
        segment.replace("~", "~0").replace("/", "~1") for segment in segments
    )


class JsonPointerBinding(BaseModel):
    """One explicit, value-preserving source-to-target JSON Pointer move."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    source: str = Field(description="RFC 6901 pointer in the supplied payload")
    target: str = Field(description="RFC 6901 pointer in the chain input")

    _source_pointer = field_validator("source")(_validate_pointer)
    _target_pointer = field_validator("target")(_validate_pointer)


class AdaptationIssue(BaseModel):
    """Value-redacted reason why a preview cannot be applied."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    code: Literal[
        "duplicate_source",
        "duplicate_target",
        "overlapping_source",
        "overlapping_target",
        "source_missing",
        "target_undeclared",
        "target_conflict",
        "schema_validation",
        "unmapped_source",
    ]
    pointer: str = ""
    keyword: str | None = None


class AdaptationPreview(BaseModel):
    """Public, value-free receipt for one proposed payload adaptation."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    preview_id: str | None
    status: Literal["ready", "needs_input"]
    chain_id: str
    chain_digest: str
    schema_digest: str
    source_digest: str
    adapted_digest: str | None
    policy_digest: str = LOSSLESS_POLICY_DIGEST
    adapter_version: str = ADAPTER_VERSION
    confirmation_digest: str | None = None
    bindings: tuple[JsonPointerBinding, ...]
    issues: tuple[AdaptationIssue, ...] = ()
    expires_at_unix: float | None = None


@dataclass(frozen=True)
class _StoredPreview:
    payload: Any
    chain_digest: str
    schema_digest: str
    source_digest: str
    confirmation_digest: str
    expires_at: float


def _locked(method: Callable[..., Any]) -> Callable[..., Any]:
    @wraps(method)
    def wrapper(self: "AdaptationPreviewStore", *args: Any, **kwargs: Any) -> Any:
        with self._lock:
            return method(self, *args, **kwargs)

    return wrapper


def _is_prefix(left: tuple[str, ...], right: tuple[str, ...]) -> bool:
    return len(left) <= len(right) and right[: len(left)] == left


def _overlap_issues(
    bindings: Sequence[JsonPointerBinding],
    *,
    field: Literal["source", "target"],
) -> list[AdaptationIssue]:
    paths = [
        (_pointer_segments(getattr(binding, field)), index)
        for index, binding in enumerate(bindings)
    ]
    issues: list[AdaptationIssue] = []
    duplicate_code = "duplicate_source" if field == "source" else "duplicate_target"
    overlap_code = "overlapping_source" if field == "source" else "overlapping_target"
    for position, (path, index) in enumerate(paths):
        for other_path, _other_index in paths[position + 1 :]:
            pointer = getattr(bindings[index], field)
            if path == other_path:
                issues.append(AdaptationIssue(code=duplicate_code, pointer=pointer))
            elif _is_prefix(path, other_path) or _is_prefix(other_path, path):
                issues.append(AdaptationIssue(code=overlap_code, pointer=pointer))
    return issues


def _resolve_pointer(document: Any, pointer: str) -> Any:
    current = document
    for segment in _pointer_segments(pointer):
        if isinstance(current, Mapping) and segment in current:
            current = current[segment]
            continue
        if isinstance(current, list) and segment.isdecimal():
            index = int(segment)
            if index < len(current):
                current = current[index]
                continue
        raise KeyError(pointer)
    return current


def _schema_declares_pointer(schema: Mapping[str, Any], pointer: str) -> bool:
    current: Any = schema
    for segment in _pointer_segments(pointer):
        if not isinstance(current, Mapping):
            return False
        properties = current.get("properties")
        if not isinstance(properties, Mapping) or segment not in properties:
            return False
        current = properties[segment]
    return True


def _assign_pointer(document: dict[str, Any], pointer: str, value: Any) -> None:
    segments = _pointer_segments(pointer)
    if not segments:
        if not isinstance(value, Mapping):
            raise ValueError("root target must receive an object")
        if document:
            raise ValueError("root target conflicts with another binding")
        document.update(_json_copy(dict(value)))
        return
    current = document
    for segment in segments[:-1]:
        existing = current.get(segment)
        if existing is None:
            child: dict[str, Any] = {}
            current[segment] = child
            current = child
            continue
        if not isinstance(existing, dict):
            raise ValueError("target path conflicts with an assigned scalar")
        current = existing
    leaf = segments[-1]
    if leaf in current:
        raise ValueError("target path is already assigned")
    current[leaf] = _json_copy(value)


def _leaf_pointers(value: Any, base: tuple[str, ...] = ()) -> list[str]:
    if isinstance(value, Mapping) and value:
        leaves: list[str] = []
        for key, child in value.items():
            leaves.extend(_leaf_pointers(child, (*base, str(key))))
        return leaves
    # Arrays move atomically in P0. This avoids inventing a positional mapping
    # rule and preserves their exact order and contents.
    return [_encode_pointer(base)]


def _validation_issues(
    schema: Mapping[str, Any],
    value: Any,
) -> list[AdaptationIssue]:
    validator = Draft202012Validator(schema)
    issues: list[AdaptationIssue] = []
    for error in sorted(
        validator.iter_errors(value), key=lambda item: list(item.absolute_path)
    ):
        pointer = _encode_pointer(tuple(str(item) for item in error.absolute_path))
        issues.append(
            AdaptationIssue(
                code="schema_validation",
                pointer=pointer,
                keyword=str(error.validator),
            ),
        )
    return issues


class AdaptationPreviewStore:
    """Bounded process-local owner of single-use adaptation previews."""

    def __init__(
        self,
        *,
        ttl_seconds: float = 900.0,
        max_entries: int = 128,
        max_payload_bytes: int = 5 * 1024 * 1024,
        clock: Callable[[], float] = time.time,
        id_factory: Callable[[], str] | None = None,
    ) -> None:
        if not math.isfinite(ttl_seconds) or ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive and finite")
        if max_entries < 1:
            raise ValueError("max_entries must be positive")
        if max_payload_bytes < 1:
            raise ValueError("max_payload_bytes must be positive")
        self._ttl_seconds = ttl_seconds
        self._max_entries = max_entries
        self._max_payload_bytes = max_payload_bytes
        self._clock = clock
        self._id_factory = id_factory or (lambda: secrets.token_urlsafe(24))
        self._previews: dict[str, _StoredPreview] = {}
        self._lock = threading.RLock()

    @_locked
    def preview(
        self,
        contract: ChainInputContract,
        source_payload: Any,
        bindings: Sequence[JsonPointerBinding],
    ) -> AdaptationPreview:
        """Validate a lossless mapping and retain only a ready payload."""

        now = self._clock()
        self._purge(now)
        source_json = _canonical_json(source_payload)
        if len(source_json.encode("utf-8")) > self._max_payload_bytes:
            raise HarnessContractError(
                "source payload exceeds the configured byte limit"
            )
        source_value = json.loads(source_json)
        source_digest = hashlib.sha256(source_json.encode("utf-8")).hexdigest()
        normalized_bindings = tuple(bindings)
        issues = [
            *_overlap_issues(normalized_bindings, field="source"),
            *_overlap_issues(normalized_bindings, field="target"),
        ]
        adapted: dict[str, Any] = {}
        consumed_source_paths: list[tuple[str, ...]] = []

        if not issues:
            for binding in normalized_bindings:
                try:
                    value = _resolve_pointer(source_value, binding.source)
                except KeyError:
                    issues.append(
                        AdaptationIssue(
                            code="source_missing",
                            pointer=binding.source,
                        ),
                    )
                    continue
                if not _schema_declares_pointer(contract.schema, binding.target):
                    issues.append(
                        AdaptationIssue(
                            code="target_undeclared",
                            pointer=binding.target,
                        ),
                    )
                    continue
                try:
                    _assign_pointer(adapted, binding.target, value)
                except ValueError:
                    issues.append(
                        AdaptationIssue(
                            code="target_conflict",
                            pointer=binding.target,
                        ),
                    )
                    continue
                consumed_source_paths.append(_pointer_segments(binding.source))

        if not issues:
            for pointer in _leaf_pointers(source_value):
                path = _pointer_segments(pointer)
                if not any(
                    _is_prefix(consumed, path) for consumed in consumed_source_paths
                ):
                    issues.append(
                        AdaptationIssue(
                            code="unmapped_source",
                            pointer=pointer,
                        ),
                    )
            issues.extend(_validation_issues(contract.schema, adapted))

        adapted_digest = canonical_sha256(adapted)
        if issues:
            return AdaptationPreview(
                preview_id=None,
                status="needs_input",
                chain_id=contract.chain_id,
                chain_digest=contract.chain_digest,
                schema_digest=contract.schema_digest,
                source_digest=source_digest,
                adapted_digest=adapted_digest,
                bindings=normalized_bindings,
                issues=tuple(issues),
            )

        if len(self._previews) >= self._max_entries:
            oldest = next(iter(self._previews))
            self._previews.pop(oldest, None)
        preview_id = self._unique_id()
        expires_at = now + self._ttl_seconds
        confirmation_digest = canonical_sha256(
            {
                "adapter_version": ADAPTER_VERSION,
                "bindings": [
                    binding.model_dump(mode="json") for binding in normalized_bindings
                ],
                "chain_digest": contract.chain_digest,
                "chain_id": contract.chain_id,
                "expires_at_unix": expires_at,
                "policy_digest": LOSSLESS_POLICY_DIGEST,
                "preview_id": preview_id,
                "schema_digest": contract.schema_digest,
                "source_digest": source_digest,
                "adapted_digest": adapted_digest,
            }
        )
        self._previews[preview_id] = _StoredPreview(
            payload=_json_copy(adapted),
            chain_digest=contract.chain_digest,
            schema_digest=contract.schema_digest,
            source_digest=source_digest,
            confirmation_digest=confirmation_digest,
            expires_at=expires_at,
        )
        return AdaptationPreview(
            preview_id=preview_id,
            status="ready",
            chain_id=contract.chain_id,
            chain_digest=contract.chain_digest,
            schema_digest=contract.schema_digest,
            source_digest=source_digest,
            adapted_digest=adapted_digest,
            bindings=normalized_bindings,
            expires_at_unix=expires_at,
            confirmation_digest=confirmation_digest,
        )

    @_locked
    def consume(
        self,
        preview_id: str,
        *,
        chain_digest: str,
        schema_digest: str,
        source_digest: str,
        confirmation_digest: str,
    ) -> Any:
        """Consume a ready preview after rechecking current chain identity."""

        now = self._clock()
        self._purge(now)
        stored = self._previews.get(preview_id)
        if stored is None:
            raise PreviewUnavailableError(
                "preview is missing, expired, or already used"
            )
        if stored.chain_digest != chain_digest:
            raise PreviewUnavailableError("chain changed after preview")
        if stored.schema_digest != schema_digest:
            raise PreviewUnavailableError("input contract changed after preview")
        if not hmac.compare_digest(stored.source_digest, source_digest):
            raise PreviewUnavailableError("source input changed after preview")
        if not hmac.compare_digest(
            stored.confirmation_digest,
            confirmation_digest,
        ):
            raise PreviewUnavailableError("preview confirmation binding is invalid")
        self._previews.pop(preview_id, None)
        return _json_copy(stored.payload)

    @_locked
    def discard(self, preview_id: str) -> bool:
        """Drop a preview without exposing its payload."""

        return self._previews.pop(preview_id, None) is not None

    def _purge(self, now: float) -> None:
        expired = [
            preview_id
            for preview_id, stored in self._previews.items()
            if stored.expires_at <= now
        ]
        for preview_id in expired:
            self._previews.pop(preview_id, None)

    def _unique_id(self) -> str:
        for _attempt in range(8):
            candidate = self._id_factory()
            if candidate and candidate not in self._previews:
                return candidate
        raise HarnessContractError("could not allocate a unique preview id")


__all__ = [
    "AdaptationIssue",
    "AdaptationPreview",
    "AdaptationPreviewStore",
    "ChainInputContract",
    "HarnessContractError",
    "InvalidInputContractError",
    "JsonPointerBinding",
    "MissingInputContractError",
    "PreviewUnavailableError",
    "canonical_sha256",
    "chain_input_contract",
]
