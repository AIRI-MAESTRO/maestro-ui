from __future__ import annotations

import io
from pathlib import Path
from typing import Any

import pytest

from care.desktop_auth import DesktopAuthDeniedError
from care.harness_desktop import HarnessDesktopError, run_harness_desktop
from care.harness_settings import NativeSettingsApprovalRequest


class Ready:
    launch_url = "http://127.0.0.1:43123/?token=bootstrap-secret"


class Host:
    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs
        self.start_calls = 0
        self.close_calls = 0

    def start(self) -> Ready:
        self.start_calls += 1
        return Ready()

    def close(self) -> None:
        self.close_calls += 1


class Window:
    def __init__(self) -> None:
        self.confirmations: list[tuple[str, str]] = []

    def create_confirmation_dialog(self, title: str, message: str) -> bool:
        self.confirmations.append((title, message))
        return True


class Webview:
    def __init__(self, *, fail: Exception | None = None) -> None:
        self.settings: dict[str, object] = {}
        self.windows: list[tuple[tuple[object, ...], dict[str, object]]] = []
        self.starts: list[dict[str, object]] = []
        self.fail = fail
        self.window: Window | None = None

    def create_window(self, *args: object, **kwargs: object) -> Window:
        self.windows.append((args, kwargs))
        self.window = Window()
        return self.window

    def start(self, **kwargs: object) -> None:
        self.starts.append(kwargs)
        if self.fail is not None:
            raise self.fail


def test_native_harness_uses_private_webview_and_closes_child(tmp_path: Path) -> None:
    created: list[Host] = []

    def factory(**kwargs: Any) -> Host:
        host = Host(**kwargs)
        created.append(host)
        return host

    branded: list[object] = []
    authenticated: list[bool] = []
    keychain_guarded: list[bool] = []
    approvals: list[object] = []
    settings_api = object()

    def settings_factory(**kwargs: object) -> object:
        approvals.append(kwargs["native_approval"])
        assert isinstance(kwargs["workspace"], Path)
        return settings_api

    webview = Webview()
    output = io.StringIO()
    run_harness_desktop(
        width=1_300,
        height=840,
        source_root=tmp_path,
        dsh_home=tmp_path / "dsh-home",
        stdout=output,
        _webview_module=webview,
        _host_factory=factory,
        _brand_binder=branded.append,
        _authenticator=lambda: authenticated.append(True),
        _keychain_ui_guard=lambda: keychain_guarded.append(True),
        _settings_api_factory=settings_factory,
    )

    host = created[0]
    assert host.start_calls == 1
    assert host.close_calls == 1
    assert host.kwargs["source_root"] == tmp_path
    assert webview.windows[0][0] == ("MAESTRO",)
    assert webview.windows[0][1]["url"] == Ready.launch_url
    assert webview.windows[0][1]["js_api"] is settings_api
    assert webview.starts[0]["private_mode"] is True
    assert "func" not in webview.starts[0]
    assert "args" not in webview.starts[0]
    assert len(branded) == 1
    assert isinstance(branded[0], Window)
    assert authenticated == [True]
    assert keychain_guarded == [True]
    assert len(approvals) == 1
    assert callable(approvals[0])
    request = NativeSettingsApprovalRequest(
        title="Save?",
        message="Native review",
        changes=("mage.model: old → new",),
    )
    assert approvals[0](request) is True  # type: ignore[operator]
    assert webview.window is not None
    assert webview.window.confirmations == [
        ("Save?", "Native review\n\n• mage.model: old → new")
    ]
    assert "token" not in output.getvalue().casefold()
    assert "secret" not in output.getvalue().casefold()


def test_native_harness_does_not_install_legacy_dom_branding_by_default(
    tmp_path: Path,
) -> None:
    host = Host()
    webview = Webview()

    run_harness_desktop(
        dsh_home=tmp_path / "home",
        _webview_module=webview,
        _host_factory=lambda **_kwargs: host,
        _authenticator=lambda: None,
        _keychain_ui_guard=lambda: None,
        _settings_api_factory=lambda **_kwargs: object(),
    )

    assert host.close_calls == 1
    assert len(webview.windows) == 1


def test_window_failure_still_closes_harness(tmp_path: Path) -> None:
    host = Host()
    webview = Webview(fail=RuntimeError("Cocoa unavailable"))

    with pytest.raises(HarnessDesktopError, match="Cocoa unavailable"):
        run_harness_desktop(
            dsh_home=tmp_path / "home",
            _webview_module=webview,
            _host_factory=lambda **_kwargs: host,
            _brand_binder=lambda _window: None,
            _authenticator=lambda: None,
            _keychain_ui_guard=lambda: None,
            _settings_api_factory=lambda **_kwargs: object(),
        )

    assert host.close_calls == 1


@pytest.mark.parametrize(
    "kwargs",
    [
        {"width": 919},
        {"height": 619},
        {"executable": "/bin/dsh", "source_root": "/checkout"},
    ],
)
def test_options_fail_before_host_construction(kwargs: dict[str, object]) -> None:
    called = False
    authenticated = False

    def factory(**_kwargs: Any) -> Host:
        nonlocal called
        called = True
        return Host()

    def authenticate() -> None:
        nonlocal authenticated
        authenticated = True

    with pytest.raises(ValueError):
        run_harness_desktop(
            _webview_module=Webview(),
            _host_factory=factory,
            _authenticator=authenticate,
            _keychain_ui_guard=lambda: None,
            **kwargs,
        )
    assert called is False
    assert authenticated is False


def test_authentication_failure_prevents_host_and_window(tmp_path: Path) -> None:
    host_called = False
    notifications: list[object] = []
    webview = Webview()

    def factory(**_kwargs: Any) -> Host:
        nonlocal host_called
        host_called = True
        return Host()

    def deny() -> None:
        raise DesktopAuthDeniedError("MAESTRO stayed locked")

    with pytest.raises(HarnessDesktopError, match="stayed locked"):
        run_harness_desktop(
            dsh_home=tmp_path / "home",
            _webview_module=webview,
            _host_factory=factory,
            _authenticator=deny,
            _auth_failure_notifier=notifications.append,
            _keychain_ui_guard=lambda: None,
        )

    assert host_called is False
    assert webview.windows == []
    assert len(notifications) == 1
