"""Offline installed-app checks using a disposable native credential and DSH."""

import http.cookiejar
import json
import os
import re
import tempfile
import threading
import time
import traceback
import urllib.request
import uuid
from contextlib import ExitStack
from pathlib import Path
from urllib.parse import urlsplit

import psutil

from care.config import CareConfig
from care.redaction import redact_sensitive_text
from care.runtime.credential_broker import (
    CARE_CREDENTIAL_SLOTS,
    HarnessCredentialResolver,
)
from care.runtime.deepseek_host import DeepSeekHarnessHost
from care.runtime.keystore import MemoryKeystore, make_keystore_url


def verify_client_bootstrap(html: str, fetch_bundle) -> None:
    """Require the compiled profile's real browser modules on every platform."""
    assert "<html" in html.lower(), "bundled UI HTML absent"
    match = re.search(r'globalThis\["__DSH_BOOT__"\] = (\{[^\n]+?\})</script>', html)
    assert match is not None, "bundled UI boot graph absent"
    graph = json.loads(match.group(1))
    core = "@deepseek-ai/dsh-client-modules"
    required = {
        core, "@maestro-care/dsh-client-ui-settings",
        "@maestro-care/dsh-client-ui-branding",
    }
    declared = {entry["id"] for entry in graph["entries"]}
    missing = required - declared
    assert not missing, f"bundled UI graph missing required modules: {sorted(missing)}"
    batched = {entry for batch in graph["batches"] for entry in batch["entries"]}
    assert declared == batched, "bundled UI graph modules do not match served batches"
    bootstrap = next((batch for batch in graph["batches"]
                      if batch["phase"] == "bootstrap" and core in batch["entries"]), None)
    assert bootstrap is not None, "bundled UI bootstrap batch absent"
    script_url = bootstrap["url"].replace("&", "&amp;")
    assert f'<script src="{script_url}"></script>' in html, "bundled UI bootstrap script absent"
    for batch in graph["batches"]:
        path = batch["url"]
        parsed = urlsplit(path)
        assert path.startswith("/plugins/") and not parsed.scheme and not parsed.netloc, (
            "bundled UI batch must use its own plugin route"
        )
        status, content_type, body = fetch_bundle(path)
        assert status == 200, "bundled UI batch request failed"
        assert "javascript" in content_type.lower(), "bundled UI batch is not JavaScript"
        registrations = set(re.findall(
            r'__ModuleLoader__\.load\(\{\s*id:\s*"([^"]+)"', body.decode("utf-8"),
        ))
        assert set(batch["entries"]) <= registrations, "bundled UI batch registrations missing"


def verify_windows_native_secret_capture(api, value: str) -> None:
    """Drive only this process's masked test dialog on the Windows CI desktop."""
    import ctypes
    from ctypes import wintypes

    native = ctypes.WinDLL("user32", use_last_error=True)
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    native.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    native.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    native.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
    native.EnumWindows.argtypes = [callback_type, wintypes.LPARAM]
    native.EnumChildWindows.argtypes = [wintypes.HWND, callback_type, wintypes.LPARAM]
    native.SendMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    failures = []
    entered = threading.Event()
    capture = api._native_secret_capture
    api._native_secret_capture._timeout = 35

    def record_capture(request):
        try:
            return capture(request)
        except Exception:
            failures.append(traceback.format_exc())
            raise

    api._native_secret_capture = record_capture

    def title(handle):
        buffer = ctypes.create_unicode_buffer(256)
        native.GetWindowTextW(handle, buffer, len(buffer))
        return buffer.value

    def drive():
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            windows = []

            @callback_type
            def find(handle, _):
                pid = wintypes.DWORD()
                native.GetWindowThreadProcessId(handle, ctypes.byref(pid))
                if pid.value == os.getpid() and title(handle) == "Set Generation API key":
                    windows.append(handle)
                return True

            native.EnumWindows(find, 0)
            for window in windows:
                fields, buttons = [], []

                @callback_type
                def controls(handle, _):
                    if native.GetWindowLongW(handle, -16) & 0x20:  # ES_PASSWORD
                        fields.append(handle)
                    if title(handle) == "Use key":
                        buttons.append(handle)
                    return True

                native.EnumChildWindows(window, controls, 0)
                if len(fields) == 1 and len(buttons) == 1:
                    buffer = ctypes.create_unicode_buffer(value)
                    native.SendMessageW(fields[0], 0xC, 0, ctypes.cast(buffer, ctypes.c_void_p).value)
                    native.SendMessageW(buttons[0], 0xF5, 0, 0)  # BM_CLICK
                    entered.set()
                    return
            time.sleep(0.1)
        failures.append("masked native dialog absent")

    driver = threading.Thread(target=drive, daemon=True)
    driver.start()
    response = api.maestro_settings_capture_secret("api_key")
    driver.join(timeout=5)
    assert entered.is_set() and not failures, "masked native input did not open: " + "\n".join(failures)
    assert response["payload"]["ok"], "native input failed"
    assert value not in json.dumps(response), "native input reached the web bridge"
    handle = response["payload"]["secret_handle"]
    assert api._secret_handles.pop(handle).value == value


