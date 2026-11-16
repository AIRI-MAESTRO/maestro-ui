"""Narrow native-settings API exposed to the resident-agent WebView.

Every installed client plugin shares ``window.pywebview.api``.  Consequently a
browser-visible confirmation token is not sufficient authority to mutate
MAESTRO configuration: another plugin could replay it.  This facade keeps the
existing two-phase :class:`~care.desktop_settings.DesktopSettingsBridge`, but
adds a second, native-only approval boundary before the commit phase.

Replacement credentials are collected by a native secure-text dialog.  The
WebView receives only a short-lived opaque handle; the credential itself never
enters the DOM, React state, or the JavaScript bridge.  The approval callback
receives only redacted change descriptions.  If either native boundary is not
available, the corresponding operation fails closed.
"""

from __future__ import annotations

import hashlib
import json
import logging
import secrets
import sys
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from care.desktop_settings import (
    DesktopSettingsBridge,
    SettingsDispatch,
    SettingsLoadError,
)
from care.redaction import redact_sensitive_text


_log = logging.getLogger("care.harness_settings")

MAX_NATIVE_APPROVAL_CHARS = 8_000
DEFAULT_SECRET_HANDLE_TTL_SECONDS = 180.0
DEFAULT_MAX_SECRET_HANDLES = 32
DEFAULT_MAX_PREVIEWS = 64
MAX_NATIVE_SECRET_CHARS = 16_384

_SECRET_FIELD_LABELS = {
    "api_key": "Generation API key",
    "memory_api_key": "Memory API key",
    "platform_api_key": "Platform API key",
    "mutation_api_key": "Mutation API key",
    "validation_api_key": "Validation API key",
    "search_api_key": "Web search API key",
    "upload_api_key": "Upload API key",
}


@dataclass(frozen=True, slots=True)
class NativeSettingsApprovalRequest:
    """Secret-free content suitable for a native confirmation dialog."""

    title: str
    message: str
    changes: tuple[str, ...]


NativeSettingsApproval = Callable[[NativeSettingsApprovalRequest], bool]


@dataclass(frozen=True, slots=True)
class NativeSecretCaptureRequest:
    """Secret-free description for a native secure-input dialog."""

    field: str
    title: str
    message: str
    label: str


NativeSecretCapture = Callable[[NativeSecretCaptureRequest], str | None]


class MacOSNativeSecretCapture:
    """Collect one credential through an AppKit ``NSSecureTextField``.

    AppKit imports are deliberately lazy so importing the facade stays
    portable. JavaScript invokes the facade method, but only the resulting
    opaque handle crosses that boundary.
    """

    def __init__(self, *, timeout_seconds: float = 180.0) -> None:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self._timeout_seconds = timeout_seconds

    def __call__(self, request: NativeSecretCaptureRequest) -> str | None:
        if sys.platform != "darwin":
            raise RuntimeError("native secure input requires macOS")
        return _run_appkit_secure_prompt(
            request,
            timeout_seconds=self._timeout_seconds,
        )


class PyWebViewNativeSettingsApproval:
    """Late-bound adapter to pywebview's operating-system confirmation dialog.

    Construct this before ``webview.create_window``, pass it to the facade, and
    call :meth:`bind_window` with the returned window before ``webview.start``.
    Until then it fails closed.
    """

    def __init__(self) -> None:
        self._window: Any | None = None
        self._lock = threading.RLock()

    def bind_window(self, window: Any) -> None:
        if not callable(getattr(window, "create_confirmation_dialog", None)):
            raise TypeError("window must provide create_confirmation_dialog")
        with self._lock:
            self._window = window

    def __call__(self, request: NativeSettingsApprovalRequest) -> bool:
        with self._lock:
            window = self._window
        if window is None:
            raise RuntimeError("native settings approval window is not bound")
        change_text = "\n".join(f"• {change}" for change in request.changes)
        message = request.message
        if change_text:
            message = f"{message}\n\n{change_text}"
        safe_message = redact_sensitive_text(message)
        if len(safe_message) > MAX_NATIVE_APPROVAL_CHARS:
            raise RuntimeError("native settings approval summary is too long")
        return bool(window.create_confirmation_dialog(request.title, safe_message))


