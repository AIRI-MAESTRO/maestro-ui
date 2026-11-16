from __future__ import annotations

import socket
from pathlib import Path
from typing import Any

import pytest

from care.config import CareConfig
from care.harness_mcp import CareMcpAdapter
from care.harness_mcp_runtime import (
    ARTIFACT_ROOT_ENV,
    CONFIG_PATH_ENV,
    EXECUTION_ENABLED_ENV,
    EVOLUTION_ENABLED_ENV,
    GENERATION_ENABLED_ENV,
    MEMORY_ENABLED_ENV,
    MEMORY_WRITE_ENABLED_ENV,
    SESSION_ID_ENV,
    WORKSPACE_ROOT_ENV,
    HarnessMcpRuntimeContextError,
    build_harness_mcp_runtime_context,
    load_harness_mcp_runtime_settings,
)
from care.harness_service import HarnessServiceError
import care.harness_mcp_runtime as runtime


def _env(tmp_path: Path, **overrides: str) -> dict[str, str]:
    values = {
        SESSION_ID_ENV: "session-01",
        ARTIFACT_ROOT_ENV: str(tmp_path / "artifacts"),
        WORKSPACE_ROOT_ENV: str(tmp_path),
    }
    values.update(overrides)
    return values


@pytest.mark.parametrize(
    "missing",
    [SESSION_ID_ENV, ARTIFACT_ROOT_ENV, WORKSPACE_ROOT_ENV],
)
def test_settings_require_explicit_host_coordinates(
    tmp_path: Path,
    missing: str,
) -> None:
    env = _env(tmp_path)
    del env[missing]

    with pytest.raises(HarnessMcpRuntimeContextError) as raised:
        load_harness_mcp_runtime_settings(env)

    assert raised.value.code == "missing_runtime_coordinate"
    assert missing in str(raised.value)


@pytest.mark.parametrize(
    "value, expected",
    [
        ("1", True),
        ("true", True),
        ("YES", True),
        ("on", True),
        ("0", False),
        ("false", False),
        ("NO", False),
        ("off", False),
    ],
)
def test_memory_flag_is_strict_and_explicit(
    tmp_path: Path,
    value: str,
    expected: bool,
) -> None:
    settings = load_harness_mcp_runtime_settings(
        _env(tmp_path, **{MEMORY_ENABLED_ENV: value})
    )

    assert settings.memory_enabled is expected


def test_invalid_memory_flag_fails_closed(tmp_path: Path) -> None:
    with pytest.raises(HarnessMcpRuntimeContextError) as raised:
        load_harness_mcp_runtime_settings(
            _env(tmp_path, **{MEMORY_ENABLED_ENV: "sometimes"})
        )

    assert raised.value.code == "invalid_memory_flag"
    assert "sometimes" not in str(raised.value)


@pytest.mark.parametrize(
    "flag",
    [
        MEMORY_ENABLED_ENV,
        GENERATION_ENABLED_ENV,
        EXECUTION_ENABLED_ENV,
        EVOLUTION_ENABLED_ENV,
        MEMORY_WRITE_ENABLED_ENV,
    ],
)
def test_invalid_feature_flags_fail_closed(tmp_path: Path, flag: str) -> None:
    with pytest.raises(HarnessMcpRuntimeContextError) as raised:
        load_harness_mcp_runtime_settings(_env(tmp_path, **{flag: "sometimes"}))

    expected = {
        MEMORY_ENABLED_ENV: "invalid_memory_flag",
        GENERATION_ENABLED_ENV: "invalid_generation_flag",
        EXECUTION_ENABLED_ENV: "invalid_execution_flag",
        EVOLUTION_ENABLED_ENV: "invalid_evolution_flag",
        MEMORY_WRITE_ENABLED_ENV: "invalid_memory_write_flag",
    }[flag]
    assert raised.value.code == expected
    assert "sometimes" not in str(raised.value)


@pytest.mark.parametrize(
    ("override", "value"),
    [
        (ARTIFACT_ROOT_ENV, "relative/artifacts"),
        (WORKSPACE_ROOT_ENV, "relative/workspace"),
        (SESSION_ID_ENV, "../other-session"),
    ],
)
def test_runtime_coordinates_reject_ambiguous_or_escaping_values(
    tmp_path: Path,
    override: str,
    value: str,
) -> None:
    with pytest.raises(HarnessMcpRuntimeContextError) as raised:
        load_harness_mcp_runtime_settings(_env(tmp_path, **{override: value}))

    assert raised.value.code == "invalid_runtime_coordinates"
    assert value not in str(raised.value)


