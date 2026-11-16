from __future__ import annotations

import stat
import threading
import tomllib
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from care.desktop_auth import (
    APP_LOCK_CONFIG_ENV,
    APP_LOCK_MODE_ENV,
    LOCAL_AUTHENTICATION_REQUIREMENT,
    DesktopAuthConfig,
    DesktopAuthConfigurationError,
    DesktopAuthDeniedError,
    DesktopAuthError,
    DesktopAuthMode,
    DesktopAuthTimeoutError,
    DesktopAuthUnavailableError,
    MacOSLocalAuthenticationBackend,
    authenticate_desktop_launch,
    save_desktop_auth_mode,
    show_desktop_auth_failure,
)


class _FakeContext:
    def __init__(
        self,
        *,
        available: object = (True, None),
        result: bool | None = True,
        evaluate_error: Exception | None = None,
        reply_error: object | None = None,
    ) -> None:
        self.available = available
        self.result = result
        self.evaluate_error = evaluate_error
        self.reply_error = object() if reply_error is None else reply_error
        self.checked_policy: int | None = None
        self.evaluated_policy: int | None = None
        self.reason: str | None = None
        self.fallback_titles: list[str] = []
        self.cancel_titles: list[str] = []
        self.invalidated = False

    def setLocalizedFallbackTitle_(self, title: str) -> None:
        self.fallback_titles.append(title)

    def setLocalizedCancelTitle_(self, title: str) -> None:
        self.cancel_titles.append(title)

    def canEvaluatePolicy_error_(
        self,
        policy: int,
        _error: object,
    ) -> object:
        self.checked_policy = policy
        return self.available

    def evaluatePolicy_localizedReason_reply_(
        self,
        policy: int,
        reason: str,
        reply: Any,
    ) -> None:
        self.evaluated_policy = policy
        self.reason = reason
        if self.evaluate_error is not None:
            raise self.evaluate_error
        if self.result is not None:
            reply(self.result, self.reply_error)

    def invalidate(self) -> None:
        self.invalidated = True


def _fake_local_authentication(context: _FakeContext) -> object:
    class LAContext:
        @classmethod
        def alloc(cls) -> "LAContext":
            return cls()

        def init(self) -> _FakeContext:
            return context

    return SimpleNamespace(
        LAContext=LAContext,
        LAPolicyDeviceOwnerAuthenticationWithBiometrics=101,
        LAPolicyDeviceOwnerAuthentication=202,
        LAErrorAuthenticationFailed=-1,
        LAErrorUserCancel=-2,
        LAErrorUserFallback=-3,
        LAErrorSystemCancel=-4,
        LAErrorPasscodeNotSet=-5,
        LAErrorBiometryNotAvailable=-6,
        LAErrorBiometryNotEnrolled=-7,
        LAErrorBiometryLockout=-8,
        LAErrorAppCancel=-9,
        LAErrorCompanionNotAvailable=-11,
        LAErrorBiometryDisconnected=-13,
        LAErrorNotInteractive=-1004,
    )


def _config(mode: DesktopAuthMode) -> DesktopAuthConfig:
    return DesktopAuthConfig(mode=mode, path=Path("/unused"))


def test_absent_config_defaults_to_disabled(tmp_path: Path) -> None:
    path = tmp_path / "desktop-auth.toml"

    config = DesktopAuthConfig.load(path=path, environ={})

    assert config == DesktopAuthConfig(
        mode=DesktopAuthMode.DISABLED,
        path=path.resolve(),
        overridden_by_environment=False,
    )


@pytest.mark.parametrize("mode", list(DesktopAuthMode))
def test_config_round_trip_for_each_mode(
    tmp_path: Path,
    mode: DesktopAuthMode,
) -> None:
    path = tmp_path / "nested" / "desktop-auth.toml"

    saved = save_desktop_auth_mode(mode, path=path, environ={})

    assert saved == path.resolve()
    assert DesktopAuthConfig.load(path=path, environ={}).mode is mode
    assert path.read_text(encoding="utf-8") == f'mode = "{mode.value}"\n'
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_environment_can_override_mode_and_config_path(tmp_path: Path) -> None:
    file_path = tmp_path / "desktop-auth.toml"
    file_path.write_text('mode = "disabled"\n', encoding="utf-8")

    config = DesktopAuthConfig.load(
        environ={
            APP_LOCK_CONFIG_ENV: str(file_path),
            APP_LOCK_MODE_ENV: " TOUCH-ID ",
        }
    )

    assert config.mode is DesktopAuthMode.TOUCH_ID
    assert config.path == file_path.resolve()
    assert config.overridden_by_environment is True


@pytest.mark.parametrize(
    ("contents", "environment"),
    [
        ('mode = "password"\n', {}),
        ("mode = 7\n", {}),
        ('mode = "disabled"\nsecret = "no"\n', {}),
        ("mode = [\n", {}),
        ('mode = "disabled"\n', {APP_LOCK_MODE_ENV: "password"}),
    ],
)
def test_invalid_configuration_fails_closed(
    tmp_path: Path,
    contents: str,
    environment: dict[str, str],
) -> None:
    path = tmp_path / "desktop-auth.toml"
    path.write_text(contents, encoding="utf-8")

    with pytest.raises(DesktopAuthConfigurationError):
        DesktopAuthConfig.load(path=path, environ=environment)


