"""Native desktop shell for the Maestro workbench.

The desktop client deliberately keeps the existing HTTP/SSE boundary between
the UI and the runtime, but renders it inside an operating-system window via
``pywebview``.  The server is bound to an ephemeral loopback port and is shut
down with the window; no browser tab is opened and no Python API is exposed to
page JavaScript.

``pywebview`` is imported lazily so the TUI and headless CLI remain usable
without the optional desktop dependency.
"""

from __future__ import annotations

import importlib
import os
import sys
import threading
from pathlib import Path
from typing import Any, Callable, TextIO


DEFAULT_WIDTH = 1_280
DEFAULT_HEIGHT = 820
MIN_WIDTH = 920
MIN_HEIGHT = 620
WINDOW_TITLE = "Maestro"
_BACKGROUND_COLOR = "#0f1115"
DESKTOP_WORKSPACE_ENV = "CARE_DESKTOP_WORKSPACE"


class DesktopError(RuntimeError):
    """Base class for actionable desktop-launch failures."""


class DesktopDependencyError(DesktopError):
    """Raised when the optional native-window dependency is unavailable."""


class DesktopLaunchError(DesktopError):
    """Raised when the native GUI backend cannot create or run a window."""


class NativeBridge:
    """Minimal JS bridge for actions that require a native window handle.

    Runtime/provider functionality deliberately stays behind the authenticated
    HTTP API.  Only closing the current window needs a native primitive so the
    desktop `/quit` command can match the TUI without exposing a general Python
    object surface to page JavaScript.
    """

    def __init__(self) -> None:
        self._window: Any | None = None
        self._lock = threading.Lock()

    def _bind(self, window: Any) -> None:
        with self._lock:
            self._window = window

    def close_app(self) -> bool:
        with self._lock:
            window = self._window
        if window is None:
            return False
        window.destroy()
        return True


def _load_webview() -> Any:
    try:
        return importlib.import_module("webview")
    except ImportError as exc:
        raise DesktopDependencyError(
            "the desktop extra is not installed; run "
            "`pip install \"maestro-care[desktop]\"`",
        ) from exc


def _bundled_assets_dir() -> Path | None:
    """Return py2app's copied web-assets directory when running in a bundle."""

    resource_path = os.environ.get("RESOURCEPATH")
    if not resource_path:
        return None
    candidate = Path(resource_path).expanduser() / "web_assets"
    return candidate if candidate.is_dir() else None


def prepare_desktop_working_directory(
    *,
    resource_path: str | Path | None = None,
    fallback_workspace: str | Path | None = None,
) -> Path:
    """Keep relative CARE paths outside a packaged ``.app`` bundle.

    py2app historically changes GUI applications into ``Contents/Resources``.
    That makes relative exports/config writes mutate the signed bundle.  The
    bundle disables that behaviour, and this second guard handles Finder's
    usual root working directory while preserving an intentional terminal cwd.

    ``CARE_DESKTOP_WORKSPACE`` selects an explicit directory.  Otherwise a
    Finder-style unsafe cwd falls back to ``~/Documents/Maestro``.  Source/CLI
    launches without ``RESOURCEPATH`` are left untouched.
    """

    raw_resources = resource_path
    if raw_resources is None:
        raw_resources = os.environ.get("RESOURCEPATH")
    if not raw_resources:
        return Path.cwd().resolve()

    resources = Path(raw_resources).expanduser().resolve()
    # Standard layout is Maestro.app/Contents/Resources. Treat the complete
    # app as sealed even in tests/custom bundle names.
    bundle_root = (
        resources.parent if resources.name == "_internal" else resources.parent.parent
    ).resolve()
    current = Path.cwd().resolve()
    configured = os.environ.get(DESKTOP_WORKSPACE_ENV, "").strip()

    if configured:
        target = Path(configured).expanduser()
        if not target.is_absolute():
            raise DesktopLaunchError(
                f"{DESKTOP_WORKSPACE_ENV} must be an absolute directory",
            )
        target = target.resolve()
    else:
        inside_bundle = current.is_relative_to(bundle_root)
        if current != Path(current.anchor) and not inside_bundle:
            return current
        target = (
            Path(fallback_workspace).expanduser()
            if fallback_workspace is not None
            else Path.home() / "Documents" / "Maestro"
        ).resolve()

    if target == Path(target.anchor) or target.is_relative_to(bundle_root):
        raise DesktopLaunchError(
            "desktop workspace must be a normal directory outside Maestro.app",
        )
    try:
        target.mkdir(parents=True, exist_ok=True)
        if not target.is_dir():
            raise NotADirectoryError(str(target))
        os.chdir(target)
    except OSError as exc:
        raise DesktopLaunchError(
            f"desktop workspace is unavailable: {target}",
        ) from exc
    return target