def test_config_path_cannot_implicitly_enable_memory(tmp_path: Path) -> None:
    config_path = tmp_path / "care.toml"
    config_path.write_text("[memory]\n", encoding="utf-8")

    with pytest.raises(HarnessMcpRuntimeContextError) as raised:
        load_harness_mcp_runtime_settings(
            _env(tmp_path, **{CONFIG_PATH_ENV: str(config_path)})
        )

    assert raised.value.code == "invalid_runtime_coordinates"
    assert str(config_path) not in str(raised.value)


def test_workspace_root_must_be_existing_non_symlink_directory(
    tmp_path: Path,
) -> None:
    missing = tmp_path / "missing-workspace"
    with pytest.raises(HarnessMcpRuntimeContextError) as raised:
        build_harness_mcp_runtime_context(
            _env(tmp_path, **{WORKSPACE_ROOT_ENV: str(missing)})
        )
    assert raised.value.code == "workspace_root_unavailable"
    assert str(missing) not in str(raised.value)

    real = tmp_path / "real-workspace"
    real.mkdir()
    linked = tmp_path / "linked-workspace"
    linked.symlink_to(real, target_is_directory=True)
    with pytest.raises(HarnessMcpRuntimeContextError) as linked_error:
        build_harness_mcp_runtime_context(
            _env(tmp_path, **{WORKSPACE_ROOT_ENV: str(linked)})
        )
    assert linked_error.value.code == "workspace_root_unavailable"


def test_memory_write_cannot_be_enabled_without_memory(tmp_path: Path) -> None:
    with pytest.raises(HarnessMcpRuntimeContextError) as raised:
        load_harness_mcp_runtime_settings(
            _env(tmp_path, **{MEMORY_WRITE_ENABLED_ENV: "true"})
        )

    assert raised.value.code == "invalid_runtime_coordinates"


def test_explicit_config_path_must_exist_when_memory_is_enabled(
    tmp_path: Path,
) -> None:
    with pytest.raises(HarnessMcpRuntimeContextError) as raised:
        build_harness_mcp_runtime_context(
            _env(
                tmp_path,
                **{
                    MEMORY_ENABLED_ENV: "true",
                    CONFIG_PATH_ENV: str(tmp_path / "missing.toml"),
                },
            )
        )

    assert raised.value.code == "config_path_unavailable"
    assert str(tmp_path) not in str(raised.value)


