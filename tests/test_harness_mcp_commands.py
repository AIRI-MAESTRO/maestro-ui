from __future__ import annotations

import asyncio
import json
import threading
from typing import Any

import pytest

from care.desktop_commands import CommandBridge, CommandDispatch
from care.harness_commands import HarnessCommandService
from care.harness_mcp import (
    CareMcpAdapter,
    CareReadOnlyMcpBackend,
    InvalidHarnessToolArguments,
)


class _StateBoundBridge:
    def __init__(self) -> None:
        self.used = False
        self.calls: list[tuple[str, str | None]] = []

    def registry(self) -> dict[str, Any]:
        return {
            "commands": {
                "doctor": {"risk": "read_only"},
                "mutate": {"risk": "filesystem_write_or_mutation"},
            }
        }

    def execute(
        self,
        command: str,
        *,
        confirmation_token: str | None = None,
        transcript: Any = None,
    ) -> CommandDispatch:
        del transcript
        self.calls.append((command, confirmation_token))
        if command == "doctor" and confirmation_token is None:
            return CommandDispatch(
                200,
                {"ok": True, "executed": True, "result": {"healthy": True}},
            )
        if command != "mutate":
            return CommandDispatch(
                400,
                {"ok": False, "executed": False, "error": {"code": "unknown"}},
            )
        if confirmation_token is None:
            return CommandDispatch(
                409,
                {
                    "ok": False,
                    "executed": False,
                    "error": {"code": "confirmation_required"},
                    "confirmation": {
                        "token": "capability-exact-123",
                        "ttl_seconds": 90.0,
                        "risk": "filesystem_write_or_mutation",
                        "reason": "Writes one reviewed file.",
                        "effects": ["write reviewed file"],
                    },
                },
            )
        if confirmation_token != "capability-exact-123" or self.used:
            return CommandDispatch(
                409,
                {
                    "ok": False,
                    "executed": False,
                    "error": {"code": "confirmation_invalid"},
                },
            )
        self.used = True
        return CommandDispatch(
            200,
            {
                "ok": True,
                "executed": True,
                "command": {"risk": "filesystem_write_or_mutation"},
                "result": {"written": True},
            },
        )


def _adapter(bridge: _StateBoundBridge) -> CareMcpAdapter:
    backend = CareReadOnlyMcpBackend(
        commands=HarnessCommandService(bridge),
    )
    return CareMcpAdapter(backend, request_id_factory=lambda: "command-request")


@pytest.mark.asyncio
async def test_command_catalog_and_read_only_review_are_available() -> None:
    bridge = _StateBoundBridge()
    adapter = _adapter(bridge)

    catalog = await adapter.call_tool("maestro_command_catalog", {})
    read = await adapter.call_tool(
        "maestro_command_review",
        {"command": "doctor"},
    )

    assert catalog["status"] == "completed"
    assert catalog["receipt"]["status"] == "ready"
    assert catalog["receipt"]["catalog"]["commands"]["doctor"]["risk"] == "read_only"
    assert read["status"] == "completed"
    assert read["outcome"] == "completed"
    assert read["payload"]["result"] == {"healthy": True}


@pytest.mark.asyncio
async def test_resident_catalog_does_not_advertise_desktop_local_slash_routes() -> None:
    adapter = CareMcpAdapter(
        CareReadOnlyMcpBackend(commands=HarnessCommandService(CommandBridge())),
        request_id_factory=lambda: "catalog-request",
    )

    result = await adapter.call_tool("maestro_command_catalog", {})
    entries = {row["name"]: row for row in result["receipt"]["catalog"]["slash_commands"]}
    local = [row for row in entries.values() if row["classification"] == "local"]

    assert len(local) == 19
    assert all(row["available"] is False for row in local)
    assert all(row["scope"] == "resident_host" for row in local)
    assert all(row["execution"] == "deepseek_harness" for row in local)
    assert "chat model" in entries["settings"]["replacement"]
    assert "CARE configuration" in entries["settings"]["replacement"]
    assert entries["library"]["available"] is True
    counts = result["receipt"]["catalog"]["counts"]
    assert counts["slash_host_managed"] == 19
    assert counts["slash_available_in_resident"] == 25


@pytest.mark.asyncio
async def test_review_exposes_only_a_public_challenge_id() -> None:
    bridge = _StateBoundBridge()
    adapter = _adapter(bridge)

    review = await adapter.call_tool(
        "maestro_command_review",
        {"command": "mutate"},
    )

    assert review["outcome"] == "confirmation_required"
    assert review["payload"].get("confirmation") is None
    confirmation = review["confirmation"]
    assert confirmation == {
        "challenge_id": confirmation["challenge_id"],
        "ttl_seconds": 90.0,
        "risk": "filesystem_write_or_mutation",
        "reason": "Writes one reviewed file.",
        "effects": ["write reviewed file"],
    }
    assert confirmation["challenge_id"] != "capability-exact-123"
    assert "capability-exact-123" not in json.dumps(review)


