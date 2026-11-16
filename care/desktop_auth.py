"""Opt-in local authentication for the native MAESTRO window.

The app lock is deliberately separate from provider credentials and from the
headless/TUI configuration.  Its default is ``disabled``.  When enabled on
macOS it asks LocalAuthentication to authenticate the current device owner:

* ``touch-id`` uses biometric authentication only.  MAESTRO suppresses the
  framework's fallback button and never asks for a password.
* legacy ``device-owner`` configuration is narrowed to ``touch-id`` at the
  desktop-launch boundary.  MAESTRO never requests the macOS password-fallback
  policy.

There is intentionally no MAESTRO-owned password or password verifier in this
module.  A future app-password fallback would require its own explicit secure
storage and UX review rather than silently broadening ``device-owner``.
"""

from __future__ import annotations

import os
import stat
import sys
import tempfile
import threading
import tomllib
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol

from care.redaction import redact_sensitive_text


DEFAULT_DESKTOP_AUTH_CONFIG_PATH = Path("~/.config/care/desktop-auth.toml").expanduser()
APP_LOCK_CONFIG_ENV = "MAESTRO_APP_LOCK_CONFIG"
APP_LOCK_MODE_ENV = "MAESTRO_APP_LOCK_MODE"
LOCAL_AUTHENTICATION_REQUIREMENT = "pyobjc-framework-LocalAuthentication==12.2.2"

_CONFIG_KEYS = frozenset({"mode"})
_CONFIG_LIMIT_BYTES = 4_096
_DEFAULT_REASON = "Unlock MAESTRO to continue."
_DEFAULT_TIMEOUT_SECONDS = 120.0
_AUTH_FAILURE_RECOVERY = (
    "Reopen MAESTRO and try again. To recover from an unavailable app lock, "
    'set mode = "disabled" in ~/.config/care/desktop-auth.toml.'
)


class DesktopAuthMode(StrEnum):
    """Supported launch-lock policies."""

    DISABLED = "disabled"
    TOUCH_ID = "touch-id"
    DEVICE_OWNER = "device-owner"


class DesktopAuthError(RuntimeError):
    """Base class for safe, user-facing app-lock failures."""


class DesktopAuthConfigurationError(DesktopAuthError):
    """The app-lock configuration is invalid or unreadable."""


class DesktopAuthUnavailableError(DesktopAuthError):
    """The configured macOS authentication policy cannot be evaluated."""


class DesktopAuthDeniedError(DesktopAuthError):
    """The user did not complete device-owner authentication."""


class DesktopAuthTimeoutError(DesktopAuthDeniedError):
    """The system authentication request did not finish in time."""


@dataclass(frozen=True, slots=True)
class DesktopAuthConfig:
    """Resolved app-lock configuration for one desktop launch."""

    mode: DesktopAuthMode = DesktopAuthMode.DISABLED
    path: Path = DEFAULT_DESKTOP_AUTH_CONFIG_PATH
    overridden_by_environment: bool = False

    @classmethod
    def load(
        cls,
        path: str | Path | None = None,
        environ: Mapping[str, str] | None = None,
    ) -> "DesktopAuthConfig":
        """Load the mode from TOML, then apply the explicit environment override.

        An absent file is the secure-by-default *feature* default: the lock is
        disabled rather than unexpectedly making a headless installation
        unusable.  Once a file or environment override opts in, malformed
        configuration fails closed at native-app launch.
        """

        environment = os.environ if environ is None else environ
        resolved_path = _resolve_config_path(path, environment)
        mode = DesktopAuthMode.DISABLED
        if resolved_path.exists():
            document = _read_config_document(resolved_path)
            unknown = set(document) - _CONFIG_KEYS
            if unknown:
                names = ", ".join(sorted(unknown))
                raise DesktopAuthConfigurationError(
                    f"desktop app-lock config contains unsupported fields: {names}"
                )
            raw_mode = document.get("mode", DesktopAuthMode.DISABLED.value)
            mode = _parse_mode(raw_mode, source=str(resolved_path))

        raw_override = environment.get(APP_LOCK_MODE_ENV)
        overridden = raw_override is not None
        if raw_override is not None:
            mode = _parse_mode(raw_override, source=APP_LOCK_MODE_ENV)
        return cls(
            mode=mode,
            path=resolved_path,
            overridden_by_environment=overridden,
        )


class DesktopAuthBackend(Protocol):
    """Synchronous boundary around one operating-system auth request."""

    def authenticate(
        self,
        mode: DesktopAuthMode,
        *,
        reason: str,
        timeout_seconds: float,
    ) -> bool:
        """Return ``True`` only after the configured policy succeeds."""