@dataclass(frozen=True, slots=True)
class _PreviewGrant:
    payload_digest: str
    changes: tuple[str, ...]
    secret_handles: tuple[str, ...]
    expires_at: float


@dataclass(frozen=True, slots=True)
class _SecretHandleGrant:
    field: str
    value: str
    expires_at: float


@dataclass(frozen=True, slots=True)
class _ConsumedSecretHandle:
    handle: str
    grant: _SecretHandleGrant


class _FacadeRequestError(ValueError):
    def __init__(self, code: str, message: str, *, status: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.status = status


class HarnessSettingsApi:
    """pywebview-safe facade for the MAESTRO settings client plugin."""

    def __init__(
        self,
        bridge: DesktopSettingsBridge,
        *,
        native_approval: NativeSettingsApproval | None = None,
        native_secret_capture: NativeSecretCapture | None = None,
        clock: Callable[[], float] = time.monotonic,
        secret_handle_factory: Callable[[], str] | None = None,
        secret_handle_ttl_seconds: float = DEFAULT_SECRET_HANDLE_TTL_SECONDS,
        max_secret_handles: int = DEFAULT_MAX_SECRET_HANDLES,
        max_previews: int = DEFAULT_MAX_PREVIEWS,
    ) -> None:
        if secret_handle_ttl_seconds <= 0:
            raise ValueError("secret_handle_ttl_seconds must be positive")
        if max_secret_handles <= 0:
            raise ValueError("max_secret_handles must be positive")
        if max_previews <= 0:
            raise ValueError("max_previews must be positive")
        self._bridge = bridge
        self._native_approval = native_approval
        self._native_secret_capture = native_secret_capture
        self._clock = clock
        self._secret_handle_factory = (
            secret_handle_factory or (lambda: secrets.token_urlsafe(32))
        )
        self._secret_handle_ttl_seconds = secret_handle_ttl_seconds
        self._max_secret_handles = max_secret_handles
        self._max_previews = max_previews
        self._lock = threading.RLock()
        self._previews: dict[str, _PreviewGrant] = {}
        self._secret_handles: dict[str, _SecretHandleGrant] = {}
        self._bound_secret_handles: dict[str, _SecretHandleGrant] = {}

    def maestro_settings_snapshot(self) -> dict[str, Any]:
        """Return a non-secret settings snapshot to the client plugin."""

        try:
            snapshot = dict(self._bridge.snapshot())
            snapshot["restart_policy"] = {
                "after_save": "full_app_restart",
                "current_agent_uses_startup_settings": True,
            }
            return _envelope(200, snapshot)
        except SettingsLoadError:
            return _safe_error(
                500,
                "settings_load_failed",
                "MAESTRO settings could not be loaded safely.",
            )
        except Exception as exc:  # noqa: BLE001 - backend text may hold a key
            _log.error(
                "native settings snapshot facade failed (%s)",
                type(exc).__name__,
                exc_info=False,
            )
            return _safe_error(
                500,
                "settings_load_failed",
                "MAESTRO settings could not be loaded safely.",
            )

    def maestro_settings_capture_secret(self, field: Any) -> dict[str, Any]:
        """Open native secure input and return only an opaque one-use handle."""

        if not isinstance(field, str) or field not in _SECRET_FIELD_LABELS:
            return _safe_error(
                400,
                "invalid_secret_field",
                "Unknown credential field.",
            )
        capture = self._native_secret_capture
        if capture is None:
            return _safe_error(
                503,
                "native_secret_input_unavailable",
                "Native secure input is unavailable; no credential was captured.",
            )
        label = _SECRET_FIELD_LABELS[field]
        request = NativeSecretCaptureRequest(
            field=field,
            title=f"Set {label}",
            message=(
                "The credential stays in native MAESTRO memory until you confirm "
                "Save. It is never sent to the web interface."
            ),
            label=label,
        )
        try:
            value = capture(request)
        except Exception as exc:  # noqa: BLE001 - native UI errors are unstable
            _log.error(
                "native secret capture failed (%s)",
                type(exc).__name__,
                exc_info=False,
            )
            return _safe_error(
                503,
                "native_secret_input_unavailable",
                "Native secure input failed; no credential was captured.",
            )
        if value is None:
            return _safe_error(
                403,
                "native_secret_input_cancelled",
                "Credential entry was cancelled.",
            )
        if not _valid_native_secret(value):
            return _safe_error(
                400,
                "invalid_secret",
                "The credential is empty, invalid, or too long.",
            )

        now = self._clock()
        with self._lock:
            self._prune(now)
            try:
                handle = self._new_secret_handle()
            except RuntimeError:
                return _safe_error(
                    503,
                    "native_secret_input_unavailable",
                    "A secure credential handle could not be created.",
                )
            while len(self._secret_handles) >= self._max_secret_handles:
                oldest = next(iter(self._secret_handles))
                self._secret_handles.pop(oldest, None)
            self._secret_handles[handle] = _SecretHandleGrant(
                field=field,
                value=value,
                expires_at=now + self._secret_handle_ttl_seconds,
            )
        return _envelope(
            200,
            {
                "ok": True,
                "captured": True,
                "secret_handle": handle,
                "expires_in_seconds": self._secret_handle_ttl_seconds,
            },
        )

    def maestro_settings_preview(self, settings: Any) -> dict[str, Any]:
        """Validate a candidate and return its one-use review challenge.

        This method deliberately has no commit path, even if the input happens
        to contain fields named ``confirmation`` or ``confirmation_token``.
        """

        if not isinstance(settings, Mapping):
            return _safe_error(400, "invalid_settings", "settings must be an object")
        consumed: tuple[_ConsumedSecretHandle, ...] = ()
        try:
            digest = _payload_digest(settings)
            hydrated, consumed = self._consume_secret_handles(settings)
            dispatch = self._bridge.update(hydrated)
        except _FacadeRequestError as exc:
            self._restore_secret_handles(consumed)
            return _safe_error(exc.status, exc.code, str(exc))
        except Exception as exc:  # noqa: BLE001 - input may contain credentials
            self._restore_secret_handles(consumed)
            _log.error(
                "native settings preview facade failed (%s)",
                type(exc).__name__,
                exc_info=False,
            )
            return _safe_error(
                500,
                "settings_preview_failed",
                "MAESTRO settings could not be previewed safely.",
            )

        token, ttl, changes = _confirmation_details(dispatch)
        if token is None:
            self._restore_secret_handles(consumed)
            return _dispatch_envelope(dispatch)
        now = self._clock()
        with self._lock:
            self._prune(now)
            self._drop_preview(token)
            while len(self._previews) >= self._max_previews:
                self._drop_preview(next(iter(self._previews)))
            bound_handles: list[str] = []
            for item in consumed:
                self._bound_secret_handles[item.handle] = _SecretHandleGrant(
                    field=item.grant.field,
                    value=item.grant.value,
                    expires_at=now + ttl,
                )
                bound_handles.append(item.handle)
            self._previews[token] = _PreviewGrant(
                payload_digest=digest,
                changes=changes,
                secret_handles=tuple(bound_handles),
                expires_at=now + ttl,
            )
        return _dispatch_envelope(dispatch)

    def maestro_settings_save(
        self,
        settings: Any,
        confirmation_token: Any = None,
    ) -> dict[str, Any]:
        """Commit an exact preview only after a user-visible native approval."""

        if not isinstance(settings, Mapping):
            return _safe_error(400, "invalid_settings", "settings must be an object")
        if not isinstance(confirmation_token, str) or not confirmation_token:
            return _safe_error(
                409,
                "confirmation_invalid",
                "Preview these settings again before saving.",
            )
        try:
            digest = _payload_digest(settings)
        except (TypeError, ValueError):
            return _safe_error(400, "invalid_settings", "settings must be JSON data")

        now = self._clock()
        with self._lock:
            self._prune(now)
            preview = self._previews.get(confirmation_token)
            if (
                preview is None
                or preview.expires_at <= now
                or preview.payload_digest != digest
            ):
                return _safe_error(
                    409,
                    "confirmation_invalid",
                    "Preview these exact settings again before saving.",
                )
            if any(
                handle not in self._bound_secret_handles
                for handle in preview.secret_handles
            ):
                self._drop_preview(confirmation_token)
                return _safe_error(
                    409,
                    "confirmation_invalid",
                    "Preview these exact settings again before saving.",
                )

            approval = self._native_approval
            if approval is None:
                return _safe_error(
                    503,
                    "native_confirmation_unavailable",
                    "Native confirmation is unavailable; no settings were changed.",
                )

            request = NativeSettingsApprovalRequest(
                title="Save MAESTRO settings?",
                message=(
                    "This writes MAESTRO configuration and may update credentials "
                    "in macOS Keychain. The current agent keeps its startup settings "
                    "until MAESTRO is fully quit and reopened."
                ),
                changes=preview.changes,
            )
            try:
                approved = approval(request) is True
            except Exception as exc:  # noqa: BLE001 - native UI errors are unstable
                _log.error(
                    "native settings approval failed (%s)",
                    type(exc).__name__,
                    exc_info=False,
                )
                return _safe_error(
                    503,
                    "native_confirmation_unavailable",
                    "Native confirmation failed; no settings were changed.",
                )

            # A native decision consumes the facade grant whether accepted or
            # rejected.  Reopening the dialog therefore requires a new preview.
            self._previews.pop(confirmation_token, None)
            secret_values: tuple[tuple[str, str], ...] = ()
            if approved:
                resolved: list[tuple[str, str]] = []
                for handle in preview.secret_handles:
                    grant = self._bound_secret_handles.pop(handle)
                    resolved.append((grant.field, grant.value))
                secret_values = tuple(resolved)
            else:
                for handle in preview.secret_handles:
                    self._bound_secret_handles.pop(handle, None)

        if not approved:
            return _safe_error(
                403,
                "native_confirmation_cancelled",
                "Save cancelled; no settings were changed.",
            )

        try:
            hydrated = _hydrate_preview_payload(settings, secret_values)
            dispatch = self._bridge.update(
                hydrated,
                confirmation_token=confirmation_token,
            )
            response = _dispatch_envelope(dispatch)
            if dispatch.status == 200:
                response["payload"]["restart_required"] = True
                response["payload"]["restart_scope"] = "full_app"
                response["payload"]["restart_message"] = (
                    "Quit and reopen MAESTRO before the current agent uses the "
                    "saved provider, model, credentials, or runtime settings."
                )
            return response
        except _FacadeRequestError as exc:
            return _safe_error(exc.status, exc.code, str(exc))
        except Exception as exc:  # noqa: BLE001 - input may contain credentials
            _log.error(
                "native settings commit facade failed (%s)",
                type(exc).__name__,
                exc_info=False,
            )
            return _safe_error(
                500,
                "settings_save_failed",
                "MAESTRO settings could not be saved safely.",
            )

    def _prune(self, now: float) -> None:
        expired = [
            token
            for token, preview in self._previews.items()
            if preview.expires_at <= now
        ]
        for token in expired:
            self._drop_preview(token)
        expired_handles = [
            handle
            for handle, grant in self._secret_handles.items()
            if grant.expires_at <= now
        ]
        for handle in expired_handles:
            self._secret_handles.pop(handle, None)
        expired_bound_handles = [
            handle
            for handle, grant in self._bound_secret_handles.items()
            if grant.expires_at <= now
        ]
        for handle in expired_bound_handles:
            self._bound_secret_handles.pop(handle, None)

    def _drop_preview(self, token: str) -> None:
        preview = self._previews.pop(token, None)
        if preview is None:
            return
        for handle in preview.secret_handles:
            self._bound_secret_handles.pop(handle, None)

    def _new_secret_handle(self) -> str:
        for _attempt in range(8):
            handle = self._secret_handle_factory()
            if (
                isinstance(handle, str)
                and len(handle) >= 16
                and handle not in self._secret_handles
                and handle not in self._bound_secret_handles
            ):
                return handle
        raise RuntimeError("secret handle factory did not produce a unique handle")

    def _consume_secret_handles(
        self,
        settings: Mapping[str, Any],
    ) -> tuple[dict[str, Any], tuple[_ConsumedSecretHandle, ...]]:
        raw = dict(settings)
        for field in _SECRET_FIELD_LABELS:
            if field in raw:
                raise _FacadeRequestError(
                    "plaintext_secret_forbidden",
                    "Credentials must be entered in the native secure dialog.",
                )

        now = self._clock()
        consumed: list[_ConsumedSecretHandle] = []
        with self._lock:
            self._prune(now)
            for field in _SECRET_FIELD_LABELS:
                handle_field = f"{field}_handle"
                action_field = f"{field}_action"
                handle = raw.pop(handle_field, None)
                action = raw.get(action_field, "keep")
                if handle is None:
                    if action == "replace":
                        raise _FacadeRequestError(
                            "secret_handle_required",
                            f"Use native secure input before replacing {field}.",
                        )
                    continue
                if action != "replace" or not isinstance(handle, str):
                    raise _FacadeRequestError(
                        "secret_handle_invalid",
                        "Credential handles are valid only for replace actions.",
                    )
                grant = self._secret_handles.get(handle)
                if grant is None or grant.expires_at <= now or grant.field != field:
                    raise _FacadeRequestError(
                        "secret_handle_invalid",
                        "The credential handle is invalid or expired; enter it again.",
                        status=409,
                    )
                consumed.append(_ConsumedSecretHandle(handle=handle, grant=grant))
            for item in consumed:
                self._secret_handles.pop(item.handle, None)
                raw[item.grant.field] = item.grant.value
        return raw, tuple(consumed)

    def _restore_secret_handles(
        self,
        consumed: tuple[_ConsumedSecretHandle, ...],
    ) -> None:
        if not consumed:
            return
        now = self._clock()
        with self._lock:
            self._prune(now)
            for item in consumed:
                if (
                    item.grant.expires_at > now
                    and item.handle not in self._secret_handles
                    and len(self._secret_handles) < self._max_secret_handles
                ):
                    self._secret_handles[item.handle] = item.grant


def create_harness_settings_api(
    *,
    workspace: Path | str,
    native_approval: NativeSettingsApproval | None = None,
    native_secret_capture: NativeSecretCapture | None = None,
    bridge: DesktopSettingsBridge | None = None,
) -> HarnessSettingsApi:
    """Build the facade root can attach as the native Harness ``js_api``."""

    resolved_bridge = bridge or DesktopSettingsBridge(cwd=Path(workspace))
    if native_secret_capture is None and sys.platform == "win32":
        from care.runtime.windows_secret_input import WindowsNativeSecretCapture

        native_secret_capture = WindowsNativeSecretCapture()
    return HarnessSettingsApi(
        resolved_bridge,
        native_approval=native_approval,
        native_secret_capture=(
            native_secret_capture or MacOSNativeSecretCapture()
        ),
    )


def _valid_native_secret(value: Any) -> bool:
    return (
        isinstance(value, str)
        and bool(value)
        and value == value.strip()
        and len(value) <= MAX_NATIVE_SECRET_CHARS
        and not any(ord(char) < 32 or ord(char) == 127 for char in value)
    )


def _hydrate_preview_payload(
    settings: Mapping[str, Any],
    secret_values: tuple[tuple[str, str], ...],
) -> dict[str, Any]:
    raw = dict(settings)
    for field in _SECRET_FIELD_LABELS:
        if field in raw:
            raise _FacadeRequestError(
                "plaintext_secret_forbidden",
                "Credentials must be entered in the native secure dialog.",
            )
        raw.pop(f"{field}_handle", None)
    for field, value in secret_values:
        raw[field] = value
    return raw


def _run_appkit_secure_prompt(
    request: NativeSecretCaptureRequest,
    *,
    timeout_seconds: float,
) -> str | None:
    try:
        from AppKit import (  # type: ignore[import-not-found]
            NSAlert,
            NSAlertFirstButtonReturn,
            NSApplication,
            NSSecureTextField,
        )
        from Foundation import NSOperationQueue, NSThread  # type: ignore[import-not-found]
    except Exception as exc:  # noqa: BLE001 - optional native dependency
        raise RuntimeError("AppKit secure input is unavailable") from exc

    def present() -> str | None:
        alert = NSAlert.alloc().init()
        alert.setMessageText_(request.title)
        alert.setInformativeText_(request.message)
        alert.addButtonWithTitle_("Use key")
        alert.addButtonWithTitle_("Cancel")
        field = NSSecureTextField.alloc().initWithFrame_(
            ((0.0, 0.0), (420.0, 28.0))
        )
        field.setPlaceholderString_(request.label)
        field.setStringValue_("")
        alert.setAccessoryView_(field)
        NSApplication.sharedApplication().activateIgnoringOtherApps_(True)
        try:
            if alert.runModal() != NSAlertFirstButtonReturn:
                return None
            return str(field.stringValue())
        finally:
            field.setStringValue_("")

    if bool(NSThread.isMainThread()):
        return present()

    completed = threading.Event()
    result: dict[str, Any] = {}

    def run_on_main() -> None:
        try:
            result["value"] = present()
        except BaseException as exc:  # noqa: BLE001 - shuttle to calling thread
            result["error"] = exc
        finally:
            completed.set()

    NSOperationQueue.mainQueue().addOperationWithBlock_(run_on_main)
    if not completed.wait(timeout_seconds):
        raise RuntimeError("native secure input timed out")
    error = result.get("error")
    if isinstance(error, BaseException):
        raise RuntimeError("native secure input failed") from error
    value = result.get("value")
    return value if isinstance(value, str) else None


def _payload_digest(settings: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        dict(settings),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _confirmation_details(
    dispatch: SettingsDispatch,
) -> tuple[str | None, float, tuple[str, ...]]:
    payload = dispatch.payload
    error = payload.get("error")
    confirmation = payload.get("confirmation")
    if (
        dispatch.status != 409
        or not isinstance(error, Mapping)
        or error.get("code") != "confirmation_required"
        or not isinstance(confirmation, Mapping)
        or not isinstance(confirmation.get("token"), str)
        or not confirmation["token"]
    ):
        return None, 0.0, ()
    raw_ttl = confirmation.get("ttl_seconds", 0)
    ttl = float(raw_ttl) if isinstance(raw_ttl, (int, float)) else 0.0
    if ttl <= 0:
        return None, 0.0, ()
    raw_changes = payload.get("changes")
    changes = tuple(
        str(change)[:1_024]
        for change in raw_changes
        if isinstance(change, str)
    ) if isinstance(raw_changes, list) else ()
    return str(confirmation["token"]), ttl, changes


def _dispatch_envelope(dispatch: SettingsDispatch) -> dict[str, Any]:
    return _envelope(dispatch.status, dispatch.payload)


def _envelope(status: int, payload: Mapping[str, Any]) -> dict[str, Any]:
    return {"status": int(status), "payload": dict(payload)}


def _safe_error(status: int, code: str, message: str) -> dict[str, Any]:
    return _envelope(
        status,
        {"ok": False, "error": {"code": code, "message": message}},
    )


__all__ = [
    "HarnessSettingsApi",
    "MacOSNativeSecretCapture",
    "NativeSecretCapture",
    "NativeSecretCaptureRequest",
    "NativeSettingsApproval",
    "NativeSettingsApprovalRequest",
    "PyWebViewNativeSettingsApproval",
    "create_harness_settings_api",
]