def test_disabled_context_never_loads_config_or_memory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def forbidden(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("disabled Memory was initialized")

    monkeypatch.setattr(runtime.CareConfig, "load", forbidden)
    monkeypatch.setattr(runtime.CareMemory, "from_config", forbidden)

    context = build_harness_mcp_runtime_context(_env(tmp_path))

    assert context.memory is None
    assert context.config is None
    assert context.generation is None
    assert context.execution is None
    assert context.memory_write is None
    assert context.evolution is None
    assert context.commands is None
    assert context.backend.memory_available is False
    assert context.backend.orchestration_available is False
    assert context.backend.generation_available is False
    assert context.backend.artifact_staging_available is True
    assert context.backend.execution_available is False
    assert context.backend.memory_write_available is False
    assert context.backend.evolution_available is False
    assert context.backend.command_surface_available is False
    assert context.store.session_id == "session-01"
    assert context.store.root == (tmp_path / "artifacts").resolve()

    receipt = context.service.stage_input({"town": "Moscow"})
    assert not hasattr(receipt, "session_id")
    assert context.store.resolve(
        receipt.artifact_id,
        content_sha256=receipt.content_sha256,
    ) == {"town": "Moscow"}

    with pytest.raises(HarnessServiceError, match="Memory is not enabled"):
        context.service.inspect_contract("chain-1")


@pytest.mark.asyncio
async def test_disabled_context_capabilities_do_not_advertise_orchestration(
    tmp_path: Path,
) -> None:
    context = build_harness_mcp_runtime_context(_env(tmp_path))
    adapter = CareMcpAdapter(
        context.backend,
        request_id_factory=lambda: "request-disabled-context",
    )

    result = await adapter.call_tool("maestro_capabilities", {})
    capabilities = {
        item["name"]: item["availability"] for item in result["capabilities"]
    }

    assert capabilities["memory_read"] == "unavailable"
    assert capabilities["inspect_input_contract"] == "unavailable"
    assert capabilities["preview_input_adaptation"] == "unavailable"
    assert capabilities["generate_chain"] == "unavailable"
    assert capabilities["stage_artifact"] == "approval_gated"
    assert capabilities["execute_chain"] == "unavailable"
    assert capabilities["memory_write"] == "unavailable"
    assert capabilities["evolution_status"] == "unavailable"
    assert capabilities["evolve_chain"] == "unavailable"
    assert capabilities["cancel_evolution"] == "unavailable"
    assert capabilities["command_catalog_and_review"] == "unavailable"
    assert capabilities["command_confirm"] == "unavailable"
    assert capabilities["command_status"] == "unavailable"

    contract = await adapter.call_tool(
        "maestro_chain_input_contract",
        {"chain_id": "chain-1"},
    )
    assert contract["status"] == "unavailable"

    command = await adapter.call_tool(
        "maestro_command_review",
        {"command": "doctor"},
    )
    assert command["status"] == "unavailable"


class _FakeMemory:
    def search(self, *_args: Any, **_kwargs: Any) -> list[Any]:
        return []

    def get_chain(self, entity_id: str, *, channel: str) -> dict[str, Any]:
        return {
            "id": entity_id,
            "channel": channel,
            "metadata": {
                "care": {
                    "runtime_input_schema": {
                        "type": "object",
                        "properties": {"city": {"type": "string"}},
                        "required": ["city"],
                        "additionalProperties": False,
                    }
                }
            },
            "steps": [],
        }

    def save_chain(self, *_args: Any, **_kwargs: Any) -> str:
        return "chain-version-1"


def test_enabled_context_loads_explicit_config_and_wires_domain_objects(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "care.toml"
    config_path.write_text("[memory]\nbase_url = 'http://127.0.0.1:8002'\n", encoding="utf-8")
    observed: dict[str, Any] = {}
    fake_memory = _FakeMemory()

    def fake_load(
        _cls: type[CareConfig],
        *,
        path: Path | None = None,
        env: dict[str, str] | None = None,
        cwd: Path | None = None,
    ) -> CareConfig:
        observed["path"] = path
        observed["env"] = env
        observed["cwd"] = cwd
        return CareConfig()

    def fake_from_config(
        _cls: type[runtime.CareMemory],
        config: CareConfig,
    ) -> _FakeMemory:
        observed["config"] = config
        return fake_memory

    monkeypatch.setattr(runtime.CareConfig, "load", classmethod(fake_load))
    monkeypatch.setattr(
        runtime.CareMemory,
        "from_config",
        classmethod(fake_from_config),
    )
    env = _env(
        tmp_path,
        **{
            MEMORY_ENABLED_ENV: "true",
            CONFIG_PATH_ENV: str(config_path),
            "CARE_MEMORY__API_KEY": "super-secret",
        },
    )

    context = build_harness_mcp_runtime_context(env)

    assert context.memory is fake_memory
    assert context.config == CareConfig()
    assert context.generation is None
    assert context.execution is None
    assert context.memory_write is None
    assert context.evolution is None
    assert context.backend.memory_available is True
    assert context.backend.orchestration_available is True
    assert observed["path"] == config_path
    assert observed["env"] == env
    assert observed["cwd"] == tmp_path
    assert observed["config"] == CareConfig()
    inspection = context.service.inspect_contract("chain-1")
    assert inspection.status == "ready"
    assert inspection.input_schema is not None
    assert inspection.input_schema["required"] == ["city"]


@pytest.mark.asyncio
async def test_context_backend_exposes_contract_through_typed_mcp_call(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "care.toml"
    config_path.write_text("[memory]\n", encoding="utf-8")
    fake_memory = _FakeMemory()

    monkeypatch.setattr(
        runtime.CareMemory,
        "from_config",
        classmethod(lambda _cls, _config: fake_memory),
    )
    context = build_harness_mcp_runtime_context(
        _env(
            tmp_path,
            **{
                MEMORY_ENABLED_ENV: "true",
                CONFIG_PATH_ENV: str(config_path),
            },
        )
    )
    adapter = CareMcpAdapter(
        context.backend,
        request_id_factory=lambda: "request-runtime-context",
    )

    result = await adapter.call_tool(
        "maestro_chain_input_contract",
        {"chain_id": "chain-1", "channel": "stable"},
    )

    assert result["status"] == "completed"
    assert result["request_id"] == "request-runtime-context"
    assert result["inspection"]["status"] == "ready"
    assert result["inspection"]["channel"] == "stable"
    assert result["inspection"]["input_schema"]["required"] == ["city"]

    capabilities_result = await adapter.call_tool("maestro_capabilities", {})
    capabilities = {
        item["name"]: item["availability"]
        for item in capabilities_result["capabilities"]
    }
    assert capabilities["memory_read"] == "available"
    assert capabilities["inspect_input_contract"] == "available"
    assert capabilities["preview_input_adaptation"] == "available"
    assert capabilities["generate_chain"] == "unavailable"


def test_generation_can_be_enabled_without_memory_or_network(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "care.toml"
    config_path.write_text("[mage]\nmode = 'fast'\n", encoding="utf-8")

    def forbidden(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("Memory must remain disabled")

    monkeypatch.setattr(runtime.CareMemory, "from_config", forbidden)
    context = build_harness_mcp_runtime_context(
        _env(
            tmp_path,
            **{
                GENERATION_ENABLED_ENV: "true",
                CONFIG_PATH_ENV: str(config_path),
            },
        )
    )

    assert context.settings.generation_enabled is True
    assert context.settings.memory_enabled is False
    assert context.config is not None
    assert context.memory is None
    assert context.generation is not None
    assert context.backend.generation_available is True
    assert context.backend.memory_available is False


def test_execution_can_be_enabled_without_memory_or_network(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def forbidden(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("Memory must remain disabled")

    monkeypatch.setattr(runtime.CareMemory, "from_config", forbidden)
    context = build_harness_mcp_runtime_context(
        _env(tmp_path, **{EXECUTION_ENABLED_ENV: "true"})
    )

    assert context.settings.execution_enabled is True
    assert context.settings.memory_enabled is False
    assert context.config is not None
    assert context.memory is None
    assert context.execution is not None
    assert context.backend.execution_available is True


def test_evolution_can_be_enabled_without_memory_or_network(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakePlatform:
        def start_evolution(self, **_kwargs: Any) -> Any:
            raise AssertionError("evolution must not start during construction")

        def get_evolution(self, _evolution_id: str) -> dict[str, Any]:
            raise AssertionError("evolution must not poll during construction")

        def cancel(self, _evolution_id: str) -> dict[str, Any]:
            raise AssertionError("evolution must not cancel during construction")

    fake_platform = FakePlatform()
    monkeypatch.setattr(
        runtime.CarePlatform,
        "from_config",
        classmethod(lambda _cls, _config: fake_platform),
    )
    context = build_harness_mcp_runtime_context(
        _env(tmp_path, **{EVOLUTION_ENABLED_ENV: "true"})
    )

    assert context.settings.evolution_enabled is True
    assert context.config is not None
    assert context.memory is None
    assert context.evolution is not None
    assert context.backend.evolution_available is True


def test_memory_write_is_wired_only_when_explicitly_enabled(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_memory = _FakeMemory()
    monkeypatch.setattr(
        runtime.CareMemory,
        "from_config",
        classmethod(lambda _cls, _config: fake_memory),
    )
    context = build_harness_mcp_runtime_context(
        _env(
            tmp_path,
            **{
                MEMORY_ENABLED_ENV: "true",
                MEMORY_WRITE_ENABLED_ENV: "true",
            },
        )
    )

    assert context.memory_write is not None
    assert context.backend.memory_write_available is True


def test_complete_command_surface_receives_selected_workspace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: dict[str, Any] = {}
    fake_memory = _FakeMemory()

    class FakeCommandBridge:
        def __init__(self, *, workspace_root: Path) -> None:
            observed["workspace_root"] = workspace_root

        def registry(self) -> dict[str, Any]:
            return {}

        def execute(self, *_args: Any, **_kwargs: Any) -> Any:
            raise AssertionError("command execution is not part of context setup")

    monkeypatch.setattr(runtime, "CommandBridge", FakeCommandBridge)
    monkeypatch.setattr(
        runtime.CareMemory,
        "from_config",
        classmethod(lambda _cls, _config: fake_memory),
    )

    context = build_harness_mcp_runtime_context(
        _env(
            tmp_path,
            **{
                MEMORY_ENABLED_ENV: "true",
                GENERATION_ENABLED_ENV: "true",
                EXECUTION_ENABLED_ENV: "true",
                MEMORY_WRITE_ENABLED_ENV: "true",
                EVOLUTION_ENABLED_ENV: "true",
            },
        )
    )

    assert context.commands is not None
    assert observed["workspace_root"] == tmp_path


@pytest.mark.asyncio
async def test_generation_enabled_is_advertised_as_approval_gated(
    tmp_path: Path,
) -> None:
    context = build_harness_mcp_runtime_context(
        _env(tmp_path, **{GENERATION_ENABLED_ENV: "true"})
    )
    adapter = CareMcpAdapter(
        context.backend,
        request_id_factory=lambda: "request-generation-context",
    )

    result = await adapter.call_tool("maestro_capabilities", {})
    capabilities = {
        item["name"]: item["availability"] for item in result["capabilities"]
    }

    assert capabilities["generate_chain"] == "approval_gated"
    assert capabilities["memory_read"] == "unavailable"


def test_memory_context_construction_does_not_connect_to_network(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "care.toml"
    config_path.write_text(
        "[memory]\nbase_url = 'http://127.0.0.1:65535'\n",
        encoding="utf-8",
    )

    def reject_network(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("network access during runtime context construction")

    monkeypatch.setattr(socket.socket, "connect", reject_network)

    context = build_harness_mcp_runtime_context(
        _env(
            tmp_path,
            **{
                MEMORY_ENABLED_ENV: "true",
                CONFIG_PATH_ENV: str(config_path),
            },
        )
    )

    assert context.memory is not None
    assert context.backend.memory_available is True


def test_config_failure_diagnostic_does_not_echo_secret(
    tmp_path: Path,
) -> None:
    secret = "super-secret-provider-token"
    config_path = tmp_path / "care.toml"
    config_path.write_text(
        f"[memory\napi_key = '{secret}'\n",
        encoding="utf-8",
    )

    with pytest.raises(HarnessMcpRuntimeContextError) as raised:
        build_harness_mcp_runtime_context(
            _env(
                tmp_path,
                **{
                    MEMORY_ENABLED_ENV: "true",
                    CONFIG_PATH_ENV: str(config_path),
                },
            )
        )

    assert raised.value.code == "care_config_load_failed"
    assert secret not in str(raised.value)
    assert str(config_path) not in str(raised.value)


def test_memory_factory_failure_diagnostic_is_redacted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "super-secret-provider-token"
    config_path = tmp_path / "care.toml"
    config_path.write_text("[memory]\n", encoding="utf-8")

    def fail_memory(
        _cls: type[runtime.CareMemory],
        _config: CareConfig,
    ) -> Any:
        raise RuntimeError(f"https://memory.invalid/?api_key={secret}")

    monkeypatch.setattr(
        runtime.CareMemory,
        "from_config",
        classmethod(fail_memory),
    )

    with pytest.raises(HarnessMcpRuntimeContextError) as raised:
        build_harness_mcp_runtime_context(
            _env(
                tmp_path,
                **{
                    MEMORY_ENABLED_ENV: "true",
                    CONFIG_PATH_ENV: str(config_path),
                },
            )
        )

    assert raised.value.code == "memory_initialization_failed"
    assert secret not in str(raised.value)
    assert "memory.invalid" not in str(raised.value)


def test_artifact_store_failure_is_actionable_without_echoing_path(
    tmp_path: Path,
) -> None:
    artifact_root = tmp_path / "this-is-a-file"
    artifact_root.write_text("not a directory", encoding="utf-8")

    with pytest.raises(HarnessMcpRuntimeContextError) as raised:
        build_harness_mcp_runtime_context(
            {
                SESSION_ID_ENV: "session-01",
                ARTIFACT_ROOT_ENV: str(artifact_root),
                WORKSPACE_ROOT_ENV: str(tmp_path),
            }
        )

    assert raised.value.code == "artifact_store_initialization_failed"
    assert str(artifact_root) not in str(raised.value)
    assert "writable" in str(raised.value)
