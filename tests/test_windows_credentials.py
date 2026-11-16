import os
import socket
import sys
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from care.runtime import windows_credentials as windows
from care.runtime.credential_broker import HarnessCredentialBrokerError


def test_windows_settings_selects_native_secure_input(monkeypatch, tmp_path):
    from care import harness_settings
    from care.runtime.windows_secret_input import WindowsNativeSecretCapture

    monkeypatch.setattr(harness_settings.sys, "platform", "win32")
    api = harness_settings.create_harness_settings_api(workspace=tmp_path, bridge=object())
    assert isinstance(api._native_secret_capture, WindowsNativeSecretCapture)


@pytest.mark.skipif(os.name != "nt", reason="native Windows ESM module paths")
def test_windows_direct_patch_uses_file_urls_for_native_absolute_paths(tmp_path):
    from care.runtime.deepseek_bundle import materialize_direct_patch

    template = Path("integrations/deepseek-harness/cordis.direct.patch.yml.in").read_text()
    module = tmp_path / "Maestro with spaces" / "plugin.mjs"
    rendered = materialize_direct_patch(template, module, module, module, module, module)
    assert module.as_uri() in rendered


@pytest.mark.parametrize(
    "change", ["parent", "executable", "arguments", "older", "exited"]
)
def test_unrelated_process_cannot_bootstrap_credentials(monkeypatch, change):
    image = str(Path("trusted-helper.exe").resolve())
    data = {
        "parent": 42,
        "exe": image,
        "args": windows.MCP_ARGUMENTS,
        "time": 20,
        "live": True,
    }
    if change == "parent":
        data["parent"] = 55
    if change == "executable":
        data["exe"] = str(Path("other.exe").resolve())
    if change == "arguments":
        data["args"] = ["-c", "untrusted code"]
    if change == "older":
        data["time"] = 5
    if change == "exited":
        data["live"] = False
    child = SimpleNamespace(
        ppid=lambda: data["parent"],
        exe=lambda: data["exe"],
        cmdline=lambda: [image, *data["args"]],
        create_time=lambda: data["time"],
    )
    harness = SimpleNamespace(
        pid=42, is_running=lambda: data["live"], create_time=lambda: 10
    )
    monkeypatch.setattr(windows.psutil, "Process", lambda pid: child)
    with pytest.raises(HarnessCredentialBrokerError, match="not trusted"):
        windows.validate_client(99, harness, Path(image))


def test_matching_live_resident_helper_is_admitted(monkeypatch):
    image = str(Path("trusted-helper.exe").resolve())
    child = SimpleNamespace(
        ppid=lambda: 42,
        exe=lambda: image,
        cmdline=lambda: [image, *windows.MCP_ARGUMENTS],
        create_time=lambda: 20,
    )
    harness = SimpleNamespace(pid=42, is_running=lambda: True, create_time=lambda: 10)
    monkeypatch.setattr(windows.psutil, "Process", lambda pid: child)
    windows.validate_client(99, harness, Path(image))


@pytest.mark.skipif(os.name != "nt", reason="uses native Windows Credential Manager")
def test_windows_native_keystore_roundtrip_overwrite_and_cleanup():
    from care.runtime.keystore import detect_keystore
    from care.runtime.windows_keystore import WindowsCredentialManagerKeystore

    store = detect_keystore()
    assert isinstance(store, WindowsCredentialManagerKeystore)
    service = "maestro-ci-" + uuid.uuid4().hex
    try:
        assert store.fetch(service, "test") is None
        store.store(service, "test", "unicode-test-ключ")
        assert store.fetch_noninteractive(service, "test") == "unicode-test-ключ"
        store.store(service, "test", "replacement")
        assert store.fetch(service, "test") == "replacement"
    finally:
        store.delete(service, "test")
    store.delete(service, "test")
    assert store.fetch(service, "test") is None


@pytest.mark.skipif(os.name != "nt", reason="uses Windows kernel TCP ownership table")
def test_unrelated_local_process_cannot_obtain_bootstrap_or_credentials():
    from care.runtime.credential_broker import _recv_frame, _send_frame

    calls = []
    server = windows.WindowsCredentialBrokerServer(
        session_id="peer-denial-test",
        provider=lambda slot: calls.append(slot),
        helper=Path(sys.executable),
    )
    server.start()
    server.bind_harness(SimpleNamespace(pid=os.getpid(), poll=lambda: None))
    try:
        with socket.create_connection(
            ("127.0.0.1", server.bootstrap.tcp_port), timeout=3
        ) as connection:
            with pytest.raises((HarnessCredentialBrokerError, OSError)):
                # The server can reject this peer before the request is sent.
                _send_frame(
                    connection, {"version": 1, "operation": "bootstrap"}, maximum=2048
                )
                _recv_frame(connection, maximum=4096)
        assert calls == []
    finally:
        server.close()
