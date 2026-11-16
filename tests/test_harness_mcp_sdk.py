"""Wire-contract tests for CARE's required official MCP Python 1.x SDK.

Distributed CARE installs require ``mcp>=1.28,<2``. ``importorskip`` remains a
defensive collection guard for source-only test environments that did not sync
the declared project dependencies.
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("mcp")

from mcp import ClientSession, StdioServerParameters  # noqa: E402
from mcp.client.stdio import stdio_client  # noqa: E402
from mcp.shared.exceptions import McpError  # noqa: E402
from mcp.shared.memory import create_connected_server_and_client_session  # noqa: E402
from mcp.types import (  # noqa: E402
    CancelledNotification,
    CancelledNotificationParams,
    ClientNotification,
)

from care.harness_mcp import (  # noqa: E402
    CareMcpAdapter,
    MemoryGetChainData,
    MemoryGetChainRequest,
    MemorySearchData,
    MemorySearchRequest,
    ValidateChainData,
    ValidateChainRequest,
    build_mcp_server,
)
import care.harness_mcp as harness_mcp_module  # noqa: E402
import care.harness_mcp_runtime as harness_runtime_module  # noqa: E402
import care.runtime.keystore as keystore_module  # noqa: E402
from care.harness_mcp_runtime import (  # noqa: E402
    ARTIFACT_ROOT_ENV,
    CONFIG_PATH_ENV,
    GENERATION_ENABLED_ENV,
    MEMORY_ENABLED_ENV,
    SESSION_ID_ENV,
    WORKSPACE_ROOT_ENV,
)
from care.runtime.cancellation import CancellationToken  # noqa: E402
from care.runtime.credential_broker import BROKER_BOOTSTRAP_FD_ENV  # noqa: E402


class WireBackend:
    memory_available = True

    async def validate_chain(
        self,
        request: ValidateChainRequest,
        *,
        cancellation: CancellationToken,
    ) -> ValidateChainData:
        return ValidateChainData(
            parsed=True,
            ok=True,
            preflight_available=True,
        )

    async def search_memory(
        self,
        request: MemorySearchRequest,
        *,
        cancellation: CancellationToken,
    ) -> MemorySearchData:
        return MemorySearchData(status="completed", hits=[])

    async def get_memory_chain(
        self,
        request: MemoryGetChainRequest,
        *,
        cancellation: CancellationToken,
    ) -> MemoryGetChainData:
        return MemoryGetChainData(
            status="completed",
            entity_id=request.entity_id,
            channel=request.channel,
            chain={"version": "1.0", "steps": []},
        )


class BlockingWireBackend(WireBackend):
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.token: CancellationToken | None = None

    async def validate_chain(
        self,
        request: ValidateChainRequest,
        *,
        cancellation: CancellationToken,
    ) -> ValidateChainData:
        self.token = cancellation
        self.started.set()
        await asyncio.Future()
        raise AssertionError("unreachable")


def test_runtime_entrypoint_disables_keychain_ui_before_loading_config(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    calls: list[str] = []

    class _Context:
        backend = WireBackend()

    def _disable_interaction() -> None:
        calls.append("guard")

    def _build_context() -> _Context:
        calls.append("config")
        return _Context()

    async def _run_stdio(_adapter: CareMcpAdapter | None = None) -> None:
        calls.append("stdio")

    monkeypatch.setenv(SESSION_ID_ENV, "guard-order-session")
    monkeypatch.setenv(ARTIFACT_ROOT_ENV, str(tmp_path / "artifacts"))
    monkeypatch.setattr(
        keystore_module,
        "disable_macos_keychain_interaction",
        _disable_interaction,
    )
    monkeypatch.setattr(
        harness_runtime_module,
        "build_harness_mcp_runtime_context",
        _build_context,
    )
    monkeypatch.setattr(harness_mcp_module, "run_stdio", _run_stdio)

    assert harness_mcp_module.main() == 0
    assert calls == ["guard", "config", "stdio"]


def test_runtime_entrypoint_fails_closed_when_required_broker_is_missing(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    calls: list[str] = []

    monkeypatch.setenv(SESSION_ID_ENV, "missing-broker-session")
    monkeypatch.setenv(ARTIFACT_ROOT_ENV, str(tmp_path / "artifacts"))
    monkeypatch.setenv(BROKER_BOOTSTRAP_FD_ENV, "17")
    monkeypatch.setattr(
        keystore_module,
        "disable_macos_keychain_interaction",
        lambda: calls.append("guard"),
    )
    monkeypatch.setattr(
        harness_runtime_module,
        "build_harness_mcp_runtime_context",
        lambda: calls.append("config"),
    )

    assert harness_mcp_module.main() == 2
    captured = capsys.readouterr()
    assert calls == ["guard"]
    assert captured.out == ""
    assert "credential_broker_bootstrap_failed" in captured.err
    assert "17" not in captured.err


@pytest.mark.asyncio
async def test_official_sdk_lists_and_returns_structured_content() -> None:
    server = build_mcp_server(
        CareMcpAdapter(WireBackend(), request_id_factory=lambda: "wire-1")
    )

    async with create_connected_server_and_client_session(server) as client:
        listed = await client.list_tools()
        result = await client.call_tool("maestro_capabilities", {})
        invalid = await client.call_tool("maestro_capabilities", {"extra": True})

    tools: dict[str, Any] = {tool.name: tool for tool in listed.tools}
    assert set(tools) == {
        "maestro_capabilities",
        "maestro_command_catalog",
        "maestro_command_review",
        "maestro_command_status",
        "maestro_command_confirm",
        "maestro_validate_chain",
        "maestro_memory_search",
        "maestro_memory_get_chain",
        "maestro_chain_input_contract",
        "maestro_input_adaptation_inspect",
        "maestro_input_adaptation_preview",
        "maestro_generate_chain",
        "maestro_stage_artifact",
        "maestro_execute_chain",
        "maestro_memory_write_chain",
        "maestro_evolution_start",
        "maestro_evolution_status",
        "maestro_evolution_cancel",
    }
    assert tools["maestro_capabilities"].inputSchema["additionalProperties"] is False
    assert tools["maestro_capabilities"].outputSchema is not None
    assert result.isError is not True
    assert result.structuredContent is not None
    assert result.structuredContent["request_id"] == "wire-1"
    assert invalid.isError is True


@pytest.mark.asyncio
async def test_official_sdk_cancel_notification_reaches_care_token() -> None:
    backend = BlockingWireBackend()
    server = build_mcp_server(CareMcpAdapter(backend))

    async with create_connected_server_and_client_session(server) as client:
        # Capture the next wire ID before call_tool allocates it.  The SDK does
        # not expose an ID-returning call API; this mirrors its own cancellation
        # conformance test and remains test-only private access.
        request_id = client._request_id  # type: ignore[attr-defined]
        task = asyncio.create_task(
            client.call_tool("maestro_validate_chain", {"chain": {}})
        )
        await backend.started.wait()
        await client.send_notification(
            ClientNotification(
                CancelledNotification(
                    params=CancelledNotificationParams(
                        requestId=request_id,
                        reason="test cancellation",
                    )
                )
            )
        )
        with pytest.raises(McpError, match="Request cancelled"):
            await task

        # Cancellation is scoped to one request; the session stays usable.
        follow_up = await client.call_tool("maestro_capabilities", {})

    assert backend.token is not None
    assert backend.token.is_cancelled is True
    assert follow_up.isError is not True


@pytest.mark.asyncio
async def test_module_entrypoint_round_trips_over_real_stdio_process() -> None:
    project_root = Path(__file__).resolve().parents[1]
    parameters = StdioServerParameters(
        command=sys.executable,
        args=["-P", "-s", "-m", "care.harness_mcp"],
        cwd=project_root,
    )

    with tempfile.TemporaryFile(mode="w+", encoding="utf-8") as stderr:
        async with stdio_client(parameters, errlog=stderr) as (read_stream, write_stream):
            async with ClientSession(read_stream, write_stream) as client:
                initialized = await client.initialize()
                listed = await client.list_tools()
                result = await client.call_tool("maestro_capabilities", {})
        stderr.seek(0)
        stderr_text = stderr.read()

    assert initialized.serverInfo.name == "maestro-care"
    assert len(listed.tools) == 18
    assert result.isError is not True
    assert result.structuredContent is not None
    assert result.structuredContent["transport"] == "stdio"
    assert stderr_text == ""


@pytest.mark.asyncio
async def test_real_stdio_process_advertises_host_enabled_generation(
    tmp_path: Path,
) -> None:
    shadow = tmp_path / "care"
    shadow.mkdir()
    (shadow / "__init__.py").write_text(
        "raise RuntimeError('workspace package shadowed trusted CARE')\n",
        encoding="utf-8",
    )
    config_path = tmp_path / "care.toml"
    config_path.write_text("", encoding="utf-8")
    child_env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("CARE_", "MAESTRO_HARNESS_"))
    }
    child_env.update(
        {
            SESSION_ID_ENV: "stdio-generation-session",
            ARTIFACT_ROOT_ENV: str(tmp_path / "artifacts"),
            WORKSPACE_ROOT_ENV: str(tmp_path),
            CONFIG_PATH_ENV: str(config_path),
            MEMORY_ENABLED_ENV: "false",
            GENERATION_ENABLED_ENV: "true",
        }
    )
    parameters = StdioServerParameters(
        command=sys.executable,
        args=["-P", "-s", "-m", "care.harness_mcp"],
        env=child_env,
        # Match the packaged fallback where the MCP cwd may be the selected
        # workspace.  ``-P`` must keep this fake package from shadowing CARE.
        cwd=tmp_path,
    )

    with tempfile.TemporaryFile(mode="w+", encoding="utf-8") as stderr:
        async with stdio_client(parameters, errlog=stderr) as (read_stream, write_stream):
            async with ClientSession(read_stream, write_stream) as client:
                await client.initialize()
                result = await client.call_tool("maestro_capabilities", {})
        stderr.seek(0)
        stderr_text = stderr.read()

    assert result.isError is not True
    assert result.structuredContent is not None
    capabilities = {
        item["name"]: item["availability"]
        for item in result.structuredContent["capabilities"]
    }
    assert capabilities["generate_chain"] == "approval_gated"
    assert capabilities["memory_read"] == "unavailable"
    assert stderr_text == ""