def test_relative_environment_config_path_is_rejected() -> None:
    with pytest.raises(
        DesktopAuthConfigurationError,
        match="must name an absolute file",
    ):
        DesktopAuthConfig.load(
            environ={APP_LOCK_CONFIG_ENV: "relative/desktop-auth.toml"}
        )


def test_saved_config_contains_no_password_material(tmp_path: Path) -> None:
    path = save_desktop_auth_mode(
        DesktopAuthMode.DEVICE_OWNER,
        path=tmp_path / "desktop-auth.toml",
        environ={},
    )

    contents = path.read_text(encoding="utf-8").casefold()
    assert "password" not in contents
    assert "hash" not in contents
    assert "verifier" not in contents


def test_disabled_mode_does_not_construct_macos_backend() -> None:
    constructed = False

    def factory() -> object:
        nonlocal constructed
        constructed = True
        raise AssertionError("backend must remain lazy")

    authenticate_desktop_launch(
        config=_config(DesktopAuthMode.DISABLED),
        platform="linux",
        backend_factory=factory,  # type: ignore[arg-type]
    )

    assert constructed is False


def test_enabled_mode_fails_closed_off_macos_without_backend() -> None:
    with pytest.raises(DesktopAuthUnavailableError, match="only in the native macOS"):
        authenticate_desktop_launch(
            config=_config(DesktopAuthMode.TOUCH_ID),
            platform="linux",
            backend_factory=lambda: pytest.fail("backend should not be constructed"),
        )


def test_touch_id_uses_biometrics_only_and_suppresses_password_fallback() -> None:
    context = _FakeContext(result=True)
    backend = MacOSLocalAuthenticationBackend(
        module=_fake_local_authentication(context)
    )

    authenticated = backend.authenticate(
        DesktopAuthMode.TOUCH_ID,
        reason="Unlock for test",
        timeout_seconds=1,
    )

    assert authenticated is True
    assert context.checked_policy == 101
    assert context.evaluated_policy == 101
    assert context.reason == "Unlock for test"
    assert context.fallback_titles == [""]
    assert context.cancel_titles == ["Quit MAESTRO"]


def test_device_owner_leaves_fallback_ui_to_macos() -> None:
    context = _FakeContext(result=True)
    backend = MacOSLocalAuthenticationBackend(
        module=_fake_local_authentication(context)
    )

    authenticated = backend.authenticate(
        DesktopAuthMode.DEVICE_OWNER,
        reason="Unlock for test",
        timeout_seconds=1,
    )

    assert authenticated is True
    assert context.checked_policy == 202
    assert context.evaluated_policy == 202
    assert context.fallback_titles == []


def test_unavailable_policy_never_starts_authentication() -> None:
    context = _FakeContext(available=(False, object()))
    backend = MacOSLocalAuthenticationBackend(
        module=_fake_local_authentication(context)
    )

    with pytest.raises(DesktopAuthUnavailableError, match="Touch ID is not available"):
        backend.authenticate(
            DesktopAuthMode.TOUCH_ID,
            reason="Unlock for test",
            timeout_seconds=1,
        )

    assert context.evaluated_policy is None


def test_unavailable_policy_maps_safe_enrollment_reason() -> None:
    framework_error = SimpleNamespace(code=lambda: -7)
    context = _FakeContext(available=(False, framework_error))
    backend = MacOSLocalAuthenticationBackend(
        module=_fake_local_authentication(context)
    )

    with pytest.raises(DesktopAuthUnavailableError, match="not configured"):
        backend.authenticate(
            DesktopAuthMode.TOUCH_ID,
            reason="Unlock for test",
            timeout_seconds=1,
        )


def test_failed_system_authentication_keeps_app_locked() -> None:
    context = _FakeContext(result=False)

    with pytest.raises(DesktopAuthDeniedError, match="stayed locked"):
        authenticate_desktop_launch(
            config=_config(DesktopAuthMode.DEVICE_OWNER),
            platform="darwin",
            backend_factory=lambda: MacOSLocalAuthenticationBackend(
                module=_fake_local_authentication(context)
            ),
        )

    assert context.checked_policy == 101
    assert context.evaluated_policy == 101
    assert context.fallback_titles == [""]


@pytest.mark.parametrize(
    ("code", "message"),
    [
        (-2, "cancelled"),
        (-8, "Touch ID is locked"),
        (-1, "not accepted"),
        (-1004, "current session"),
    ],
)
def test_framework_error_codes_map_to_safe_user_messages(
    code: int,
    message: str,
) -> None:
    framework_error = SimpleNamespace(code=lambda: code)
    context = _FakeContext(result=False, reply_error=framework_error)
    backend = MacOSLocalAuthenticationBackend(
        module=_fake_local_authentication(context)
    )

    with pytest.raises(DesktopAuthError, match=message) as caught:
        backend.authenticate(
            DesktopAuthMode.TOUCH_ID,
            reason="Unlock for test",
            timeout_seconds=1,
        )

    assert str(code) not in str(caught.value)


