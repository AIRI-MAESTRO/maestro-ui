"""Transport-neutral access to the reviewed CARE command surface.

``CommandBridge`` is the security boundary for CLI/TUI parity: it owns the
reviewed registry, parses commands without a shell, classifies their effective
risk, and issues short-lived, one-use, state-bound confirmation tokens.  This
module deliberately does not duplicate any of that parser or policy logic.

The resident harness gets two invocation entry points:

* :meth:`HarnessCommandService.plan_or_execute` never accepts a confirmation
  token.  Read-only commands may execute; effectful commands can only return
  the exact challenge produced by ``CommandBridge``.
* :meth:`HarnessCommandService.confirm_and_execute` requires that exact token
  and passes it back to ``CommandBridge`` unchanged.  There is intentionally no
  boolean ``approved`` field.

Both paths are async so transports do not block the event loop.  The existing
bridge and its command handlers are synchronous, however, so cancellation is
best-effort: it is checked before dispatch and immediately after the worker
thread returns, but cannot interrupt a handler already executing.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping, Sequence
from datetime import date, datetime
from enum import Enum
import hashlib
import json
import math
from pathlib import Path
import re
from typing import Any, Literal, Protocol

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    field_validator,
    model_validator,
)

from care.desktop_commands import (
    MAX_COMMAND_CHARS,
    CommandBridge,
    CommandDispatch,
)
from care.redaction import redact_sensitive_text
from care.runtime.cancellation import CancellationToken


MAX_CONFIRMATION_TOKEN_CHARS = 512
MAX_TRANSCRIPT_JSON_CHARS = 256 * 1024
MAX_COMMAND_RESULT_CHARS = 512 * 1024
MAX_RESULT_STRING_CHARS = 128 * 1024
MAX_RESULT_ITEMS = 16_384
MAX_RESULT_DEPTH = 16
MAX_RESULT_KEY_CHARS = 256
MAX_CONFIRMATION_REASON_CHARS = 4_096
MAX_CONFIRMATION_EFFECTS = 64
MAX_CONFIRMATION_EFFECT_CHARS = 4_096

_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
_SAFE_FAILURE_TYPE_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_.]{0,159}")
_SECRET_KEY_RE = re.compile(
    r"(?ix)(?:^|[_-])(?:"
    r"api[_-]?key|api[_-]?token|access[_-]?token|refresh[_-]?token|"
    r"client[_-]?secret|password|passwd|secret|token|"
    r"confirmation[_-]?token"
    r")(?:$|[_-])"
)


class HarnessCommandModel(BaseModel):
    """Strict immutable base for transport models."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        allow_inf_nan=False,
    )


class CommandReviewRequest(HarnessCommandModel):
    """An invocation that cannot carry confirmation authority."""

    command: str = Field(min_length=1, max_length=MAX_COMMAND_CHARS, repr=False)
    transcript: JsonValue | None = Field(default=None, repr=False)

    @field_validator("command")
    @classmethod
    def validate_command(cls, value: str) -> str:
        if "\x00" in value:
            raise ValueError("command must not contain NUL characters")
        return value

    @model_validator(mode="after")
    def validate_transcript_budget(self) -> CommandReviewRequest:
        _validate_json_budget(
            self.transcript,
            max_chars=MAX_TRANSCRIPT_JSON_CHARS,
            label="transcript",
        )
        return self


class ConfirmedCommandRequest(HarnessCommandModel):
    """One exact bridge challenge retry.

    The token is intentionally not normalised or trimmed.  ``CommandBridge``
    binds it to the command and resolved state and consumes it once.
    """

    command: str = Field(min_length=1, max_length=MAX_COMMAND_CHARS, repr=False)
    confirmation_token: str = Field(
        min_length=1,
        max_length=MAX_CONFIRMATION_TOKEN_CHARS,
        repr=False,
    )
    transcript: JsonValue | None = Field(default=None, repr=False)

    @field_validator("command")
    @classmethod
    def validate_command(cls, value: str) -> str:
        if "\x00" in value:
            raise ValueError("command must not contain NUL characters")
        return value

    @field_validator("confirmation_token")
    @classmethod
    def validate_confirmation_token(cls, value: str) -> str:
        if _CONTROL_RE.search(value):
            raise ValueError("confirmation_token must not contain control characters")
        return value

    @model_validator(mode="after")
    def validate_transcript_budget(self) -> ConfirmedCommandRequest:
        _validate_json_budget(
            self.transcript,
            max_chars=MAX_TRANSCRIPT_JSON_CHARS,
            label="transcript",
        )
        return self


