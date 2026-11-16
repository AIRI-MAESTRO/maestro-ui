from __future__ import annotations

import io
import threading
from pathlib import Path

import pytest

from care import desktop


class _Manager:
    def __init__(self) -> None:
        self.shutdown_calls: list[bool] = []

    def shutdown(self, *, wait: bool) -> None:
        self.shutdown_calls.append(wait)


class _Server:
    server_address = ("127.0.0.1", 43123)
    launch_url = "http://127.0.0.1:43123/?token=one-time-secret"

    def __init__(self) -> None:
        self.run_manager = _Manager()
        self.serving = threading.Event()
        self.release = threading.Event()
        self.shutdown_calls = 0
        self.close_calls = 0

    def serve_forever(self, *, poll_interval: float) -> None:
        assert poll_interval == 0.25
        self.serving.set()
        self.release.wait(timeout=2.0)

    def shutdown(self) -> None:
        self.shutdown_calls += 1
        self.release.set()

    def server_close(self) -> None:
        self.close_calls += 1


class _Webview:
    def __init__(self, *, start_error: Exception | None = None) -> None:
        self.settings: dict[str, object] = {}
        self.windows: list[tuple[tuple[object, ...], dict[str, object]]] = []
        self.start_calls: list[dict[str, object]] = []
        self.start_thread_ids: list[int] = []
        self.start_error = start_error

    def create_window(self, *args, **kwargs):
        self.windows.append((args, kwargs))
        return _Window()

    def start(self, **kwargs) -> None:
        self.start_calls.append(kwargs)
        self.start_thread_ids.append(threading.get_ident())
        if self.start_error is not None:
            raise self.start_error


class _Window:
    def __init__(self) -> None:
        self.destroy_calls = 0

    def destroy(self) -> None:
        self.destroy_calls += 1


def test_run_desktop_uses_authenticated_native_window_and_cleans_up() -> None:
    server = _Server()
    webview = _Webview()
    calls: list[dict[str, object]] = []

    def factory(**kwargs):
        calls.append(kwargs)
        return server

    output = io.StringIO()
    caller_thread = threading.get_ident()
    desktop.run_desktop(
        port=0,
        width=1_200,
        height=760,
        stdout=output,
        _webview_module=webview,
        _server_factory=factory,
    )

    assert calls == [{"host": "127.0.0.1", "port": 0, "assets_dir": None}]
    assert server.serving.is_set()
    assert webview.windows[0][0] == ("Maestro",)
    assert webview.windows[0][1]["url"] == server.launch_url
    assert isinstance(webview.windows[0][1]["js_api"], desktop.NativeBridge)
    assert webview.windows[0][1]["min_size"] == (920, 620)
    assert webview.start_calls == [{"debug": False, "private_mode": True}]
    assert webview.start_thread_ids == [caller_thread]
    assert webview.settings["ALLOW_DOWNLOADS"] is False
    assert webview.settings["ALLOW_FILE_URLS"] is False
    assert server.shutdown_calls == 1
    assert server.run_manager.shutdown_calls == [True]
    assert server.close_calls == 1
    assert "token" not in output.getvalue().lower()


def test_native_bridge_exposes_only_bounded_close_action() -> None:
    bridge = desktop.NativeBridge()
    window = _Window()
    assert bridge.close_app() is False
    bridge._bind(window)
    assert bridge.close_app() is True
    assert window.destroy_calls == 1


def test_run_desktop_wraps_gui_failure_after_cleanup() -> None:
    server = _Server()
    webview = _Webview(start_error=RuntimeError("Cocoa unavailable"))

    with pytest.raises(desktop.DesktopLaunchError, match="Cocoa unavailable"):
        desktop.run_desktop(
            _webview_module=webview,
            _server_factory=lambda **_: server,
        )

    assert server.shutdown_calls == 1
    assert server.run_manager.shutdown_calls == [True]
    assert server.close_calls == 1


def test_unusable_windowed_app_stdout_does_not_abort_launch() -> None:
    class _ClosedStdout:
        def write(self, _value: str) -> None:
            raise ValueError("I/O operation on closed file")

        def flush(self) -> None:
            raise ValueError("I/O operation on closed file")

    server = _Server()
    webview = _Webview()
    desktop.run_desktop(
        stdout=_ClosedStdout(),
        _webview_module=webview,
        _server_factory=lambda **_: server,
    )
    assert len(webview.windows) == 1
    assert server.close_calls == 1


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"port": -1}, "port"),
        ({"port": 65_536}, "port"),
        ({"width": 919}, "at least"),
        ({"height": 619}, "at least"),
    ],
)
def test_run_desktop_validates_options_before_import(kwargs, message) -> None:
    with pytest.raises(ValueError, match=message):
        desktop.run_desktop(**kwargs)


def test_bundled_assets_dir_uses_py2app_resource_path(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    assets = tmp_path / "web_assets"
    assets.mkdir()
    monkeypatch.setenv("RESOURCEPATH", str(tmp_path))
    assert desktop._bundled_assets_dir() == assets


def test_bundled_entry_moves_resources_cwd_to_user_workspace(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    resources = tmp_path / "Maestro.app" / "Contents" / "Resources"
    resources.mkdir(parents=True)
    workspace = tmp_path / "user-workspace"
    monkeypatch.chdir(resources)

    selected = desktop.prepare_desktop_working_directory(
        resource_path=resources,
        fallback_workspace=workspace,
    )

    assert selected == workspace.resolve()
    assert Path.cwd() == workspace.resolve()
    assert not Path.cwd().is_relative_to(resources.parent.parent)


def test_bundled_entry_preserves_intentional_terminal_cwd(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    resources = tmp_path / "Maestro.app" / "Contents" / "Resources"
    resources.mkdir(parents=True)
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.chdir(project)

    selected = desktop.prepare_desktop_working_directory(
        resource_path=resources,
        fallback_workspace=tmp_path / "unused",
    )

    assert selected == project.resolve()
    assert Path.cwd() == project.resolve()


def test_explicit_desktop_workspace_must_be_absolute_and_outside_bundle(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    resources = tmp_path / "Maestro.app" / "Contents" / "Resources"
    resources.mkdir(parents=True)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv(desktop.DESKTOP_WORKSPACE_ENV, "relative/project")

    with pytest.raises(desktop.DesktopLaunchError, match="absolute"):
        desktop.prepare_desktop_working_directory(resource_path=resources)

    monkeypatch.setenv(desktop.DESKTOP_WORKSPACE_ENV, str(resources))
    with pytest.raises(desktop.DesktopLaunchError, match="outside"):
        desktop.prepare_desktop_working_directory(resource_path=resources)


def test_pyinstaller_workspace_accepts_sibling_project(monkeypatch, tmp_path):
    resources = tmp_path / "Programs" / "Maestro" / "_internal"
    resources.mkdir(parents=True)
    project = resources.parent.parent / "my-project"
    project.mkdir()
    monkeypatch.chdir(project)
    assert desktop.prepare_desktop_working_directory(resource_path=resources) == project

def test_main_routes_to_app_subcommand(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[list[str]] = []

    def fake_main(argv):
        seen.append(argv)
        return 7

    monkeypatch.setattr("care.cli.main", fake_main)
    assert desktop.main(["--debug"]) == 7
    assert seen == [["app", "--debug"]]
