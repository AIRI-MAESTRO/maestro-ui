"""Safe resident-harness access to the reviewed CARE command surface."""

from __future__ import annotations

import asyncio
import json
import threading
from typing import Any

from pydantic import ValidationError
import pytest

from care.desktop_commands import CommandBridge, CommandDispatch
from care.harness_commands import (
    MAX_CONFIRMATION_TOKEN_CHARS,
    MAX_TRANSCRIPT_JSON_CHARS,
    CommandReviewRequest,
    ConfirmedCommandRequest,
    HarnessCommandBridgeError,
    HarnessCommandService,
)
from care.runtime.cancellation import CancellationToken, CancelledByUserError


@pytest.mark.asyncio
async def test_catalog_exposes_complete_reviewed_cli_and_tui_registry() -> None:
    service = HarnessCommandService(CommandBridge())

    receipt = await service.catalog()

    assert receipt.status == "ready"
    assert receipt.catalog["policy_complete"] is True
    assert receipt.catalog["unclassified_routes"] == []
    assert receipt.catalog["counts"] == {
        "top_level": 33,
        "leaf_routes": 38,
        "aliases": 3,
        "slash_commands": 44,
        "slash_local": 19,
        "slash_cli": 3,
        "slash_backend": 4,
        "slash_domain": 18,
        "slash_unavailable": 0,
    }
    cli_names = {item["name"] for item in receipt.catalog["commands"]}
    slash_names = {item["name"] for item in receipt.catalog["slash_commands"]}
    assert {"generate", "run", "evolve", "memory", "doctor"} <= cli_names
    assert {"library", "evolution", "settings", "sessions", "subagents"} <= slash_names
    assert len(receipt.catalog_sha256) == 64
    assert receipt == await service.catalog()


@pytest.mark.asyncio
async def test_read_only_command_executes_during_review_without_token() -> None:
    calls: list[str] = []

    def catalog_handler(_args: Any, stdout: Any, _stderr: Any) -> int:
        calls.append("catalog")
        stdout.write("reviewed catalog\n")
        return 0

    service = HarnessCommandService(
        CommandBridge(handler_overrides={"catalog": catalog_handler})
    )

    receipt = await service.plan_or_execute(CommandReviewRequest(command="catalog"))

    assert receipt.phase == "review"
    assert receipt.outcome == "completed"
    assert receipt.confirmation is None
    assert receipt.payload["executed"] is True
    assert receipt.payload["stdout"] == "reviewed catalog\n"
    assert calls == ["catalog"]


@pytest.mark.asyncio
async def test_effectful_review_only_returns_exact_bridge_challenge() -> None:
    calls: list[str] = []

    def handler(args: Any, _stdout: Any, _stderr: Any) -> int:
        calls.append(args.entity_id)
        return 0

    service = HarnessCommandService(
        CommandBridge(
            handler_overrides={"favourite": handler},
            token_factory=lambda: "bridge-state-token",
        )
    )

    receipt = await service.plan_or_execute(
        CommandReviewRequest(command="favourite chain-a")
    )

    assert receipt.phase == "review"
    assert receipt.outcome == "confirmation_required"
    assert receipt.status_code == 409
    assert receipt.confirmation is not None
    assert receipt.confirmation.token == "bridge-state-token"
    assert receipt.confirmation.risk == "filesystem_write_or_mutation"
    assert receipt.payload["error"]["code"] == "confirmation_required"
    assert "confirmation" not in receipt.payload
    assert "bridge-state-token" not in repr(receipt)
    assert calls == []


@pytest.mark.asyncio
async def test_exact_token_executes_once_and_replay_is_rejected() -> None:
    calls: list[str] = []

    def handler(args: Any, stdout: Any, _stderr: Any) -> int:
        calls.append(args.entity_id)
        stdout.write("saved\n")
        return 0

    service = HarnessCommandService(
        CommandBridge(
            handler_overrides={"favourite": handler},
            token_factory=lambda: "one-use-token",
        )
    )
    review = await service.plan_or_execute(
        CommandReviewRequest(command="favourite chain-a")
    )
    assert review.confirmation is not None

    confirmed = await service.confirm_and_execute(
        ConfirmedCommandRequest(
            command="favourite chain-a",
            confirmation_token=review.confirmation.token,
        )
    )
    replay = await service.confirm_and_execute(
        ConfirmedCommandRequest(
            command="favourite chain-a",
            confirmation_token=review.confirmation.token,
        )
    )

    assert confirmed.outcome == "completed"
    assert confirmed.payload["stdout"] == "saved\n"
    assert confirmed.confirmation is None
    assert calls == ["chain-a"]
    assert replay.outcome == "rejected"
    assert replay.status_code == 409
    assert replay.payload["error"]["code"] == "confirmation_invalid"