def save_desktop_auth_mode(
    mode: DesktopAuthMode | str,
    path: str | Path | None = None,
    *,
    environ: Mapping[str, str] | None = None,
) -> Path:
    """Atomically persist one non-secret app-lock mode with mode ``0600``.

    The file contains only ``mode = "..."``.  In particular, this API has no
    password, hash, verifier, or Keychain parameter for callers to misuse.
    """

    parsed = _parse_mode(mode, source="desktop app-lock setting")
    environment = os.environ if environ is None else environ
    destination = _resolve_config_path(path, environment)
    try:
        destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    except OSError as exc:
        raise DesktopAuthConfigurationError(
            f"desktop app-lock config directory is unavailable: {destination.parent}"
        ) from exc

    descriptor: int | None = None
    temporary: Path | None = None
    try:
        descriptor, raw_temporary = tempfile.mkstemp(
            prefix=f".{destination.name}.",
            suffix=".tmp",
            dir=destination.parent,
        )
        temporary = Path(raw_temporary)
        os.fchmod(descriptor, stat.S_IRUSR | stat.S_IWUSR)
        body = f'mode = "{parsed.value}"\n'
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            descriptor = None
            stream.write(body)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, destination)
        temporary = None
        os.chmod(destination, stat.S_IRUSR | stat.S_IWUSR)
    except OSError as exc:
        raise DesktopAuthConfigurationError(
            f"desktop app-lock config could not be saved: {destination}"
        ) from exc
    finally:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
    return destination


def authenticate_desktop_launch(
    config: DesktopAuthConfig | None = None,
    *,
    platform: str | None = None,
    backend_factory: Callable[[], DesktopAuthBackend] | None = None,
    reason: str = _DEFAULT_REASON,
    timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS,
) -> None:
    """Enforce the configured lock before a native window or agent is started.

    This function is safe to call on every platform: the default disabled mode
    returns before importing any macOS/PyObjC module.  An explicitly enabled
    lock on another OS fails closed with an actionable error; headless commands
    never call this desktop-only boundary.
    """

    resolved = DesktopAuthConfig.load() if config is None else config
    if resolved.mode is DesktopAuthMode.DISABLED:
        return
    effective_mode = (
        DesktopAuthMode.TOUCH_ID
        if resolved.mode is DesktopAuthMode.DEVICE_OWNER
        else resolved.mode
    )
    current_platform = sys.platform if platform is None else platform
    if current_platform != "darwin":
        raise DesktopAuthUnavailableError(
            "MAESTRO app lock is enabled, but device-owner authentication "
            "is available only in the native macOS application."
        )
    if timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be positive")

    factory = (
        MacOSLocalAuthenticationBackend if backend_factory is None else backend_factory
    )
    try:
        backend = factory()
        authenticated = backend.authenticate(
            effective_mode,
            reason=reason,
            timeout_seconds=timeout_seconds,
        )
    except DesktopAuthError:
        raise
    except Exception as exc:  # noqa: BLE001 - framework exceptions are unstable
        raise DesktopAuthUnavailableError(
            "macOS device-owner authentication could not be started."
        ) from exc
    if not authenticated:
        raise DesktopAuthDeniedError(
            "MAESTRO stayed locked because macOS authentication was not completed."
        )


def show_desktop_auth_failure(
    error: object,
    *,
    platform: str | None = None,
    appkit: Any | None = None,
) -> bool:
    """Show a bounded native failure alert after LocalAuthentication returns.

    Finder launches usually have no usable stderr.  Returning ``False`` keeps
    this notifier best-effort: authentication still fails closed even if Cocoa
    cannot present the explanation.  Production calls happen on the main
    thread; a non-main caller never attempts to manipulate AppKit.
    """

    current_platform = sys.platform if platform is None else platform
    if current_platform != "darwin":
        return False
    try:
        if appkit is None:
            import AppKit as appkit_module  # type: ignore[import-not-found]

            appkit = appkit_module
        thread_class = getattr(appkit, "NSThread", None)
        is_main = getattr(thread_class, "isMainThread", None)
        if callable(is_main) and not bool(is_main()):
            return False
        application = getattr(appkit, "NSApplication", None)
        shared_application = getattr(application, "sharedApplication", None)
        if callable(shared_application):
            shared_application()
        alert = appkit.NSAlert.alloc().init()
        set_style = getattr(alert, "setAlertStyle_", None)
        critical_style = getattr(appkit, "NSAlertStyleCritical", None)
        if callable(set_style) and critical_style is not None:
            set_style(critical_style)
        alert.setMessageText_("MAESTRO remains locked")
        detail = redact_sensitive_text(error, max_chars=768).strip()
        alert.setInformativeText_(
            f"{detail}\n\n{_AUTH_FAILURE_RECOVERY}"
            if detail
            else _AUTH_FAILURE_RECOVERY
        )
        alert.addButtonWithTitle_("Quit")
        alert.runModal()
    except Exception:  # noqa: BLE001 - a notifier must never weaken fail-closed auth
        return False
    return True


