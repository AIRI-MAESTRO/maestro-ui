"""Private, bounded JSON artifacts for resident-agent tool calls.

The DeepSeek session log should carry opaque references and structural
profiles, not a second copy of every user value.  The native MAESTRO host can
stage a JSON value here and give the resident agent the returned artifact id.
Artifacts are immutable, process-local, expire automatically, and are copied
on both ingress and egress.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
import hashlib
import json
import math
import secrets
import threading
import time
from typing import Any

from pydantic import BaseModel, ConfigDict


class HarnessArtifactError(RuntimeError):
    """Base error for private harness artifact operations."""


class ArtifactUnavailableError(HarnessArtifactError):
    """An artifact is missing, expired, or no longer matches its digest."""


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
        raise HarnessArtifactError("artifact must be finite lossless JSON") from exc


def _encode_pointer(segments: Sequence[str]) -> str:
    if not segments:
        return ""
    return "/" + "/".join(
        segment.replace("~", "~0").replace("/", "~1") for segment in segments
    )


def _leaf_pointers(value: Any, base: tuple[str, ...] = ()) -> list[str]:
    if isinstance(value, Mapping) and value:
        leaves: list[str] = []
        for key, child in value.items():
            leaves.extend(_leaf_pointers(child, (*base, str(key))))
        return leaves
    # P0 treats arrays as atomic to avoid disclosing values or inventing a
    # positional transformation contract.
    return [_encode_pointer(base)]


class JsonArtifactReceipt(BaseModel):
    """Value-free public receipt for one privately held JSON payload."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    artifact_id: str
    content_sha256: str
    size_bytes: int
    leaf_pointers: tuple[str, ...]
    expires_at_unix: float


@dataclass(frozen=True)
class _StoredArtifact:
    canonical_json: str
    content_sha256: str
    size_bytes: int
    leaf_pointers: tuple[str, ...]
    expires_at: float


class JsonArtifactStore:
    """Thread-safe process-local store with bounded retention and byte usage."""

    def __init__(
        self,
        *,
        ttl_seconds: float = 3600.0,
        max_entries: int = 128,
        max_payload_bytes: int = 8 * 1024 * 1024,
        max_total_bytes: int = 32 * 1024 * 1024,
        clock: Callable[[], float] = time.time,
        id_factory: Callable[[], str] | None = None,
    ) -> None:
        if not math.isfinite(ttl_seconds) or ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive and finite")
        if max_entries < 1:
            raise ValueError("max_entries must be positive")
        if max_payload_bytes < 1:
            raise ValueError("max_payload_bytes must be positive")
        if max_total_bytes < max_payload_bytes:
            raise ValueError("max_total_bytes must be at least max_payload_bytes")
        self._ttl_seconds = ttl_seconds
        self._max_entries = max_entries
        self._max_payload_bytes = max_payload_bytes
        self._max_total_bytes = max_total_bytes
        self._clock = clock
        self._id_factory = id_factory or (lambda: secrets.token_urlsafe(24))
        self._artifacts: dict[str, _StoredArtifact] = {}
        self._total_bytes = 0
        self._lock = threading.RLock()

    def stage(self, payload: Any) -> JsonArtifactReceipt:
        """Copy one object-root JSON payload into the private store."""

        if not isinstance(payload, Mapping):
            raise HarnessArtifactError("P0 adaptation input must be a JSON object")
        canonical = _canonical_json(dict(payload))
        size_bytes = len(canonical.encode("utf-8"))
        if size_bytes > self._max_payload_bytes:
            raise HarnessArtifactError(
                "artifact exceeds the configured per-payload byte limit"
            )
        digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        pointers = tuple(sorted(_leaf_pointers(json.loads(canonical))))
        with self._lock:
            now = self._clock()
            self._purge_locked(now)
            while self._artifacts and (
                len(self._artifacts) >= self._max_entries
                or self._total_bytes + size_bytes > self._max_total_bytes
            ):
                self._evict_oldest_locked()
            artifact_id = self._unique_id_locked()
            expires_at = now + self._ttl_seconds
            self._artifacts[artifact_id] = _StoredArtifact(
                canonical_json=canonical,
                content_sha256=digest,
                size_bytes=size_bytes,
                leaf_pointers=pointers,
                expires_at=expires_at,
            )
            self._total_bytes += size_bytes
            return JsonArtifactReceipt(
                artifact_id=artifact_id,
                content_sha256=digest,
                size_bytes=size_bytes,
                leaf_pointers=pointers,
                expires_at_unix=expires_at,
            )

    def inspect(self, artifact_id: str) -> JsonArtifactReceipt:
        """Return a structural receipt without exposing stored values."""

        with self._lock:
            stored = self._get_locked(artifact_id)
            return JsonArtifactReceipt(
                artifact_id=artifact_id,
                content_sha256=stored.content_sha256,
                size_bytes=stored.size_bytes,
                leaf_pointers=stored.leaf_pointers,
                expires_at_unix=stored.expires_at,
            )

    def resolve(self, artifact_id: str, *, content_sha256: str) -> dict[str, Any]:
        """Return a detached payload only when the caller pins its digest."""

        with self._lock:
            stored = self._get_locked(artifact_id)
            if not secrets.compare_digest(stored.content_sha256, content_sha256):
                raise ArtifactUnavailableError("artifact content changed")
            value = json.loads(stored.canonical_json)
            assert isinstance(value, dict)
            return value

    def discard(self, artifact_id: str) -> bool:
        """Forget an artifact without ever returning its payload."""

        with self._lock:
            stored = self._artifacts.pop(artifact_id, None)
            if stored is None:
                return False
            self._total_bytes -= stored.size_bytes
            return True

    def _get_locked(self, artifact_id: str) -> _StoredArtifact:
        self._purge_locked(self._clock())
        stored = self._artifacts.get(artifact_id)
        if stored is None:
            raise ArtifactUnavailableError("artifact is missing or expired")
        return stored

    def _purge_locked(self, now: float) -> None:
        for artifact_id in [
            artifact_id
            for artifact_id, stored in self._artifacts.items()
            if stored.expires_at <= now
        ]:
            stored = self._artifacts.pop(artifact_id)
            self._total_bytes -= stored.size_bytes

    def _evict_oldest_locked(self) -> None:
        artifact_id = next(iter(self._artifacts))
        stored = self._artifacts.pop(artifact_id)
        self._total_bytes -= stored.size_bytes

    def _unique_id_locked(self) -> str:
        for _attempt in range(8):
            candidate = self._id_factory()
            if candidate and candidate not in self._artifacts:
                return candidate
        raise HarnessArtifactError("could not allocate a unique artifact id")


__all__ = [
    "ArtifactUnavailableError",
    "HarnessArtifactError",
    "JsonArtifactReceipt",
    "JsonArtifactStore",
]