def _configure_webview(webview: Any) -> None:
    """Apply the narrowest useful native-WebView capabilities."""

    settings = getattr(webview, "settings", None)
    if settings is None:
        return
    # The UI has no download/file-navigation workflow.  Keep those ambient
    # capabilities disabled even if a platform backend changes its defaults.
    settings["ALLOW_DOWNLOADS"] = False
    settings["ALLOW_FILE_URLS"] = False
    settings["IGNORE_SSL_ERRORS"] = False
    settings["REMOTE_DEBUGGING_PORT"] = None
    # A target=_blank documentation link must never replace the application
    # surface.  pywebview delegates such links to the system browser.
    settings["OPEN_EXTERNAL_LINKS_IN_BROWSER"] = True


def _launch_url(server: Any) -> str:
    """Resolve the authenticated bootstrap URL without leaking it to logs."""

    launch_url = getattr(server, "launch_url", None)
    if isinstance(launch_url, str) and launch_url:
        return launch_url
    raise DesktopLaunchError("local server did not provide an authenticated launch URL")


def _stop_server(server: Any, thread: threading.Thread) -> None:
    """Stop HTTP, active runs, and the worker thread in a fixed order."""

    try:
        server.shutdown()
    finally:
        thread.join(timeout=5.0)
        manager = getattr(server, "run_manager", None)
        try:
            if manager is not None:
                manager.shutdown(wait=True)
        finally:
            server.server_close()


def run_desktop(
    *,
    port: int = 0,
    width: int = DEFAULT_WIDTH,
    height: int = DEFAULT_HEIGHT,
    debug: bool = False,
    stdout: TextIO | None = None,
    _webview_module: Any | None = None,
    _server_factory: Callable[..., Any] | None = None,
) -> None:
    """Run Maestro in a native window until that window closes.

    The GUI loop stays on the calling (main) thread as required by Cocoa.
    Only the loopback HTTP server runs in a daemon thread.
    """

    if not isinstance(port, int) or not 0 <= port <= 65_535:
        raise ValueError("port must be between 0 and 65535")
    if width < MIN_WIDTH or height < MIN_HEIGHT:
        raise ValueError(
            f"window must be at least {MIN_WIDTH}x{MIN_HEIGHT} logical pixels",
        )

    webview = _webview_module if _webview_module is not None else _load_webview()
    if _server_factory is None:
        from care.web import create_server

        _server_factory = create_server

    server = _server_factory(
        host="127.0.0.1",
        port=port,
        assets_dir=_bundled_assets_dir(),
    )
    server_thread = threading.Thread(
        target=server.serve_forever,
        kwargs={"poll_interval": 0.25},
        name="maestro-desktop-http",
        daemon=True,
    )
    server_started = False
    try:
        server_thread.start()
        server_started = True
        # A Finder-launched windowed .app can have no usable stdout.  Status
        # output is therefore best-effort and never part of app startup.
        output = stdout if stdout is not None else sys.stdout
        if output is not None:
            try:
                output.write("Maestro desktop started. Close the window to stop it.\n")
                output.flush()
            except (AttributeError, OSError, ValueError):
                pass
        _configure_webview(webview)
        native_bridge = NativeBridge()
        window = webview.create_window(
            WINDOW_TITLE,
            url=_launch_url(server),
            js_api=native_bridge,
            width=width,
            height=height,
            min_size=(MIN_WIDTH, MIN_HEIGHT),
            resizable=True,
            background_color=_BACKGROUND_COLOR,
            text_select=True,
            zoomable=True,
        )
        native_bridge._bind(window)
        # Private mode prevents the bootstrap HttpOnly cookie from surviving
        # the process.  A fresh server and token are created on every launch.
        webview.start(debug=debug, private_mode=True)
    except DesktopError:
        raise
    except Exception as exc:  # noqa: BLE001 - GUI backends vary by platform
        raise DesktopLaunchError(f"native window failed to start: {exc}") from exc
    finally:
        if server_started:
            _stop_server(server, server_thread)
        else:
            manager = getattr(server, "run_manager", None)
            try:
                if manager is not None:
                    manager.shutdown(wait=True)
            finally:
                server.server_close()


def main(argv: list[str] | None = None) -> int:
    """Console entry point that routes to ``maestro app``."""

    from care.cli import main as cli_main

    args = sys.argv[1:] if argv is None else list(argv)
    return cli_main(["app", *args])


if __name__ == "__main__":  # pragma: no cover - console bootstrap
    raise SystemExit(main())


__all__ = [
    "DEFAULT_HEIGHT",
    "DEFAULT_WIDTH",
    "DESKTOP_WORKSPACE_ENV",
    "DesktopDependencyError",
    "DesktopError",
    "DesktopLaunchError",
    "MIN_HEIGHT",
    "MIN_WIDTH",
    "NativeBridge",
    "main",
    "prepare_desktop_working_directory",
    "run_desktop",
]