class MacOSLocalAuthenticationBackend:
    """PyObjC adapter for ``LocalAuthentication.framework``.

    The import is lazy so Linux, headless commands, and disabled desktop locks
    do not require PyObjC.  The framework owns every credential surface; the
    callback returns only a success bit and an opaque ``NSError`` that MAESTRO
    deliberately neither logs nor exposes.
    """

    def __init__(self, module: Any | None = None) -> None:
        if module is not None:
            self._module = module
            return
        try:
            # A direct lazy import keeps non-macOS imports safe while letting
            # py2app's module graph discover and bundle the framework wrapper.
            import LocalAuthentication  # type: ignore[import-not-found]
        except ImportError as exc:
            raise DesktopAuthUnavailableError(
                "macOS LocalAuthentication support is not installed; reinstall "
                "the MAESTRO macOS application."
            ) from exc
        self._module = LocalAuthentication

    def authenticate(
        self,
        mode: DesktopAuthMode,
        *,
        reason: str,
        timeout_seconds: float,
    ) -> bool:
        if mode is DesktopAuthMode.DISABLED:
            return True
        policy = self._policy(mode)
        try:
            context = self._module.LAContext.alloc().init()
            if mode is DesktopAuthMode.TOUCH_ID:
                # Empty title suppresses LocalAuthentication's "Enter Password"
                # fallback button.  A biometric lockout therefore remains a
                # failed touch-id policy instead of silently widening it.
                context.setLocalizedFallbackTitle_("")
            context.setLocalizedCancelTitle_("Quit MAESTRO")
            available = context.canEvaluatePolicy_error_(policy, None)
        except Exception as exc:  # noqa: BLE001 - ObjC bridge failures vary
            raise DesktopAuthUnavailableError(
                "macOS could not evaluate the configured app-lock policy."
            ) from exc
        availability_error: object | None = None
        if isinstance(available, tuple):
            availability_error = available[1] if len(available) > 1 else None
            available = available[0] if available else False
        if not bool(available):
            raise DesktopAuthUnavailableError(
                _local_auth_failure_message(
                    self._module,
                    availability_error,
                    mode=mode,
                    unavailable=True,
                )
            )

        completed = threading.Event()
        result: list[bool] = []
        result_error: list[object | None] = []
        result_lock = threading.Lock()
        accepting_result = True

        def reply(success: object, error: object) -> None:
            nonlocal accepting_result
            with result_lock:
                if not accepting_result:
                    return
                result.append(bool(success))
                result_error.append(error)
                completed.set()

        try:
            context.evaluatePolicy_localizedReason_reply_(policy, reason, reply)
        except Exception as exc:  # noqa: BLE001 - ObjC bridge failures vary
            raise DesktopAuthUnavailableError(
                "macOS device-owner authentication could not be started."
            ) from exc

        if not completed.wait(timeout_seconds):
            with result_lock:
                accepting_result = False
            try:
                context.invalidate()
            except Exception:  # noqa: BLE001 - cancellation is best effort
                pass
            raise DesktopAuthTimeoutError(
                "MAESTRO stayed locked because macOS authentication timed out."
            )
        with result_lock:
            accepting_result = False
            succeeded = bool(result and result[0])
            error = result_error[0] if result_error else None
        if succeeded:
            return True
        raise DesktopAuthDeniedError(
            _local_auth_failure_message(
                self._module,
                error,
                mode=mode,
                unavailable=False,
            )
        )

    def _policy(self, mode: DesktopAuthMode) -> int:
        if mode is DesktopAuthMode.TOUCH_ID:
            return int(self._module.LAPolicyDeviceOwnerAuthenticationWithBiometrics)
        if mode is DesktopAuthMode.DEVICE_OWNER:
            return int(self._module.LAPolicyDeviceOwnerAuthentication)
        raise DesktopAuthConfigurationError(
            f"unsupported desktop app-lock mode: {mode!s}"
        )


