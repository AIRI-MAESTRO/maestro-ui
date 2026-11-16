"""Transport-neutral orchestration seam for the MAESTRO resident agent.

DeepSeek Harness owns the conversation and tool loop.  This service owns the
CARE domain boundary that can safely be reused by MCP, desktop, and headless
hosts.  The first vertical slice is intentionally read-only from the external
world: inspect a pinned chain contract and build a lossless adaptation preview.
Applying the preview and running the chain are separate approval-gated work.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from care.harness_artifacts import JsonArtifactReceipt, JsonArtifactStore
from care.harness_contracts import (
    AdaptationPreview,
    AdaptationPreviewStore,
    InvalidInputContractError,
    JsonPointerBinding,
    MissingInputContractError,
    chain_input_contract,
)
from care.runtime.cancellation import CancellationToken


class HarnessServiceError(RuntimeError):
    """A resident-agent domain operation could not be completed safely."""


class HarnessResultModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ContractInspection(HarnessResultModel):
    """Value-free view of one exact chain input contract."""

    status: Literal["ready", "contract_unavailable"]
    chain_id: str
    channel: str
    chain_digest: str | None = None
    schema_digest: str | None = None
    input_schema: dict[str, Any] | None = None
    diagnostic: str | None = None


class AdaptationInspection(HarnessResultModel):
    """The structural information a resident agent may plan against."""

    status: Literal["planning_required", "contract_unavailable"]
    chain: ContractInspection
    source: JsonArtifactReceipt
    allowed_operations: tuple[Literal["move"], ...] = ("move",)


class AdaptationPreviewResult(HarnessResultModel):
    """Typed wrapper that keeps contract-unavailable distinct from rejection."""

    status: Literal["ready", "needs_input", "contract_unavailable"]
    inspection: AdaptationInspection
    preview: AdaptationPreview | None = None


ChainLoader = Callable[[str, str], Mapping[str, Any]]


def memory_chain_loader(memory: Any) -> ChainLoader:
    """Adapt a configured ``CareMemory`` without importing its SDK here."""

    get_chain = getattr(memory, "get_chain", None)
    if not callable(get_chain):
        raise TypeError("memory must expose get_chain(entity_id, channel=...)")

    def load(chain_id: str, channel: str) -> Mapping[str, Any]:
        value = get_chain(chain_id, channel=channel)
        if not isinstance(value, Mapping):
            raise HarnessServiceError("Memory returned a non-object chain")
        return value

    return load


class HarnessOrchestrationService:
    """Small domain service shared by native and MCP transports."""

    def __init__(
        self,
        *,
        chain_loader: ChainLoader,
        artifacts: JsonArtifactStore | None = None,
        previews: AdaptationPreviewStore | None = None,
    ) -> None:
        self._chain_loader = chain_loader
        self.artifacts = artifacts or JsonArtifactStore()
        self.previews = previews or AdaptationPreviewStore()

    def stage_input(
        self,
        payload: Mapping[str, Any],
        *,
        cancellation: CancellationToken | None = None,
    ) -> JsonArtifactReceipt:
        """Stage input from a trusted native ingress, outside the MCP log."""

        self._check_cancelled(cancellation)
        receipt = self.artifacts.stage(payload)
        self._check_cancelled(cancellation)
        return receipt

    def inspect_contract(
        self,
        chain_id: str,
        *,
        channel: str = "latest",
        cancellation: CancellationToken | None = None,
    ) -> ContractInspection:
        """Resolve the current chain revision and read only an explicit schema."""

        self._check_cancelled(cancellation)
        chain = self._load_chain(chain_id, channel)
        self._check_cancelled(cancellation)
        try:
            contract = chain_input_contract(chain_id, chain)
        except MissingInputContractError:
            return ContractInspection(
                status="contract_unavailable",
                chain_id=chain_id,
                channel=channel,
                diagnostic=(
                    "The chain has no explicit runtime input contract; regenerate "
                    "or version it with metadata.care.runtime_input_schema."
                ),
            )
        except InvalidInputContractError as exc:
            return ContractInspection(
                status="contract_unavailable",
                chain_id=chain_id,
                channel=channel,
                diagnostic=str(exc),
            )
        return ContractInspection(
            status="ready",
            chain_id=chain_id,
            channel=channel,
            chain_digest=contract.chain_digest,
            schema_digest=contract.schema_digest,
            input_schema=contract.schema,
        )

    def inspect_adaptation(
        self,
        chain_id: str,
        artifact_id: str,
        *,
        channel: str = "latest",
        cancellation: CancellationToken | None = None,
    ) -> AdaptationInspection:
        """Return only source pointers and the target schema, never raw values."""

        self._check_cancelled(cancellation)
        source = self.artifacts.inspect(artifact_id)
        chain = self.inspect_contract(
            chain_id,
            channel=channel,
            cancellation=cancellation,
        )
        return AdaptationInspection(
            status=(
                "planning_required"
                if chain.status == "ready"
                else "contract_unavailable"
            ),
            chain=chain,
            source=source,
        )

    def preview_adaptation(
        self,
        chain_id: str,
        artifact_id: str,
        *,
        artifact_sha256: str,
        bindings: Sequence[JsonPointerBinding],
        channel: str = "latest",
        cancellation: CancellationToken | None = None,
    ) -> AdaptationPreviewResult:
        """Verify an agent-proposed mapping against exact current state."""

        self._check_cancelled(cancellation)
        inspection = self.inspect_adaptation(
            chain_id,
            artifact_id,
            channel=channel,
            cancellation=cancellation,
        )
        if inspection.status == "contract_unavailable":
            return AdaptationPreviewResult(
                status="contract_unavailable",
                inspection=inspection,
            )

        source_payload = self.artifacts.resolve(
            artifact_id,
            content_sha256=artifact_sha256,
        )
        chain = self._load_chain(chain_id, channel)
        contract = chain_input_contract(chain_id, chain)
        # Ensure the revision inspected for planning is still the revision
        # being previewed. A moving ``latest`` channel fails closed.
        if contract.chain_digest != inspection.chain.chain_digest:
            raise HarnessServiceError("chain changed during adaptation preview")
        self._check_cancelled(cancellation)
        preview = self.previews.preview(contract, source_payload, bindings)
        self._check_cancelled(cancellation)
        return AdaptationPreviewResult(
            status=preview.status,
            inspection=inspection,
            preview=preview,
        )

    def _load_chain(self, chain_id: str, channel: str) -> Mapping[str, Any]:
        if not chain_id.strip():
            raise HarnessServiceError("chain_id is required")
        if not channel.strip():
            raise HarnessServiceError("channel is required")
        try:
            chain = self._chain_loader(chain_id, channel)
        except HarnessServiceError:
            raise
        except Exception as exc:
            # Do not cross provider URLs or credentials into model-visible
            # diagnostics. The original exception remains chained for logs.
            raise HarnessServiceError(
                f"chain lookup failed ({type(exc).__name__})"
            ) from exc
        if not isinstance(chain, Mapping):
            raise HarnessServiceError("chain loader returned a non-object value")
        return chain

    @staticmethod
    def _check_cancelled(cancellation: CancellationToken | None) -> None:
        if cancellation is not None:
            cancellation.raise_if_cancelled()


__all__ = [
    "AdaptationInspection",
    "AdaptationPreviewResult",
    "ChainLoader",
    "ContractInspection",
    "HarnessOrchestrationService",
    "HarnessServiceError",
    "memory_chain_loader",
]