def run(*, gui: bool = False, receipt: Path | None = None) -> None:
    """Exercise the real packaged host/MCP boundary, never user credentials."""
    def phase(name):
        if receipt is not None:
            receipt.write_text(json.dumps({"phase": name}) + "\n", encoding="utf-8")

    phase("starting")
    service = "maestro-ci-" + uuid.uuid4().hex
    value = "sk-ci-" + uuid.uuid4().hex
    if os.name == "nt":
        from care.runtime.windows_keystore import WindowsCredentialManagerKeystore

        keystore = WindowsCredentialManagerKeystore()
    else:
        keystore = MemoryKeystore()
    keystore.store(service, "api-key", value)
    assert keystore.fetch(service, "api-key") == value
    config = CareConfig()
    config.mage.api_key = make_keystore_url(service, "api-key")
    config.memory.api_key = make_keystore_url(service, "api-key")
    requested = set()
    fetched = threading.Event()

    class Resolver(HarnessCredentialResolver):
        def __call__(self, slot):
            result = super().__call__(slot)
            if slot in ("mage.api_key", "memory.api_key"):
                assert result == value
            requested.add(slot)
            if requested == set(CARE_CREDENTIAL_SLOTS):
                fetched.set()
            return result

    loaded = threading.Event()
    failures = []
    children = []
    host = None
    try:
        with tempfile.TemporaryDirectory(prefix="maestro-installed-test-") as directory, ExitStack() as cleanup:
            root = Path(directory).resolve()
            host = DeepSeekHarnessHost(
                workspace=root,
                dsh_home=root / "harness",
                artifact_root=root / "artifacts",
                credential_provider=Resolver(config, keystore_factory=lambda: keystore),
                startup_timeout=90,
                environ={
                    **os.environ,
                    "CARE_MAGE__BASE_URL": "http://127.0.0.1:9/v1",
                    "CARE_MEMORY__BASE_URL": "http://127.0.0.1:9",
                    "CARE_PLATFORM__BASE_URL": "http://127.0.0.1:9",
                    "CARE_PLATFORM__VALIDATION_BASE_URL": "http://127.0.0.1:9/v1",
                    "CARE_PLATFORM__MUTATION_BASE_URL": "http://127.0.0.1:9/v1",
                },
            )
            cleanup.callback(host.close)
            ready = host.start()
            payload = {
                "type": "client-request",
                "rpcId": "maestro-ci-session",
                "method": "session/create",
                "payload": {"args": {"request": {"cwd": str(root)}}},
            }
            opener = urllib.request.build_opener(
                urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar())
            )
            with opener.open(ready.launch_url, timeout=15) as response:
                assert response.status == 200
                html = response.read().decode("utf-8")

            def fetch_bundle(path):
                with opener.open(ready.origin + path, timeout=15) as response:
                    return response.status, response.headers.get("Content-Type", ""), response.read()

            phase("verifying_client_bootstrap")
            verify_client_bootstrap(html, fetch_bundle)
            if not gui:
                request = urllib.request.Request(
                    ready.origin + "/api/session/create",
                    data=json.dumps(payload).encode(),
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with opener.open(request, timeout=90) as response:
                    result = json.load(response)["result"]
                assert result["ok"] is True, json.dumps(result).replace(value, "[redacted]")
                assert result["value"]["agentPreset"] == "maestro"
            if gui:
                phase("opening_webview")
                import webview

                from care.desktop import _configure_webview
                from care.harness_settings import (
                    PyWebViewNativeSettingsApproval,
                    create_harness_settings_api,
                )

                approval = PyWebViewNativeSettingsApproval()
                api = create_harness_settings_api(
                    workspace=root, native_approval=approval
                )
                _configure_webview(webview)
                window = webview.create_window(
                    "MAESTRO CI", ready.launch_url, js_api=api
                )
                approval.bind_window(window)

                def verify_page():
                    try:
                        if os.name == "nt":
                            phase("native_secret_input")
                            verify_windows_native_secret_capture(api, value)
                        phase("creating_gui_session")
                        window.evaluate_js(
                            "window.__maestroCISession = 'pending';"
                            "fetch('/api/session/create', {method:'POST', headers:{'Content-Type':'application/json'}, body:"
                            + json.dumps(json.dumps(payload))
                            + "}).then(r=>r.json()).then(v=>{window.__maestroCISession = "
                            "v.result?.ok && v.result.value.agentPreset==='maestro' ? 'ready' : 'failed';})"
                            ".catch(()=>window.__maestroCISession='failed');"
                        )
                        deadline = time.monotonic() + 60
                        while time.monotonic() < deadline:
                            state = window.evaluate_js("window.__maestroCISession")
                            if state == "failed":
                                raise RuntimeError(
                                    "resident agent session failed to initialize"
                                )
                            if (
                                window.evaluate_js(
                                    "document.body && document.body.innerText.includes('MAESTRO')"
                                )
                                and state == "ready"
                                and fetched.is_set()
                            ):
                                loaded.set()
                                return
                            time.sleep(0.1)
                        page = window.evaluate_js("JSON.stringify({title:document.title,text:document.body?.innerText?.slice(0,1500),state:window.__maestroCISession})")
                        raise RuntimeError("installed native UI did not render: " + str(page))
                    except Exception:
                        failures.append(traceback.format_exc())
                    finally:
                        window.destroy()

                window.events.loaded += verify_page
                timer = threading.Timer(90, window.destroy)
                timer.daemon = True
                timer.start()
                try:
                    webview.start(
                        gui="edgechromium" if os.name == "nt" else None,
                        private_mode=True,
                    )
                finally:
                    timer.cancel()
                assert loaded.is_set() and not failures, (
                    "installed native UI check failed: " + "\n".join(failures)
                )
            assert fetched.wait(timeout=10), "MCP did not hydrate all credential slots"
            assert value not in json.dumps(host._environment)
            assert value not in "\n".join(host.diagnostics)
            children = psutil.Process(host._process.pid).children(recursive=True)
            assert any(
                "maestro-python" in child.name().lower() for child in children
            ), "resident MCP helper absent"
            for path in root.rglob("*"):
                if path.is_file() and path.stat().st_size < 8 * 1024 * 1024:
                    assert value.encode() not in path.read_bytes(), (
                        "credential reached session files"
                    )
            host.close()
            assert host._process is None
            _, alive = psutil.wait_procs(children, timeout=5)
            assert not alive, "resident MCP descendants survived application shutdown"
        result = {
            "client_bootstrap": "passed",
            "credential_handoff": "passed",
            "credential_slots": len(requested),
            "native_gui": "passed" if gui else "not_requested",
            "native_secret_input": "passed" if gui and os.name == "nt" else "not_requested",
            "shutdown": "passed",
        }
        if receipt is not None:
            receipt.write_text(json.dumps(result) + "\n", encoding="utf-8")
        if os.sys.stdout is not None:
            print(json.dumps(result))
    except Exception:
        diagnostic = redact_sensitive_text(traceback.format_exc().replace(value, "[redacted]"))
        if receipt is not None:
            receipt.write_text(json.dumps({"error": diagnostic}) + "\n", encoding="utf-8")
        if host is not None and os.sys.stderr is not None:
            print("\n".join(host.diagnostics).replace(value, "[redacted]"), file=os.sys.stderr)
        raise
    finally:
        if host is not None:
            host.close()
        keystore.delete(service, "api-key")
        assert keystore.fetch(service, "api-key") is None