def _local_auth_failure_message(
    module: Any,
    error: object | None,
    *,
    mode: DesktopAuthMode,
    unavailable: bool,
) -> str:
    """Map stable ``LAError`` codes without exposing an opaque NSError."""

    code_value = getattr(error, "code", None)
    if callable(code_value):
        try:
            code_value = code_value()
        except Exception:  # noqa: BLE001 - an NSError proxy can be invalidated
            code_value = None
    try:
        code = int(code_value) if code_value is not None else None
    except (TypeError, ValueError, OverflowError):
        code = None

    def matches(name: str) -> bool:
        value = getattr(module, name, None)
        try:
            return code is not None and value is not None and code == int(value)
        except (TypeError, ValueError, OverflowError):
            return False

    if matches("LAErrorBiometryNotEnrolled"):
        return "Touch ID is not configured on this Mac."
    if matches("LAErrorBiometryNotAvailable") or matches(
        "LAErrorBiometryDisconnected"
    ):
        return "Touch ID is not available on this Mac."
    if matches("LAErrorBiometryLockout"):
        return (
            "Touch ID is locked. Unlock it in macOS, or choose the Mac owner "
            "app-lock mode after you regain access."
        )
    if matches("LAErrorPasscodeNotSet"):
        return "A Mac login password is required for device-owner authentication."
    if matches("LAErrorCompanionNotAvailable"):
        return "Apple Watch authentication is not available on this Mac."
    if matches("LAErrorNotInteractive"):
        return "macOS cannot show authentication in the current session."
    if matches("LAErrorUserCancel") or matches("LAErrorUserFallback"):
        return "MAESTRO stayed locked because authentication was cancelled."
    if matches("LAErrorSystemCancel") or matches("LAErrorAppCancel"):
        return "MAESTRO stayed locked because macOS interrupted authentication."
    if matches("LAErrorAuthenticationFailed"):
        return "MAESTRO stayed locked because authentication was not accepted."

    if unavailable:
        label = (
            "Touch ID"
            if mode is DesktopAuthMode.TOUCH_ID
            else "Device-owner authentication"
        )
        return f"{label} is not available on this Mac."
    return "MAESTRO stayed locked because macOS authentication was not completed."


def _resolve_config_path(
    path: str | Path | None,
    environ: Mapping[str, str],
) -> Path:
    if path is not None:
        return Path(path).expanduser().resolve()
    configured = environ.get(APP_LOCK_CONFIG_ENV)
    if configured is None:
        return DEFAULT_DESKTOP_AUTH_CONFIG_PATH.resolve()
    if not configured.strip():
        raise DesktopAuthConfigurationError(
            f"{APP_LOCK_CONFIG_ENV} must name an absolute file"
        )
    candidate = Path(configured).expanduser()
    if not candidate.is_absolute():
        raise DesktopAuthConfigurationError(
            f"{APP_LOCK_CONFIG_ENV} must name an absolute file"
        )
    return candidate.resolve()


def _read_config_document(path: Path) -> dict[str, object]:
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise DesktopAuthConfigurationError(
            f"desktop app-lock config could not be read: {path}"
        ) from exc
    if len(raw) > _CONFIG_LIMIT_BYTES:
        raise DesktopAuthConfigurationError(
            f"desktop app-lock config is unexpectedly large: {path}"
        )
    try:
        parsed = tomllib.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise DesktopAuthConfigurationError(
            f"desktop app-lock config is invalid TOML: {path}"
        ) from exc
    if not isinstance(parsed, dict):  # pragma: no cover - tomllib always maps roots
        raise DesktopAuthConfigurationError(
            f"desktop app-lock config must be a TOML table: {path}"
        )
    return parsed


def _parse_mode(value: object, *, source: str) -> DesktopAuthMode:
    if isinstance(value, DesktopAuthMode):
        return value
    if not isinstance(value, str):
        raise DesktopAuthConfigurationError(
            f"desktop app-lock mode in {source} must be text"
        )
    try:
        return DesktopAuthMode(value.strip().casefold())
    except ValueError:
        allowed = ", ".join(mode.value for mode in DesktopAuthMode)
        raise DesktopAuthConfigurationError(
            f"desktop app-lock mode in {source} must be one of: {allowed}"
        ) from None


__all__ = [
    "APP_LOCK_CONFIG_ENV",
    "APP_LOCK_MODE_ENV",
    "DEFAULT_DESKTOP_AUTH_CONFIG_PATH",
    "LOCAL_AUTHENTICATION_REQUIREMENT",
    "DesktopAuthBackend",
    "DesktopAuthConfig",
    "DesktopAuthConfigurationError",
    "DesktopAuthDeniedError",
    "DesktopAuthError",
    "DesktopAuthMode",
    "DesktopAuthTimeoutError",
    "DesktopAuthUnavailableError",
    "MacOSLocalAuthenticationBackend",
    "authenticate_desktop_launch",
    "save_desktop_auth_mode",
    "show_desktop_auth_failure",
]
