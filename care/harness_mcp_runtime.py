"""Safe production context construction for the MAESTRO MCP subprocess.

The native host and the stdio child communicate their *non-secret* runtime
coordinates through a deliberately small environment contract.  This module
validates that contract, opens the session-scoped shared artifact store, and
constructs the CARE domain objects that can be injected into
``CareMcpAdapter``.

Construction performs no Memory lookup, health check, or other network call.
Enabling Memory only creates the configured client object; the first tool call
is the first point at which the client may perform I/O.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import os
from pathlib import Path
import re
from typing import Any, cast

from pydantic import BaseModel, ConfigDict, model_validator

from care.config import CareConfig
from care.desktop_commands import CommandBridge
from care.harness_artifacts import JsonArtifactReceipt, JsonArtifactStore
from care.harness_commands import HarnessCommandService
from care.harness_execution import HarnessChainExecutionOperation
from care.harness_evolution import HarnessEvolutionOperation
from care.harness_generation import HarnessChainGenerationOperation
from care.harness_memory_write import HarnessChainMemoryWriteOperation
from care.harness_mcp import CareReadOnlyMcpBackend
from care.harness_service import (
    HarnessOrchestrationService,
    HarnessServiceError,
    memory_chain_loader,
)
from care.harness_shared_artifacts import (
    SharedArtifactError,
    SharedJsonArtifactReceipt,
    SharedJsonArtifactStore,
)
from care.memory import CareMemory
from care.platform import CarePlatform
from care.runtime.credential_broker import (
    CARE_CREDENTIAL_SLOTS,
    HarnessCredentialBrokerError,
    HarnessCredentialBrokerBootstrap,
    apply_harness_credentials,
    fetch_harness_credential,
)


SESSION_ID_ENV = "MAESTRO_HARNESS_SESSION_ID"
ARTIFACT_ROOT_ENV = "MAESTRO_HARNESS_ARTIFACT_ROOT"
MEMORY_ENABLED_ENV = "MAESTRO_HARNESS_MEMORY_ENABLED"
GENERATION_ENABLED_ENV = "MAESTRO_HARNESS_GENERATION_ENABLED"
EXECUTION_ENABLED_ENV = "MAESTRO_HARNESS_EXECUTION_ENABLED"
MEMORY_WRITE_ENABLED_ENV = "MAESTRO_HARNESS_MEMORY_WRITE_ENABLED"
EVOLUTION_ENABLED_ENV = "MAESTRO_HARNESS_EVOLUTION_ENABLED"
CONFIG_PATH_ENV = "MAESTRO_HARNESS_CONFIG_PATH"
WORKSPACE_ROOT_ENV = "MAESTRO_HARNESS_WORKSPACE_ROOT"

_SESSION_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}")
_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})
_FALSE_VALUES = frozenset({"0", "false", "no", "off"})


class HarnessMcpRuntimeContextError(RuntimeError):
    """A safe, actionable production-context diagnostic.

    Original exception messages are intentionally not copied into this
    boundary: TOML validation errors, provider URLs, and SDK exceptions may
    contain credentials.  ``code`` is stable enough for a native host to map
    the failure to UI guidance without parsing prose.
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class HarnessMcpRuntimeSettings(BaseModel):
    """Validated, non-secret coordinates shared with one MCP child."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    session_id: str
    artifact_root: Path
    workspace_root: Path
    memory_enabled: bool = False
    generation_enabled: bool = False
    execution_enabled: bool = False
    memory_write_enabled: bool = False
    evolution_enabled: bool = False
    config_path: Path | None = None

    @model_validator(mode="after")
    def validate_runtime_coordinates(self) -> "HarnessMcpRuntimeSettings":
        if _SESSION_ID_RE.fullmatch(self.session_id) is None:
            raise ValueError(
                "session id must contain 1-128 letters, digits, underscores, or hyphens"
            )
        if not self.artifact_root.is_absolute():
            raise ValueError("artifact root must be an absolute path")
        if not self.workspace_root.is_absolute():
            raise ValueError("workspace root must be an absolute path")
        if self.workspace_root == Path(self.workspace_root.anchor):
            raise ValueError("workspace root must be a normal directory")
        if self.config_path is not None and not self.config_path.is_absolute():
            raise ValueError("config path must be an absolute path")
        if (
            self.config_path is not None
            and not self.memory_enabled
            and not self.generation_enabled
            and not self.execution_enabled
            and not self.evolution_enabled
        ):
            raise ValueError(
                "config path requires Memory, generation, execution, or evolution "
                "to be enabled"
            )
        if self.memory_write_enabled and not self.memory_enabled:
            raise ValueError("Memory writes require Memory to be explicitly enabled")
        return self


@dataclass(frozen=True, slots=True)
class HarnessMcpRuntimeContext:
    """Objects owned by one configured MAESTRO MCP child.

    ``backend`` can be passed directly to ``CareMcpAdapter``.  ``service`` is
    the transport-neutral contract/adaptation seam and uses ``store`` through
    a receipt-shaping adapter so raw values remain in the shared SQLite store.
    """

    settings: HarnessMcpRuntimeSettings
    config: CareConfig | None
    memory: CareMemory | None
    generation: HarnessChainGenerationOperation | None
    execution: HarnessChainExecutionOperation | None
    memory_write: HarnessChainMemoryWriteOperation | None
    evolution: HarnessEvolutionOperation | None
    commands: HarnessCommandService | None
    backend: CareReadOnlyMcpBackend
    service: HarnessOrchestrationService
    store: SharedJsonArtifactStore


class _SharedServiceArtifactAdapter:
    """Present the shared store through ``JsonArtifactStore``'s value-free API."""

    def __init__(self, store: SharedJsonArtifactStore) -> None:
        self._store = store

    @staticmethod
    def _receipt(receipt: SharedJsonArtifactReceipt) -> JsonArtifactReceipt:
        return JsonArtifactReceipt(
            artifact_id=receipt.artifact_id,
            content_sha256=receipt.content_sha256,
            size_bytes=receipt.size_bytes,
            leaf_pointers=receipt.leaf_pointers,
            expires_at_unix=receipt.expires_at_unix,
        )

    def stage(self, payload: Any) -> JsonArtifactReceipt:
        return self._receipt(self._store.stage(payload))

    def inspect(self, artifact_id: str) -> JsonArtifactReceipt:
        return self._receipt(self._store.inspect(artifact_id))

    def resolve(self, artifact_id: str, *, content_sha256: str) -> dict[str, Any]:
        return self._store.resolve(
            artifact_id,
            content_sha256=content_sha256,
        )

    def discard(self, artifact_id: str) -> bool:
        return self._store.discard(artifact_id)


