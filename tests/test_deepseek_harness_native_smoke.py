"""Provider-free smoke for the native MAESTRO DeepSeek Harness profile."""

from __future__ import annotations

from http.client import HTTPConnection
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from typing import Any
from urllib.parse import urlsplit

import pytest

from care.runtime.deepseek_host import DeepSeekHarnessHost
from care.runtime.credential_broker import CARE_CREDENTIAL_SLOTS
from care.harness_mcp_runtime import CONFIG_PATH_ENV


ROOT = Path(__file__).resolve().parents[1]
DSH_SOURCE = ROOT.parent / "deepseek-harness"
DSH_BUILT = (
    (DSH_SOURCE / "apps" / "web" / "dist" / "index.html").is_file()
    and (DSH_SOURCE / "apps" / "cli" / "lib" / "bin.js").is_file()
)
DSH_SDK_RUNTIME = (
    DSH_SOURCE
    / "python"
    / "sdk-runtime"
    / "src"
    / "deepseek_harness_runtime"
    / "runtime"
    / "deepseek-harness-sdk-runtime-macos-arm64"
)
MACOS_APP = ROOT / "dist" / "Maestro.app"
MACOS_APP_PYTHON = MACOS_APP / "Contents" / "MacOS" / "python"
MACOS_APP_RESOURCES = MACOS_APP / "Contents" / "Resources"


def _request(
    port: int,
    method: str,
    path: str,
    *,
    body: bytes | None = None,
    cookie: str | None = None,
) -> tuple[int, dict[str, str], bytes]:
    connection = HTTPConnection("127.0.0.1", port, timeout=10)
    headers = {"Host": f"127.0.0.1:{port}"}
    if body is not None:
        headers["Content-Type"] = "application/json"
        headers["Content-Length"] = str(len(body))
    if cookie is not None:
        headers["Cookie"] = cookie
    try:
        connection.request(method, path, body=body, headers=headers)
        response = connection.getresponse()
        return (
            response.status,
            {name.casefold(): value for name, value in response.getheaders()},
            response.read(),
        )
    finally:
        connection.close()


def _rpc(port: int, cookie: str, endpoint: str, args: dict[str, Any]) -> Any:
    request_id = f"maestro-smoke-{endpoint.replace('/', '-')}"
    body = json.dumps(
        {
            "type": "client-request",
            "rpcId": request_id,
            "method": endpoint,
            "payload": {"args": args},
        },
        separators=(",", ":"),
    ).encode()
    status, _headers, raw = _request(
        port,
        "POST",
        f"/api/{endpoint}",
        body=body,
        cookie=cookie,
    )
    assert status == 200
    envelope = json.loads(raw)
    assert envelope["type"] == "server-response"
    assert envelope["rpcId"] == request_id
    result = envelope["result"]
    assert result["ok"] is True, result.get("error", {"code": "rpc-failed"})
    return result.get("value")