@pytest.mark.asyncio
async def test_token_is_bound_to_exact_command() -> None:
    calls: list[str] = []

    def handler(args: Any, _stdout: Any, _stderr: Any) -> int:
        calls.append(args.entity_id)
        return 0

    service = HarnessCommandService(
        CommandBridge(
            handler_overrides={"favourite": handler},
            token_factory=lambda: "command-bound-token",
        )
    )
    review = await service.plan_or_execute(
        CommandReviewRequest(command="favourite chain-a")
    )
    assert review.confirmation is not None

    mismatch = await service.confirm_and_execute(
        ConfirmedCommandRequest(
            command="favourite chain-b",
            confirmation_token=review.confirmation.token,
        )
    )

    assert mismatch.outcome == "rejected"
    assert mismatch.payload["error"]["code"] == "confirmation_invalid"
    assert calls == []


@pytest.mark.asyncio
async def test_secret_command_values_and_outputs_are_redacted() -> None:
    secret = "sk-live-super-secret-value"

    def handler(args: Any, stdout: Any, stderr: Any, *, stdin: Any) -> int:
        assert stdin.read() == ""
        stdout.write(f"api_key={args.api_key}\n")
        stderr.write(f"Authorization: Bearer {args.api_key}\n")
        return 2

    service = HarnessCommandService(
        CommandBridge(
            handler_overrides={"init": handler},
            token_factory=lambda: "redaction-token",
        )
    )
    review_request = CommandReviewRequest(
        command=f"init --api-key {secret} --non-interactive"
    )
    review = await service.plan_or_execute(review_request)
    assert review.confirmation is not None
    confirmed_request = ConfirmedCommandRequest(
        command=review_request.command,
        confirmation_token=review.confirmation.token,
    )

    result = await service.confirm_and_execute(confirmed_request)

    rendered = result.model_dump_json()
    assert secret not in rendered
    assert "[redacted]" in rendered
    assert secret not in repr(review_request)
    assert "redaction-token" not in repr(confirmed_request)


class _ProjectionBridge:
    def registry(self) -> dict[str, Any]:
        return {"commands": [], "secret": "must-not-leak"}

    def execute(self, _command: Any, **_kwargs: Any) -> CommandDispatch:
        return CommandDispatch(
            200,
            {
                "ok": True,
                "executed": True,
                "command": {"risk": "read_only"},
                "nested": {
                    "api_key": "sk-unsafe-secret",
                    "unknown": object(),
                },
                "not_finite": float("nan"),
            },
        )


@pytest.mark.asyncio
async def test_outputs_are_json_safe_and_secret_named_fields_are_redacted() -> None:
    result = await HarnessCommandService(_ProjectionBridge()).plan_or_execute(
        CommandReviewRequest(command="catalog")
    )

    assert result.outcome == "completed"
    assert result.payload["nested"] == {
        "api_key": "[redacted]",
        "unknown": "<object>",
    }
    assert result.payload["not_finite"] is None
    json.loads(result.model_dump_json())


class _OversizedBridge:
    def registry(self) -> dict[str, Any]:
        return {"commands": ["x" * 2_000]}

    def execute(self, _command: Any, **_kwargs: Any) -> CommandDispatch:
        return CommandDispatch(
            200,
            {
                "ok": True,
                "executed": True,
                "command": {"risk": "read_only"},
                "stdout": "x" * 2_000,
            },
        )


@pytest.mark.asyncio
async def test_result_and_catalog_have_hard_serialized_bounds() -> None:
    service = HarnessCommandService(_OversizedBridge(), max_result_chars=1_024)

    result = await service.plan_or_execute(CommandReviewRequest(command="catalog"))

    assert result.outcome == "rejected"
    assert result.payload["error"]["code"] == "command_result_too_large"
    assert len(result.model_dump_json()) < 1_024
    with pytest.raises(HarnessCommandBridgeError) as raised:
        await service.catalog()
    assert raised.value.code == "catalog_too_large"