def _required_environment_value(env: Mapping[str, str], name: str) -> str:
    value = env.get(name)
    if not isinstance(value, str) or not value:
        raise HarnessMcpRuntimeContextError(
            "missing_runtime_coordinate",
            f"{name} is required for the MAESTRO MCP child.",
        )
    return value


def _explicit_feature_flag(
    env: Mapping[str, str],
    name: str,
    *,
    error_code: str,
) -> bool:
    raw = env.get(name)
    if raw is None:
        return False
    if not isinstance(raw, str) or not raw:
        raise HarnessMcpRuntimeContextError(
            error_code,
            f"{name} must be an explicit true or false value.",
        )
    normalised = raw.casefold()
    if normalised in _TRUE_VALUES:
        return True
    if normalised in _FALSE_VALUES:
        return False
    raise HarnessMcpRuntimeContextError(
        error_code,
        (
            f"{name} must be one of "
            "1/0, true/false, yes/no, or on/off."
        ),
    )


def load_harness_mcp_runtime_settings(
    env: Mapping[str, str] | None = None,
) -> HarnessMcpRuntimeSettings:
    """Read and validate only the explicit MAESTRO harness environment.

    Missing ``MAESTRO_HARNESS_MEMORY_ENABLED`` means Memory is disabled.  A
    config path without an explicit true flag is rejected as partial config;
    it never implicitly enables a provider.
    """

    selected_env = os.environ if env is None else env
    session_id = _required_environment_value(selected_env, SESSION_ID_ENV)
    artifact_root = _required_environment_value(selected_env, ARTIFACT_ROOT_ENV)
    workspace_root = _required_environment_value(selected_env, WORKSPACE_ROOT_ENV)
    memory_enabled = _explicit_feature_flag(
        selected_env,
        MEMORY_ENABLED_ENV,
        error_code="invalid_memory_flag",
    )
    generation_enabled = _explicit_feature_flag(
        selected_env,
        GENERATION_ENABLED_ENV,
        error_code="invalid_generation_flag",
    )
    execution_enabled = _explicit_feature_flag(
        selected_env,
        EXECUTION_ENABLED_ENV,
        error_code="invalid_execution_flag",
    )
    memory_write_enabled = _explicit_feature_flag(
        selected_env,
        MEMORY_WRITE_ENABLED_ENV,
        error_code="invalid_memory_write_flag",
    )
    evolution_enabled = _explicit_feature_flag(
        selected_env,
        EVOLUTION_ENABLED_ENV,
        error_code="invalid_evolution_flag",
    )
    raw_config_path = selected_env.get(CONFIG_PATH_ENV)
    if raw_config_path is not None and (
        not isinstance(raw_config_path, str) or not raw_config_path
    ):
        raise HarnessMcpRuntimeContextError(
            "invalid_config_path",
            f"{CONFIG_PATH_ENV} must be a non-empty absolute path when present.",
        )
    try:
        return HarnessMcpRuntimeSettings(
            session_id=session_id,
            artifact_root=Path(artifact_root),
            workspace_root=Path(workspace_root),
            memory_enabled=memory_enabled,
            generation_enabled=generation_enabled,
            execution_enabled=execution_enabled,
            memory_write_enabled=memory_write_enabled,
            evolution_enabled=evolution_enabled,
            config_path=Path(raw_config_path) if raw_config_path is not None else None,
        )
    except (TypeError, ValueError):
        raise HarnessMcpRuntimeContextError(
            "invalid_runtime_coordinates",
            (
                "MAESTRO harness session/config paths are invalid: use a safe "
                "session id and explicit absolute paths; a config path also "
                "requires Memory, generation, or execution to be enabled; "
                "evolution is also an explicit config-backed capability; "
                "Memory writes also require Memory."
            ),
        ) from None