CommandRiskValue = Literal[
    "filesystem_write_or_mutation",
    "external_side_effect",
    "provider_or_tool_execution",
]


class CommandConfirmationChallenge(HarnessCommandModel):
    """Exact one-use authority plus a value-redacted review summary."""

    token: str = Field(
        min_length=1,
        max_length=MAX_CONFIRMATION_TOKEN_CHARS,
        repr=False,
    )
    ttl_seconds: float = Field(gt=0, le=86_400)
    risk: CommandRiskValue
    reason: str = Field(min_length=1, max_length=MAX_CONFIRMATION_REASON_CHARS)
    effects: tuple[str, ...] = Field(default=(), max_length=MAX_CONFIRMATION_EFFECTS)

    @field_validator("token")
    @classmethod
    def validate_token(cls, value: str) -> str:
        if _CONTROL_RE.search(value):
            raise ValueError("challenge token contains control characters")
        return value

    @field_validator("effects", mode="before")
    @classmethod
    def freeze_effects(cls, value: Any) -> Any:
        if isinstance(value, list):
            return tuple(value)
        return value

    @field_validator("effects")
    @classmethod
    def validate_effects(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        for effect in value:
            if not effect or len(effect) > MAX_CONFIRMATION_EFFECT_CHARS:
                raise ValueError("confirmation effects must be non-empty and bounded")
        return value


class CommandCatalogReceipt(HarnessCommandModel):
    """A detached snapshot of the complete reviewed CLI/TUI registry."""

    status: Literal["ready"] = "ready"
    catalog_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    catalog: dict[str, JsonValue]


CommandInvocationOutcome = Literal[
    "completed",
    "confirmation_required",
    "rejected",
]
CommandInvocationPhase = Literal["review", "confirm"]


class CommandInvocationReceipt(HarnessCommandModel):
    """Bounded, redacted result of one bridge dispatch."""

    phase: CommandInvocationPhase
    outcome: CommandInvocationOutcome
    status_code: int = Field(ge=100, le=599)
    payload: dict[str, JsonValue]
    confirmation: CommandConfirmationChallenge | None = None

    @model_validator(mode="after")
    def validate_confirmation_shape(self) -> CommandInvocationReceipt:
        required = self.outcome == "confirmation_required"
        if required != (self.confirmation is not None):
            raise ValueError(
                "confirmation is required exactly for confirmation_required receipts"
            )
        if self.phase == "confirm" and self.confirmation is not None:
            raise ValueError("a confirmed retry must never issue a new challenge")
        return self


class HarnessCommandBridgeError(RuntimeError):
    """Stable value-redacted failure outside a normal command dispatch."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class _CommandBridgeProtocol(Protocol):
    def registry(self) -> dict[str, Any]: ...

    def execute(
        self,
        command: Any,
        *,
        confirmation_token: Any = None,
        transcript: Any = None,
    ) -> CommandDispatch: ...


class HarnessCommandService:
    """Async, shell-free façade over :class:`CommandBridge`.

    ``bridge`` is injectable for deterministic tests.  Production callers
    normally construct ``HarnessCommandService()`` and receive the same parser,
    route registry, domain adapters, and confirmation ledger as the native
    workbench.
    """

    def __init__(
        self,
        bridge: _CommandBridgeProtocol | None = None,
        *,
        max_result_chars: int = MAX_COMMAND_RESULT_CHARS,
    ) -> None:
        if (
            not isinstance(max_result_chars, int)
            or isinstance(max_result_chars, bool)
            or max_result_chars < 1_024
            or max_result_chars > 4 * 1024 * 1024
        ):
            raise ValueError("max_result_chars must be an integer from 1024 to 4194304")
        self._bridge = bridge or CommandBridge()
        if not callable(getattr(self._bridge, "registry", None)) or not callable(
            getattr(self._bridge, "execute", None)
        ):
            raise TypeError("bridge must expose public registry() and execute() methods")
        self._max_result_chars = max_result_chars

    async def catalog(
        self,
        cancellation: CancellationToken | None = None,
    ) -> CommandCatalogReceipt:
        """Return the full reviewed CLI/TUI registry as finite JSON."""

        token = _require_cancellation(cancellation)
        try:
            raw = await _run_sync(self._bridge.registry, token)
            catalog = _project_mapping(raw)
            canonical = _bounded_canonical_json(
                catalog,
                max_chars=self._max_result_chars,
                code="catalog_too_large",
            )
        except HarnessCommandBridgeError:
            raise
        except Exception as exc:  # noqa: BLE001 - convert to stable redacted boundary
            raise _bridge_error("catalog_failed", exc) from None
        return CommandCatalogReceipt(
            catalog_sha256=hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
            catalog=catalog,
        )

    async def plan_or_execute(
        self,
        request: CommandReviewRequest,
        cancellation: CancellationToken | None = None,
    ) -> CommandInvocationReceipt:
        """Execute a read or return an effectful command's bridge challenge."""

        if not isinstance(request, CommandReviewRequest):
            raise TypeError("request must be a CommandReviewRequest")
        token = _require_cancellation(cancellation)
        transcript = _clone_json(request.transcript)
        try:
            dispatch = await _run_sync(
                lambda: self._bridge.execute(
                    request.command,
                    transcript=transcript,
                ),
                token,
            )
        except Exception as exc:  # noqa: BLE001 - transport must not leak failures
            return self._failure_receipt("review", "command_bridge_failed", exc)
        return self._receipt_from_dispatch(dispatch, phase="review")

    async def confirm_and_execute(
        self,
        request: ConfirmedCommandRequest,
        cancellation: CancellationToken | None = None,
    ) -> CommandInvocationReceipt:
        """Retry one command with the exact state-bound bridge token."""

        if not isinstance(request, ConfirmedCommandRequest):
            raise TypeError("request must be a ConfirmedCommandRequest")
        token = _require_cancellation(cancellation)
        transcript = _clone_json(request.transcript)
        try:
            dispatch = await _run_sync(
                lambda: self._bridge.execute(
                    request.command,
                    confirmation_token=request.confirmation_token,
                    transcript=transcript,
                ),
                token,
            )
        except Exception as exc:  # noqa: BLE001 - transport must not leak failures
            return self._failure_receipt(
                "confirm",
                "command_bridge_failed",
                exc,
                supplied_secrets=(request.confirmation_token,),
            )
        return self._receipt_from_dispatch(
            dispatch,
            phase="confirm",
            supplied_secrets=(request.confirmation_token,),
        )

    def _receipt_from_dispatch(
        self,
        dispatch: Any,
        *,
        phase: CommandInvocationPhase,
        supplied_secrets: tuple[str, ...] = (),
    ) -> CommandInvocationReceipt:
        if not isinstance(dispatch, CommandDispatch):
            return self._failure_receipt(
                phase,
                "invalid_bridge_result",
                TypeError("bridge returned a non-CommandDispatch value"),
                supplied_secrets=supplied_secrets,
            )
        if (
            not isinstance(dispatch.status, int)
            or isinstance(dispatch.status, bool)
            or not 100 <= dispatch.status <= 599
            or not isinstance(dispatch.payload, Mapping)
        ):
            return self._failure_receipt(
                phase,
                "invalid_bridge_result",
                TypeError("bridge returned an invalid dispatch shape"),
                supplied_secrets=supplied_secrets,
            )

        raw_payload = dict(dispatch.payload)
        challenge = None
        if _is_confirmation_required(dispatch.status, raw_payload):
            if phase == "confirm":
                # A retry never gets to mint fresh authority.  Wrong, stale,
                # replayed, or state-mismatched tokens must be returned by the
                # bridge as confirmation_invalid instead.
                return self._failure_receipt(
                    phase,
                    "invalid_bridge_confirmation_flow",
                    RuntimeError("confirmed retry returned a new challenge"),
                    supplied_secrets=supplied_secrets,
                )
            try:
                challenge = _extract_challenge(raw_payload)
            except (TypeError, ValueError) as exc:
                return self._failure_receipt(
                    phase,
                    "invalid_bridge_challenge",
                    exc,
                    supplied_secrets=supplied_secrets,
                )
            raw_payload.pop("confirmation", None)

        try:
            payload = _project_mapping(
                raw_payload,
                supplied_secrets=supplied_secrets,
            )
            _bounded_canonical_json(
                payload,
                max_chars=self._max_result_chars,
                code="command_result_too_large",
            )
        except HarnessCommandBridgeError as exc:
            return self._failure_receipt(
                phase,
                exc.code,
                exc,
                supplied_secrets=supplied_secrets,
            )

        if challenge is not None:
            if raw_payload.get("executed") is True:
                return self._failure_receipt(
                    phase,
                    "invalid_bridge_confirmation_flow",
                    RuntimeError("effectful challenge was marked executed"),
                    supplied_secrets=supplied_secrets,
                )
            outcome: CommandInvocationOutcome = "confirmation_required"
        elif phase == "review" and _reports_effectful_execution(raw_payload):
            # CommandBridge is expected to make this impossible.  Keep an
            # explicit fail-closed assertion at the transport boundary so a
            # future bridge regression is never represented as a safe read.
            return self._failure_receipt(
                phase,
                "effect_gate_bypassed",
                RuntimeError("effectful command executed without a challenge"),
                supplied_secrets=supplied_secrets,
            )
        elif raw_payload.get("ok") is True:
            outcome = "completed"
        else:
            outcome = "rejected"
        return CommandInvocationReceipt(
            phase=phase,
            outcome=outcome,
            status_code=dispatch.status,
            payload=payload,
            confirmation=challenge,
        )

    def _failure_receipt(
        self,
        phase: CommandInvocationPhase,
        code: str,
        exc: BaseException,
        *,
        supplied_secrets: tuple[str, ...] = (),
    ) -> CommandInvocationReceipt:
        failure_type = _safe_failure_type(exc)
        payload: dict[str, JsonValue] = {
            "ok": False,
            "executed": False,
            "error": {
                "code": redact_sensitive_text(code, supplied_secrets, max_chars=128),
                "message": "The CARE command bridge refused this invocation safely.",
                "failure_type": failure_type,
            },
        }
        return CommandInvocationReceipt(
            phase=phase,
            outcome="rejected",
            status_code=500,
            payload=payload,
        )


async def _run_sync(
    call: Callable[[], Any],
    cancellation: CancellationToken,
) -> Any:
    """Run one synchronous bridge call without blocking the event loop.

    The bridge does not accept a cancellation token.  A cancel that arrives
    after dispatch therefore becomes visible only once the handler returns.
    This is an explicit ambiguity boundary for effectful commands.
    """

    cancellation.raise_if_cancelled()
    result = await asyncio.to_thread(call)
    cancellation.raise_if_cancelled()
    return result


def _require_cancellation(value: CancellationToken | None) -> CancellationToken:
    if value is None:
        return CancellationToken(reason="resident command invocation cancelled")
    if not isinstance(value, CancellationToken):
        raise TypeError("cancellation must be a CancellationToken")
    return value


def _is_confirmation_required(status: int, payload: Mapping[str, Any]) -> bool:
    error = payload.get("error")
    return (
        status == 409
        and isinstance(error, Mapping)
        and error.get("code") == "confirmation_required"
        and isinstance(payload.get("confirmation"), Mapping)
    )


def _extract_challenge(payload: Mapping[str, Any]) -> CommandConfirmationChallenge:
    raw = payload.get("confirmation")
    if not isinstance(raw, Mapping):
        raise TypeError("bridge challenge is missing")
    raw_reason = raw.get("reason")
    if not isinstance(raw_reason, str):
        raise TypeError("bridge challenge reason must be text")
    reason = redact_sensitive_text(
        raw_reason,
        max_chars=MAX_CONFIRMATION_REASON_CHARS,
    )
    raw_effects = raw.get("effects", payload.get("effects", ()))
    if not isinstance(raw_effects, Sequence) or isinstance(
        raw_effects,
        (str, bytes, bytearray),
    ):
        raise TypeError("bridge challenge effects must be an array")
    if any(not isinstance(effect, str) for effect in raw_effects):
        raise TypeError("bridge challenge effects must contain only text")
    effects = tuple(
        redact_sensitive_text(effect, max_chars=MAX_CONFIRMATION_EFFECT_CHARS)
        for effect in raw_effects
    )
    return CommandConfirmationChallenge(
        token=raw.get("token"),
        ttl_seconds=raw.get("ttl_seconds"),
        risk=raw.get("risk"),
        reason=reason,
        effects=effects,
    )


def _project_mapping(
    value: Any,
    *,
    supplied_secrets: tuple[str, ...] = (),
) -> dict[str, JsonValue]:
    if not isinstance(value, Mapping):
        raise TypeError("bridge value must be a mapping")
    budget = _ProjectionBudget()
    projected = _project_json(value, 0, budget, supplied_secrets)
    if not isinstance(projected, dict):
        raise TypeError("bridge value did not project to an object")
    return projected


class _ProjectionBudget:
    def __init__(self) -> None:
        self.remaining_items = MAX_RESULT_ITEMS

    def consume(self) -> bool:
        if self.remaining_items <= 0:
            return False
        self.remaining_items -= 1
        return True


def _project_json(
    value: Any,
    depth: int,
    budget: _ProjectionBudget,
    supplied_secrets: tuple[str, ...],
) -> JsonValue:
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, str):
        return redact_sensitive_text(
            value,
            supplied_secrets,
            max_chars=MAX_RESULT_STRING_CHARS,
        )
    if isinstance(value, (bytes, bytearray, memoryview)):
        return redact_sensitive_text(
            bytes(value).decode("utf-8", errors="replace"),
            supplied_secrets,
            max_chars=MAX_RESULT_STRING_CHARS,
        )
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Path):
        return redact_sensitive_text(
            value,
            supplied_secrets,
            max_chars=MAX_RESULT_STRING_CHARS,
        )
    if isinstance(value, Enum):
        return _project_json(value.value, depth, budget, supplied_secrets)
    if depth >= MAX_RESULT_DEPTH:
        return f"<{type(value).__name__}: max depth reached>"
    if isinstance(value, Mapping):
        output: dict[str, JsonValue] = {}
        for key, item in value.items():
            if not budget.consume():
                output["__truncated__"] = "result item limit reached"
                break
            if isinstance(key, str):
                safe_key = redact_sensitive_text(
                    key,
                    supplied_secrets,
                    max_chars=MAX_RESULT_KEY_CHARS,
                )
            else:
                safe_key = f"<{type(key).__name__}>"
            if _SECRET_KEY_RE.search(safe_key):
                output[safe_key] = "[redacted]"
            else:
                output[safe_key] = _project_json(
                    item,
                    depth + 1,
                    budget,
                    supplied_secrets,
                )
        return output
    if isinstance(value, Sequence) and not isinstance(value, str):
        output_list: list[JsonValue] = []
        for item in value:
            if not budget.consume():
                output_list.append("<result item limit reached>")
                break
            output_list.append(
                _project_json(item, depth + 1, budget, supplied_secrets)
            )
        return output_list
    # Never call arbitrary ``str``/``repr`` methods at the transport boundary.
    return f"<{type(value).__name__}>"