def _exercise_maestro_web_profile(host: DeepSeekHarnessHost) -> None:
    ready = host.start()
    launch = urlsplit(ready.launch_url)
    launch_path = launch.path or "/"
    if launch.query:
        launch_path = f"{launch_path}?{launch.query}"
    status, headers, _body = _request(ready.port, "GET", launch_path)
    assert status == 303
    set_cookie = headers.get("set-cookie")
    assert set_cookie is not None
    cookie = set_cookie.split(";", 1)[0]

    status, _headers, raw_html = _request(ready.port, "GET", "/", cookie=cookie)
    assert status == 200
    html = raw_html.decode("utf-8")
    match = re.search(
        r'globalThis\["__DSH_BOOT__"\] = (?P<graph>\{[^\n]+?\})</script>',
        html,
    )
    assert match is not None
    graph = json.loads(match.group("graph"))
    assert graph["entries"]
    entry_ids = {entry["id"] for entry in graph["entries"]}
    for plugin_id in (
        "@maestro-care/dsh-client-ui-settings",
        "@maestro-care/dsh-client-ui-branding",
    ):
        assert plugin_id in entry_ids
        plugin_batch = next(
            batch for batch in graph["batches"] if plugin_id in batch["entries"]
        )
        status, plugin_headers, plugin_body = _request(
            ready.port,
            "GET",
            plugin_batch["url"],
            cookie=cookie,
        )
        assert status == 200
        assert "javascript" in plugin_headers.get("content-type", "")
        assert plugin_id.encode() in plugin_body
    assert graph["batches"]
    bootstrap = next(
        batch for batch in graph["batches"] if batch["phase"] == "bootstrap"
    )
    assert "@deepseek-ai/dsh-client-modules" in bootstrap["entries"]
    bootstrap_url = bootstrap["url"].replace("&", "&amp;")
    assert f'<script src="{bootstrap_url}"></script>' in html

    settings = _rpc(ready.port, cookie, "settings/describe", {})
    assert isinstance(settings.get("namespaces"), list)

    try:
        created = _rpc(
            ready.port,
            cookie,
            "session/create",
            {"request": {"cwd": str(ROOT)}},
        )
    except AssertionError as exc:
        raise AssertionError(
            f"session/create failed: {exc}; host diagnostics={host.diagnostics!r}"
        ) from exc
    assert created["agentPreset"] == "maestro"
    sessions = _rpc(
        ready.port,
        cookie,
        "session/list",
        {"_request": {}},
    )
    assert any(
        item["sessionId"] == created["sessionId"]
        for item in sessions["items"]
    )
    assert host.is_running
    assert host.diagnostics == ()


@pytest.mark.skipif(
    not MACOS_APP_PYTHON.is_file(),
    reason="the native Maestro.app bundle is not built",
)
def test_bundled_python_can_start_the_dynamic_anyio_backend() -> None:
    """Catch py2app omissions hidden by source/venv integration smokes."""

    script = """
import asyncio
import anyio

async def main():
    async with anyio.create_task_group():
        pass

asyncio.run(main())
"""
    completed = subprocess.run(
        [str(MACOS_APP_PYTHON), "-P", "-s", "-c", script],
        cwd=Path("/private/tmp"),
        env={
            "PYTHONHOME": str(MACOS_APP_RESOURCES),
            "PYTHONPATH": "",
            "PYTHONNOUSERSITE": "1",
            "PYTHONSAFEPATH": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
        },
        text=True,
        capture_output=True,
        timeout=15,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr or completed.stdout


@pytest.mark.skipif(not DSH_BUILT, reason="pinned DeepSeek Harness source is not built")
def test_native_profile_boots_maestro_preset_and_mcp_without_provider_call(
    tmp_path: Path,
) -> None:
    host = DeepSeekHarnessHost(
        source_root=DSH_SOURCE,
        workspace=ROOT,
        dsh_home=tmp_path / "dsh-home",
        artifact_root=tmp_path / "artifacts",
        care_python=sys.executable,
        care_cwd=ROOT,
        memory_enabled=False,
        generation_enabled=False,
        execution_enabled=False,
        evolution_enabled=False,
        startup_timeout=90,
    )
    try:
        _exercise_maestro_web_profile(host)
    finally:
        host.close()


@pytest.mark.skipif(
    not DSH_SDK_RUNTIME.is_file(),
    reason="exact pinned DeepSeek Harness SDK runtime is not built",
)
def test_official_sdk_runtime_boots_maestro_preset_without_provider_call(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "care.toml"
    config_path.write_text("", encoding="utf-8")
    credential_calls: list[str] = []

    def credential_provider(slot: str) -> None:
        credential_calls.append(slot)
        return None

    host = DeepSeekHarnessHost(
        executable=DSH_SDK_RUNTIME,
        workspace=ROOT,
        dsh_home=tmp_path / "runtime-dsh-home",
        artifact_root=tmp_path / "runtime-artifacts",
        care_python=sys.executable,
        care_cwd=ROOT,
        environ={**os.environ, CONFIG_PATH_ENV: str(config_path)},
        memory_enabled=False,
        generation_enabled=True,
        execution_enabled=False,
        evolution_enabled=False,
        credential_provider=credential_provider,
        startup_timeout=90,
    )
    try:
        _exercise_maestro_web_profile(host)
        assert credential_calls == list(CARE_CREDENTIAL_SLOTS)
    finally:
        host.close()