def _memory_disabled_chain_loader(
    _chain_id: str,
    _channel: str,
) -> Mapping[str, Any]:
    raise HarnessServiceError(
        "Memory is not enabled for this MAESTRO harness session"
    )


def build_harness_mcp_runtime_context(
    env: Mapping[str, str] | None = None,
    *,
    credential_broker: HarnessCredentialBrokerBootstrap | None = None,
) -> HarnessMcpRuntimeContext:
    """Build one fail-closed MCP runtime context without provider I/O."""

    selected_env = os.environ if env is None else env
    settings = load_harness_mcp_runtime_settings(selected_env)
    if (
        credential_broker is not None
        and credential_broker.session_id != settings.session_id
    ):
        raise HarnessMcpRuntimeContextError(
            "credential_broker_session_mismatch",
            "The native credential capability does not belong to this session.",
        )

    if settings.workspace_root.is_symlink() or not settings.workspace_root.is_dir():
        raise HarnessMcpRuntimeContextError(
            "workspace_root_unavailable",
            (
                f"{WORKSPACE_ROOT_ENV} must name an existing, non-symlink "
                "directory selected by the native host."
            ),
        )

    if settings.config_path is not None and not settings.config_path.is_file():
        raise HarnessMcpRuntimeContextError(
            "config_path_unavailable",
            (
                f"{CONFIG_PATH_ENV} must name an existing regular file when "
                "Memory, generation, or execution is enabled."
            ),
        )

    try:
        store = SharedJsonArtifactStore(
            root=settings.artifact_root,
            session_id=settings.session_id,
        )
    except (OSError, SharedArtifactError, ValueError):
        raise HarnessMcpRuntimeContextError(
            "artifact_store_initialization_failed",
            (
                "Could not initialize private MAESTRO artifact storage; check "
                "that the absolute artifact root is local, writable, and not a symlink."
            ),
        ) from None

    config: CareConfig | None = None
    memory: CareMemory | None = None
    generation: HarnessChainGenerationOperation | None = None
    execution: HarnessChainExecutionOperation | None = None
    memory_write: HarnessChainMemoryWriteOperation | None = None
    evolution: HarnessEvolutionOperation | None = None
    commands: HarnessCommandService | None = None
    if (
        settings.memory_enabled
        or settings.generation_enabled
        or settings.execution_enabled
        or settings.evolution_enabled
    ):
        try:
            load_kwargs: dict[str, Any] = {
                "path": settings.config_path,
                "env": dict(selected_env),
                "cwd": settings.workspace_root,
            }
            if credential_broker is not None:
                load_kwargs["resolve_secrets"] = False
            config = CareConfig.load(**load_kwargs)
        except Exception:  # noqa: BLE001 - the public diagnostic must stay redacted
            raise HarnessMcpRuntimeContextError(
                "care_config_load_failed",
                (
                    "Could not load CARE configuration for enabled harness "
                    "capabilities; check the TOML "
                    "and CARE_* settings. Secret values were omitted."
                ),
            ) from None

        if credential_broker is not None:
            try:
                broker_values: dict[str, str] = {}
                for slot in CARE_CREDENTIAL_SLOTS:
                    value = fetch_harness_credential(
                        credential_broker,
                        slot=slot,
                    )
                    if value is not None:
                        broker_values[slot] = value
                apply_harness_credentials(config, broker_values)
            except HarnessCredentialBrokerError:
                raise HarnessMcpRuntimeContextError(
                    "credential_broker_unavailable",
                    (
                        "Could not obtain configured credentials from the native "
                        "MAESTRO process. Restart the application and review "
                        "credential access in Settings."
                    ),
                ) from None

    if settings.memory_enabled:
        if config is None:  # pragma: no cover - guarded by the branch above
            raise AssertionError("enabled Memory requires CARE configuration")
        try:
            # GigaEvoClient.from_config constructs a client but does not issue a
            # request.  Do not add a health check to this startup boundary.
            memory = CareMemory.from_config(config)
        except Exception:  # noqa: BLE001 - provider exceptions may contain secrets
            raise HarnessMcpRuntimeContextError(
                "memory_initialization_failed",
                (
                    "Could not initialize the CARE Memory client; verify the "
                    "Memory URL and credential settings. No secret was included."
                ),
            ) from None

    if settings.generation_enabled:
        if config is None:  # pragma: no cover - guarded by the branch above
            raise AssertionError("enabled generation requires CARE configuration")
        try:
            generation = HarnessChainGenerationOperation(
                config=config,
                artifacts=store,
            )
        except Exception:  # noqa: BLE001 - keep config/provider details redacted
            raise HarnessMcpRuntimeContextError(
                "generation_initialization_failed",
                (
                    "Could not initialize MAGE chain generation; verify the "
                    "CARE MAGE configuration. No secret was included."
                ),
            ) from None

    try:
        # The broad command bridge can reach every CARE domain, so exposing it
        # under a partially-disabled typed runtime would bypass those feature
        # gates. Enable it only when the complete effect surface is enabled.
        # One instance then lives for the entire MCP child lifetime because its
        # CommandBridge owns short-lived, one-use confirmation challenges.
        commands = (
            HarnessCommandService(
                CommandBridge(workspace_root=settings.workspace_root)
            )
            if (
                settings.memory_enabled
                and settings.generation_enabled
                and settings.execution_enabled
                and settings.memory_write_enabled
                and settings.evolution_enabled
            )
            else None
        )
        shared_artifacts = _SharedServiceArtifactAdapter(store)
        service = HarnessOrchestrationService(
            chain_loader=(
                memory_chain_loader(memory)
                if memory is not None
                else _memory_disabled_chain_loader
            ),
            artifacts=cast(JsonArtifactStore, shared_artifacts),
        )
        if settings.execution_enabled:
            if config is None:  # pragma: no cover - guarded by config loading
                raise AssertionError("enabled execution requires CARE configuration")
            execution = HarnessChainExecutionOperation(
                config=config,
                artifacts=store,
                previews=service.previews,
            )
        if settings.memory_write_enabled:
            if memory is None:  # pragma: no cover - guarded by settings validation
                raise AssertionError("enabled Memory writes require Memory")
            memory_write = HarnessChainMemoryWriteOperation(
                memory=memory,
                artifacts=store,
            )
        if settings.evolution_enabled:
            if config is None:  # pragma: no cover - guarded by config loading
                raise AssertionError("enabled evolution requires CARE configuration")
            evolution = HarnessEvolutionOperation(
                platform=CarePlatform.from_config(config),
                artifacts=store,
            )
        backend = CareReadOnlyMcpBackend(
            memory=memory,
            # Contract inspection needs a real chain loader.  Keep the
            # trusted-native staging service in the context when Memory is
            # disabled, but do not advertise unusable MCP orchestration tools.
            orchestration=service if memory is not None else None,
            generation=generation,
            artifact_stager=shared_artifacts,
            execution=execution,
            memory_write=memory_write,
            evolution=evolution,
            commands=commands,
        )
    except Exception:  # noqa: BLE001 - keep SDK/config detail outside diagnostics
        raise HarnessMcpRuntimeContextError(
            "runtime_context_initialization_failed",
            "Could not initialize the MAESTRO MCP runtime domain context.",
        ) from None

    return HarnessMcpRuntimeContext(
        settings=settings,
        config=config,
        memory=memory,
        generation=generation,
        execution=execution,
        memory_write=memory_write,
        evolution=evolution,
        commands=commands,
        backend=backend,
        service=service,
        store=store,
    )


__all__ = [
    "ARTIFACT_ROOT_ENV",
    "CONFIG_PATH_ENV",
    "EXECUTION_ENABLED_ENV",
    "EVOLUTION_ENABLED_ENV",
    "GENERATION_ENABLED_ENV",
    "MEMORY_ENABLED_ENV",
    "MEMORY_WRITE_ENABLED_ENV",
    "SESSION_ID_ENV",
    "WORKSPACE_ROOT_ENV",
    "HarnessMcpRuntimeContext",
    "HarnessMcpRuntimeContextError",
    "HarnessMcpRuntimeSettings",
    "build_harness_mcp_runtime_context",
    "load_harness_mcp_runtime_settings",
]