@pytest.mark.asyncio
async def test_confirm_consumes_exact_capability_and_replay_is_rejected() -> None:
    bridge = _StateBoundBridge()
    adapter = _adapter(bridge)
    review = await adapter.call_tool(
        "maestro_command_review",
        {"command": "mutate"},
    )

    arguments = {
        "command": "mutate",
        "challenge_id": review["confirmation"]["challenge_id"],
    }
    completed = await adapter.call_tool("maestro_command_confirm", arguments)
    replay = await adapter.call_tool("maestro_command_confirm", arguments)

    assert completed["outcome"] == "completed"
    assert completed["payload"]["result"] == {"written": True}
    assert replay["outcome"] == "rejected"
    assert replay["payload"]["error"]["code"] == "confirmation_invalid"


@pytest.mark.asyncio
async def test_challenge_is_bound_to_transcript_and_consumed_on_mismatch() -> None:
    bridge = _StateBoundBridge()
    adapter = _adapter(bridge)
    review = await adapter.call_tool(
        "maestro_command_review",
        {"command": "mutate", "transcript": {"revision": 1}},
    )
    challenge_id = review["confirmation"]["challenge_id"]

    mismatch = await adapter.call_tool(
        "maestro_command_confirm",
        {
            "command": "mutate",
            "transcript": {"revision": 2},
            "challenge_id": challenge_id,
        },
    )
    replay = await adapter.call_tool(
        "maestro_command_confirm",
        {
            "command": "mutate",
            "transcript": {"revision": 1},
            "challenge_id": challenge_id,
        },
    )

    assert mismatch["payload"]["error"]["code"] == "confirmation_invalid"
    assert replay["payload"]["error"]["code"] == "confirmation_invalid"
    assert bridge.used is False


class _BlockingBridge(_StateBoundBridge):
    def __init__(self) -> None:
        super().__init__()
        self.started = threading.Event()
        self.release = threading.Event()

    def execute(
        self,
        command: str,
        *,
        confirmation_token: str | None = None,
        transcript: Any = None,
    ) -> CommandDispatch:
        if command == "mutate" and confirmation_token is not None:
            self.started.set()
            if not self.release.wait(timeout=5):
                raise RuntimeError("test worker was not released")
        return super().execute(
            command,
            confirmation_token=confirmation_token,
            transcript=transcript,
        )


@pytest.mark.asyncio
async def test_cancelled_confirm_is_reconciled_before_retry() -> None:
    bridge = _BlockingBridge()
    adapter = _adapter(bridge)
    review = await adapter.call_tool(
        "maestro_command_review",
        {"command": "mutate"},
    )
    challenge_id = review["confirmation"]["challenge_id"]
    confirm = asyncio.create_task(
        adapter.call_tool(
            "maestro_command_confirm",
            {"command": "mutate", "challenge_id": challenge_id},
        )
    )
    assert await asyncio.to_thread(bridge.started.wait, 2)

    confirm.cancel()
    with pytest.raises(asyncio.CancelledError):
        await confirm

    blocked = await adapter.call_tool(
        "maestro_command_review",
        {"command": "mutate"},
    )
    assert blocked["payload"]["error"]["code"] == "command_reconciliation_required"
    assert blocked["payload"]["reconciliation"] == {
        "challenge_id": challenge_id,
        "state": "running",
    }

    running = await adapter.call_tool(
        "maestro_command_status",
        {"challenge_id": challenge_id},
    )
    assert running["reconciliation_state"] == "running"

    bridge.release.set()
    for _ in range(100):
        final = await adapter.call_tool(
            "maestro_command_status",
            {"challenge_id": challenge_id},
        )
        if final["reconciliation_state"] != "running":
            break
        await asyncio.sleep(0.01)

    assert final["reconciliation_state"] == "completed"
    assert final["invocation"]["outcome"] == "completed"
    assert final["invocation"]["payload"]["result"] == {"written": True}
    assert bridge.used is True


@pytest.mark.asyncio
async def test_review_has_no_model_supplied_approval_escape_hatch() -> None:
    adapter = _adapter(_StateBoundBridge())

    with pytest.raises(InvalidHarnessToolArguments):
        await adapter.call_tool(
            "maestro_command_review",
            {"command": "mutate", "approved": True},
        )