def _clone_json(value: JsonValue | None) -> JsonValue | None:
    if value is None:
        return None
    return json.loads(
        json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        )
    )


def _validate_json_budget(value: JsonValue | None, *, max_chars: int, label: str) -> None:
    if value is None:
        return
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        )
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be finite JSON") from exc
    if len(encoded) > max_chars:
        raise ValueError(f"{label} exceeds {max_chars} JSON characters")


def _bounded_canonical_json(
    value: Mapping[str, JsonValue],
    *,
    max_chars: int,
    code: str,
) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    if len(encoded) > max_chars:
        raise HarnessCommandBridgeError(
            code,
            "The projected command result exceeds the configured transport bound.",
        )
    return encoded


def _safe_failure_type(exc: BaseException) -> str:
    value = type(exc).__name__
    if _SAFE_FAILURE_TYPE_RE.fullmatch(value) is None:
        return "CommandBridgeError"
    return value


def _reports_effectful_execution(payload: Mapping[str, Any]) -> bool:
    if payload.get("executed") is not True:
        return False
    command = payload.get("command")
    if not isinstance(command, Mapping):
        return False
    return command.get("risk") in {
        "filesystem_write_or_mutation",
        "external_side_effect",
        "provider_or_tool_execution",
    }


def _bridge_error(code: str, exc: BaseException) -> HarnessCommandBridgeError:
    return HarnessCommandBridgeError(
        code,
        f"The CARE command bridge failed safely ({_safe_failure_type(exc)}).",
    )


__all__ = [
    "CommandCatalogReceipt",
    "CommandConfirmationChallenge",
    "CommandInvocationReceipt",
    "CommandReviewRequest",
    "ConfirmedCommandRequest",
    "HarnessCommandBridgeError",
    "HarnessCommandService",
    "MAX_COMMAND_RESULT_CHARS",
    "MAX_CONFIRMATION_TOKEN_CHARS",
    "MAX_TRANSCRIPT_JSON_CHARS",
]
