"""Native MAESTRO shell around the resident DeepSeek Harness agent.

The Web application binds only to an ephemeral loopback port and is rendered
inside pywebview; no browser tab is opened.  DeepSeek Harness owns chat,
sessions, tools, approvals, queued follow-ups, steer/cancel, jobs, and model
settings.  CARE is injected as a typed MCP capability provider by the process
supervisor.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Callable, TextIO

from care.config import CareConfig
from care.desktop import (
    DEFAULT_HEIGHT,
    DEFAULT_WIDTH,
    MIN_HEIGHT,
    MIN_WIDTH,
    _configure_webview,
    _load_webview,
    prepare_desktop_working_directory,
)
from care.desktop_auth import (
    DesktopAuthError,
    authenticate_desktop_launch,
    show_desktop_auth_failure,
)
from care.harness_settings import (
    PyWebViewNativeSettingsApproval,
    create_harness_settings_api,
)
from care.runtime.deepseek_host import (
    DEFAULT_DSH_HOME,
    DeepSeekHarnessError,
    DeepSeekHarnessHost,
)
from care.runtime.credential_broker import HarnessCredentialResolver
from care.runtime.keystore import disable_macos_keychain_interaction


WINDOW_TITLE = "MAESTRO"
_BACKGROUND_COLOR = "#0f1115"


class HarnessDesktopError(RuntimeError):
    """The native resident-agent application could not be launched."""


def _write_status(output: TextIO | None, text: str) -> None:
    if output is None:
        return
    try:
        output.write(text + "\n")
        output.flush()
    except (AttributeError, OSError, ValueError):
        # Finder-launched app bundles commonly have no usable stdout.
        pass


def run_harness_desktop(
    *,
    width: int = DEFAULT_WIDTH,
    height: int = DEFAULT_HEIGHT,
    debug: bool = False,
    executable: str | Path | None = None,
    source_root: str | Path | None = None,
    dsh_home: str | Path = DEFAULT_DSH_HOME,
    require_pinned_version: bool = True,
    stdout: TextIO | None = None,
    _webview_module: Any | None = None,
    _host_factory: Callable[..., Any] = DeepSeekHarnessHost,
    _brand_binder: Callable[[Any], Any] | None = None,
    _authenticator: Callable[[], None] = authenticate_desktop_launch,
    _auth_failure_notifier: Callable[[object], Any] = show_desktop_auth_failure,
    _keychain_ui_guard: Callable[[], None] = disable_macos_keychain_interaction,
    _settings_api_factory: Callable[..., Any] = create_harness_settings_api,
    _settings_approval_factory: Callable[[], Any] = PyWebViewNativeSettingsApproval,
    _config_loader: Callable[..., CareConfig] = CareConfig.load,
    _credential_resolver_factory: Callable[
        [CareConfig], HarnessCredentialResolver
    ] = HarnessCredentialResolver,
) -> None:
    """Run the resident Harness in one native window until it closes."""

    if width < MIN_WIDTH or height < MIN_HEIGHT:
        raise ValueError(
            f"window must be at least {MIN_WIDTH}x{MIN_HEIGHT} logical pixels",
        )
    if executable is not None and source_root is not None:
        raise ValueError("choose either a DeepSeek Harness executable or source checkout")

    # Rebuilt/ad-hoc signed development bundles do not have a stable Keychain
    # ACL identity.  Never let an old item turn application startup into a
    # system-password dialog: protected credentials stay unavailable until
    # they can be saved for the current app identity.
    try:
        _keychain_ui_guard()
    except Exception as exc:  # noqa: BLE001 - Security.framework boundary
        raise HarnessDesktopError(
            "macOS Keychain password prompts could not be disabled safely"
        ) from exc

    # Authenticate before constructing the host or opening a WebView.  The
    # default mode is disabled; an enabled mode is owned entirely by macOS
    # LocalAuthentication and never exposes a password to MAESTRO.
    try:
        _authenticator()
    except DesktopAuthError as exc:
        try:
            _auth_failure_notifier(exc)
        except Exception:  # noqa: BLE001 - notification is best effort
            pass
        raise HarnessDesktopError(str(exc)) from exc
    except Exception as exc:  # noqa: BLE001 - injected/native adapters vary
        try:
            _auth_failure_notifier(
                "macOS authentication could not be completed safely."
            )
        except Exception:  # noqa: BLE001 - notification is best effort
            pass
        raise HarnessDesktopError(
            "native app authentication could not be completed"
        ) from exc

    webview = _webview_module if _webview_module is not None else _load_webview()
    workspace = prepare_desktop_working_directory()
    credential_resolver: HarnessCredentialResolver | None = None
    try:
        # Preserve the validated layering and raw ``keystore://`` references,
        # but do not ask Keychain for anything yet. The main-process broker
        # resolves one allow-listed slot on demand and caches it across MCP
        # reconnects.
        raw_config = _config_loader(
            cwd=workspace,
            resolve_secrets=False,
        )
        credential_resolver = _credential_resolver_factory(raw_config)
        settings_approval = _settings_approval_factory()
        settings_api = _settings_api_factory(
            workspace=workspace,
            native_approval=settings_approval,
        )
        host = _host_factory(
            executable=executable,
            source_root=source_root,
            workspace=workspace,
            dsh_home=dsh_home,
            require_pinned_version=require_pinned_version,
            credential_provider=credential_resolver,
        )
    except Exception as exc:  # noqa: BLE001 - injected factories vary in tests
        if credential_resolver is not None:
            credential_resolver.close()
        if isinstance(exc, (DeepSeekHarnessError, ValueError, OSError)):
            raise HarnessDesktopError(str(exc)) from exc
        raise HarnessDesktopError(
            "native application configuration could not be initialized"
        ) from exc

    try:
        ready = host.start()
        _configure_webview(webview)
        window = webview.create_window(
            WINDOW_TITLE,
            url=ready.launch_url,
            width=width,
            height=height,
            min_size=(MIN_WIDTH, MIN_HEIGHT),
            resizable=True,
            background_color=_BACKGROUND_COLOR,
            text_select=True,
            zoomable=True,
            js_api=settings_api,
        )
        settings_approval.bind_window(window)
        # Product identity is mounted by the first-party DSH client plugin.
        # Keep an optional hook only for isolated tests/embedders; the legacy
        # DOM injector hid real React slot occupants and must not run by default.
        if _brand_binder is not None:
            _brand_binder(window)
        _write_status(
            stdout if stdout is not None else sys.stdout,
            "MAESTRO resident agent started. Close the window to stop it.",
        )
        webview.start(
            debug=debug,
            private_mode=True,
        )
    except DeepSeekHarnessError as exc:
        raise HarnessDesktopError(str(exc)) from exc
    except HarnessDesktopError:
        raise
    except Exception as exc:  # noqa: BLE001 - GUI backends vary by platform
        raise HarnessDesktopError(
            f"native resident-agent window failed to start: {exc}",
        ) from exc
    finally:
        host.close()


__all__ = [
    "HarnessDesktopError",
    "WINDOW_TITLE",
    "run_harness_desktop",
]