def test_asynchronous_success_callback_unlocks() -> None:
    context = _FakeContext(result=None)

    def callback_later(_policy: int, _reason: str, reply: Any) -> None:
        timer = threading.Timer(0.01, reply, args=(True, None))
        timer.start()

    context.evaluatePolicy_localizedReason_reply_ = callback_later  # type: ignore[method-assign]
    backend = MacOSLocalAuthenticationBackend(
        module=_fake_local_authentication(context)
    )

    assert backend.authenticate(
        DesktopAuthMode.DEVICE_OWNER,
        reason="Unlock for test",
        timeout_seconds=1,
    ) is True


def test_authentication_timeout_invalidates_context() -> None:
    context = _FakeContext(result=None)
    backend = MacOSLocalAuthenticationBackend(
        module=_fake_local_authentication(context)
    )

    with pytest.raises(DesktopAuthTimeoutError, match="timed out"):
        backend.authenticate(
            DesktopAuthMode.DEVICE_OWNER,
            reason="Unlock for test",
            timeout_seconds=0.01,
        )

    assert context.invalidated is True


def test_late_callback_after_timeout_is_ignored() -> None:
    callback: list[Any] = []
    context = _FakeContext(result=None)

    def capture_callback(
        policy: int,
        reason: str,
        reply: Any,
    ) -> None:
        context.evaluated_policy = policy
        context.reason = reason
        callback.append(reply)

    context.evaluatePolicy_localizedReason_reply_ = capture_callback  # type: ignore[method-assign]
    backend = MacOSLocalAuthenticationBackend(
        module=_fake_local_authentication(context)
    )

    with pytest.raises(DesktopAuthTimeoutError):
        backend.authenticate(
            DesktopAuthMode.DEVICE_OWNER,
            reason="Unlock for test",
            timeout_seconds=0.01,
        )

    thread = threading.Thread(target=callback[0], args=(True, object()))
    thread.start()
    thread.join(timeout=1)
    assert thread.is_alive() is False


def test_framework_error_is_sanitized() -> None:
    secret = "do-not-leak-framework-detail"
    context = _FakeContext(evaluate_error=RuntimeError(secret))
    backend = MacOSLocalAuthenticationBackend(
        module=_fake_local_authentication(context)
    )

    with pytest.raises(DesktopAuthUnavailableError) as caught:
        backend.authenticate(
            DesktopAuthMode.DEVICE_OWNER,
            reason="Unlock for test",
            timeout_seconds=1,
        )

    assert secret not in str(caught.value)


def test_dependency_is_declared_for_every_desktop_distribution_extra() -> None:
    project = tomllib.loads(
        (Path(__file__).parents[1] / "pyproject.toml").read_text(encoding="utf-8")
    )["project"]
    expected = f"{LOCAL_AUTHENTICATION_REQUIREMENT}; sys_platform == 'darwin'"

    for extra in ("desktop", "macos-app", "full"):
        assert expected in project["optional-dependencies"][extra]


def test_native_failure_alert_is_safe_and_redacted() -> None:
    class Alert:
        def __init__(self) -> None:
            self.title = ""
            self.detail = ""
            self.button = ""
            self.style: object | None = None
            self.ran = False

        @classmethod
        def alloc(cls) -> "Alert":
            return cls()

        def init(self) -> "Alert":
            appkit.alert = self
            return self

        def setAlertStyle_(self, style: object) -> None:
            self.style = style

        def setMessageText_(self, title: str) -> None:
            self.title = title

        def setInformativeText_(self, detail: str) -> None:
            self.detail = detail

        def addButtonWithTitle_(self, button: str) -> None:
            self.button = button

        def runModal(self) -> None:
            self.ran = True

    appkit = SimpleNamespace(
        alert=None,
        NSAlert=Alert,
        NSAlertStyleCritical=99,
        NSThread=SimpleNamespace(isMainThread=lambda: True),
        NSApplication=SimpleNamespace(sharedApplication=lambda: object()),
    )

    shown = show_desktop_auth_failure(
        "api_key=sk-supersecret123",
        platform="darwin",
        appkit=appkit,
    )

    assert shown is True
    assert appkit.alert.title == "MAESTRO remains locked"
    assert "supersecret" not in appkit.alert.detail
    assert "api_key=[redacted]" in appkit.alert.detail
    assert "desktop-auth.toml" in appkit.alert.detail
    assert appkit.alert.button == "Quit"
    assert appkit.alert.style == 99
    assert appkit.alert.ran is True


def test_native_failure_alert_is_not_attempted_off_macos() -> None:
    assert show_desktop_auth_failure("denied", platform="linux") is False