class _UnsafeEffectBridge:
    def registry(self) -> dict[str, Any]:
        return {"commands": []}

    def execute(self, _command: Any, **_kwargs: Any) -> CommandDispatch:
        return CommandDispatch(
            200,
            {
                "ok": True,
                "executed": True,
                "command": {"risk": "external_side_effect"},
            },
        )


@pytest.mark.asyncio
async def test_review_fails_closed_on_bridge_effect_gate_regression() -> None:
    result = await HarnessCommandService(_UnsafeEffectBridge()).plan_or_execute(
        CommandReviewRequest(command="future-command")
    )

    assert result.outcome == "rejected"
    assert result.payload["error"]["code"] == "effect_gate_bypassed"
    assert result.payload["executed"] is False


@pytest.mark.asyncio
async def test_pre_cancelled_request_never_reaches_bridge() -> None:
    calls: list[str] = []

    def handler(_args: Any, _stdout: Any, _stderr: Any) -> int:
        calls.append("called")
        return 0

    token = CancellationToken(reason="steered away")
    token.cancel()
    service = HarnessCommandService(
        CommandBridge(handler_overrides={"catalog": handler})
    )

    with pytest.raises(CancelledByUserError, match="steered away"):
        await service.plan_or_execute(
            CommandReviewRequest(command="catalog"),
            token,
        )
    assert calls == []


@pytest.mark.asyncio
async def test_mid_handler_cancel_is_reported_after_sync_handler_returns() -> None:
    started = threading.Event()
    release = threading.Event()
    calls: list[str] = []

    def handler(_args: Any, _stdout: Any, _stderr: Any) -> int:
        started.set()
        assert release.wait(timeout=2)
        calls.append("finished")
        return 0

    token = CancellationToken(reason="user cancelled command")
    service = HarnessCommandService(
        CommandBridge(handler_overrides={"catalog": handler})
    )
    task = asyncio.create_task(
        service.plan_or_execute(CommandReviewRequest(command="catalog"), token)
    )
    assert await asyncio.to_thread(started.wait, 2)
    token.cancel()
    release.set()

    with pytest.raises(CancelledByUserError, match="user cancelled command"):
        await task
    # CommandBridge is synchronous; cancellation cannot revoke an already
    # entered handler.  This is the service's documented ambiguity boundary.
    assert calls == ["finished"]


def test_request_models_are_strict_frozen_bounded_and_have_no_approved_field() -> None:
    request = CommandReviewRequest(command=" catalog ")
    assert request.command == " catalog "  # no token-binding normalisation
    with pytest.raises(ValidationError):
        CommandReviewRequest(command="catalog", approved=True)
    with pytest.raises(ValidationError):
        CommandReviewRequest(command="x" * 16_385)
    with pytest.raises(ValidationError):
        CommandReviewRequest(
            command="catalog",
            transcript="x" * (MAX_TRANSCRIPT_JSON_CHARS + 1),
        )
    with pytest.raises(ValidationError):
        ConfirmedCommandRequest(
            command="favourite x",
            confirmation_token="x" * (MAX_CONFIRMATION_TOKEN_CHARS + 1),
        )
    with pytest.raises(ValidationError):
        ConfirmedCommandRequest(
            command="favourite x",
            confirmation_token="line\nbreak",
        )
    with pytest.raises(ValidationError):
        request.command = "doctor"
    schemas = json.dumps(
        {
            "review": CommandReviewRequest.model_json_schema(),
            "confirm": ConfirmedCommandRequest.model_json_schema(),
        }
    )
    assert "approved" not in schemas


class _RaisingBridge:
    def registry(self) -> dict[str, Any]:
        raise RuntimeError("api_key=sk-should-never-leak")

    def execute(self, _command: Any, **_kwargs: Any) -> CommandDispatch:
        raise RuntimeError("Authorization: Bearer sk-should-never-leak")


@pytest.mark.asyncio
async def test_bridge_exceptions_are_value_redacted() -> None:
    service = HarnessCommandService(_RaisingBridge())

    result = await service.plan_or_execute(CommandReviewRequest(command="catalog"))

    assert result.outcome == "rejected"
    assert "sk-should-never-leak" not in result.model_dump_json()
    with pytest.raises(HarnessCommandBridgeError) as raised:
        await service.catalog()
    assert "sk-should-never-leak" not in str(raised.value)
