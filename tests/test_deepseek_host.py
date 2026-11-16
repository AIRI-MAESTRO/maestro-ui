from __future__ import annotations

import io
import json
import subprocess
import threading
from pathlib import Path
from typing import Any

import pytest

import care.runtime.deepseek_host as deepseek_host_module
from care.runtime.deepseek_host import (
    DSH_EXECUTABLE_ENV,
    DSH_CLIENT_TITLE_ENV,
    DSH_HOME_ENV,
    DSH_SOURCE_ENV,
    DSH_TELEMETRY_DISABLED_ENV,
    MAESTRO_CWD_ENV,
    MAESTRO_ARTIFACT_ROOT_ENV,
    MAESTRO_EXECUTION_ENABLED_ENV,
    MAESTRO_EVOLUTION_ENABLED_ENV,
    MAESTRO_GENERATION_ENABLED_ENV,
    MAESTRO_MEMORY_ENABLED_ENV,
    MAESTRO_MEMORY_WRITE_ENABLED_ENV,
    MAESTRO_PYTHON_ENV,
    MAESTRO_PYTHONHOME_ENV,
    MAESTRO_SESSION_ID_ENV,
    MAESTRO_WORKSPACE_ROOT_ENV,
    PINNED_DSH_VERSION,
    PKG_NATIVE_CACHE_PATH_ENV,
    DeepSeekHarnessCancelled,
    DeepSeekHarnessCommand,
    DeepSeekHarnessDiscoveryError,
    DeepSeekHarnessHost,
    DeepSeekHarnessLaunchError,
    DeepSeekHarnessProtocolError,
    DeepSeekHarnessVersionStatus,
    build_deepseek_harness_argv,
    discover_deepseek_harness,
    parse_deepseek_harness_readiness,
    probe_deepseek_harness_version,
    redact_deepseek_harness_output,
)


TOKEN = "super-secret-bootstrap-token"


def test_frozen_host_uses_bundled_python_helper_and_resources(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executable = tmp_path / "Maestro.app" / "Contents" / "MacOS" / "Maestro"
    executable.parent.mkdir(parents=True)
    executable.write_bytes(b"gui launcher")
    helper = executable.with_name("python")
    helper.write_bytes(b"python helper")
    resources = executable.parent.parent / "Resources"
    (resources / "lib").mkdir(parents=True)

    monkeypatch.setattr(deepseek_host_module.sys, "frozen", True, raising=False)
    monkeypatch.setattr(deepseek_host_module.sys, "executable", str(executable))

    host = DeepSeekHarnessHost(
        workspace=tmp_path,
        dsh_home=tmp_path / "dsh-home",
    )

    assert host._environment[MAESTRO_PYTHON_ENV] == str(helper)
    assert host._environment[MAESTRO_PYTHONHOME_ENV] == str(resources)


class FakeProcess:
    def __init__(
        self,
        output: str,
        *,
        initial_code: int | None = None,
        hang_on_terminate: bool = False,
    ) -> None:
        self.stdout = io.StringIO(output)
        self.code = initial_code
        self.hang_on_terminate = hang_on_terminate
        self.terminate_calls = 0
        self.kill_calls = 0
        self.wait_calls: list[float | None] = []

    def poll(self) -> int | None:
        return self.code

    def terminate(self) -> None:
        self.terminate_calls += 1
        if not self.hang_on_terminate:
            self.code = 0

    def kill(self) -> None:
        self.kill_calls += 1
        self.code = -9

    def wait(self, timeout: float | None = None) -> int:
        self.wait_calls.append(timeout)
        if self.code is None:
            raise subprocess.TimeoutExpired("dsh", timeout)
        return self.code


class PopenSpy:
    def __init__(self, process: FakeProcess) -> None:
        self.process = process
        self.calls: list[tuple[list[str], dict[str, Any]]] = []
        self.called = threading.Event()

    def __call__(self, argv: list[str], **kwargs: Any) -> FakeProcess:
        self.calls.append((argv, kwargs))
        self.called.set()
        return self.process


class BlockingStream:
    def __init__(self) -> None:
        self.closed = threading.Event()

    def __iter__(self) -> BlockingStream:
        return self

    def __next__(self) -> str:
        self.closed.wait(timeout=2.0)
        raise StopIteration

    def close(self) -> None:
        self.closed.set()


def _command() -> DeepSeekHarnessCommand:
    return DeepSeekHarnessCommand(
        ("/opt/bin/dsh",),
        "installed",
        observed_version=PINNED_DSH_VERSION,
    )


def _write_source_checkout(root: Path, *, built_cli: bool) -> None:
    root.mkdir(parents=True)
    (root / "package.json").write_text(
        json.dumps({"name": "@deepseek-ai/dsh-root"}),
        encoding="utf-8",
    )
    frontend = root / "apps" / "web" / "dist" / "index.html"
    frontend.parent.mkdir(parents=True)
    frontend.write_text("<!doctype html>", encoding="utf-8")
    if built_cli:
        entry = root / "apps" / "cli" / "lib" / "bin.js"
    else:
        entry = root / "apps" / "cli" / "src" / "bin.ts"
        loader = root / "node_modules" / "tsx" / "dist" / "esm" / "index.mjs"
        loader.parent.mkdir(parents=True)
        loader.write_text("", encoding="utf-8")
    entry.parent.mkdir(parents=True)
    entry.write_text("", encoding="utf-8")


def _write_bundled_runtime(resource_root: Path) -> Path:
    runtime_root = resource_root / deepseek_host_module.BUNDLED_DSH_RUNTIME_DIRECTORY
    runtime_root.mkdir(parents=True)
    for filename in deepseek_host_module.BUNDLED_DSH_RUNTIME_FILENAMES:
        artifact = runtime_root / filename
        artifact.write_bytes(b"official runtime artifact")
        artifact.chmod(0o700)
    return runtime_root


def test_build_argv_owns_loopback_ephemeral_web_flags(tmp_path: Path) -> None:
    patch_file = tmp_path / "maestro.patch.yml"
    argv = build_deepseek_harness_argv(
        _command(),
        patch_file=patch_file,
    )
    assert argv == (
        "/opt/bin/dsh",
        "--profile",
        "maestro-care",
        "--patch",
        str(patch_file.resolve()),
        "--no-open",
        "--host",
        "127.0.0.1",
        "--port",
        "0",
    )


def test_build_argv_supports_named_profile_without_shell() -> None:
    argv = build_deepseek_harness_argv(
        _command(),
        profile="maestro-care",
    )
    assert argv[:3] == ("/opt/bin/dsh", "--profile", "maestro-care")


@pytest.mark.parametrize("profile", ["", "../web", "bad/name", "x" * 65, "space here"])
def test_build_argv_rejects_invalid_profile(profile: str) -> None:
    with pytest.raises(ValueError, match="profile"):
        build_deepseek_harness_argv(_command(), profile=profile)


def test_discovery_prefers_maestro_executable_override(tmp_path: Path) -> None:
    executable = tmp_path / "dsh-custom"
    executable.write_text("#!/bin/sh\n", encoding="utf-8")
    executable.chmod(0o700)
    resources = tmp_path / "Maestro.app" / "Contents" / "Resources"
    _write_bundled_runtime(resources)
    command = discover_deepseek_harness(
        environ={
            DSH_EXECUTABLE_ENV: str(executable),
            "RESOURCEPATH": str(resources),
        },
        which=lambda _name: None,
    )
    assert command.argv == (str(executable.resolve()),)
    assert command.provenance == "installed"


def test_discovery_prefers_bundled_runtime_over_sibling_and_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resources = tmp_path / "Maestro.app" / "Contents" / "Resources"
    runtime_root = _write_bundled_runtime(resources)
    sibling = tmp_path / "deepseek-harness"
    _write_source_checkout(sibling, built_cli=True)
    dsh = tmp_path / "dsh"
    node = tmp_path / "node"
    dsh.write_text("", encoding="utf-8")
    node.write_text("", encoding="utf-8")
    monkeypatch.setattr(
        deepseek_host_module,
        "_default_sibling_checkout",
        lambda: sibling,
    )

    command = discover_deepseek_harness(
        environ={"RESOURCEPATH": str(resources)},
        which=lambda name: str({"dsh": dsh, "node": node}[name]),
    )

    assert command == DeepSeekHarnessCommand(
        (str(runtime_root / deepseek_host_module.BUNDLED_DSH_RUNTIME_EXECUTABLE),),
        "bundled-runtime",
    )


@pytest.mark.parametrize(
    "unsafe_kind",
    ["missing", "symlink", "directory", "not-executable", "unexpected"],
)
def test_discovery_fails_closed_for_unsafe_bundled_runtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    unsafe_kind: str,
) -> None:
    resources = tmp_path / "Maestro.app" / "Contents" / "Resources"
    runtime_root = _write_bundled_runtime(resources)
    candidate = runtime_root / deepseek_host_module.BUNDLED_DSH_RUNTIME_FILENAMES[-1]
    if unsafe_kind == "missing":
        candidate.unlink()
    elif unsafe_kind == "symlink":
        candidate.unlink()
        candidate.symlink_to(
            runtime_root / deepseek_host_module.BUNDLED_DSH_RUNTIME_EXECUTABLE
        )
    elif unsafe_kind == "directory":
        candidate.unlink()
        candidate.mkdir()
    elif unsafe_kind == "not-executable":
        candidate.chmod(0o600)
    else:
        (runtime_root / "unexpected").write_bytes(b"not part of the release")

    sibling = tmp_path / "deepseek-harness"
    _write_source_checkout(sibling, built_cli=True)
    dsh = tmp_path / "dsh"
    node = tmp_path / "node"
    dsh.write_text("", encoding="utf-8")
    node.write_text("", encoding="utf-8")
    monkeypatch.setattr(
        deepseek_host_module,
        "_default_sibling_checkout",
        lambda: sibling,
    )

    with pytest.raises(DeepSeekHarnessDiscoveryError, match="bundled"):
        discover_deepseek_harness(
            environ={"RESOURCEPATH": str(resources)},
            which=lambda name: str({"dsh": dsh, "node": node}[name]),
        )


def test_discovery_falls_back_to_installed_when_sibling_is_unusable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dsh = tmp_path / "dsh"
    dsh.write_text("", encoding="utf-8")
    sibling = tmp_path / "unbuilt-source"
    sibling.mkdir()
    monkeypatch.setattr(
        deepseek_host_module,
        "_default_sibling_checkout",
        lambda: sibling,
    )

    def which(name: str) -> str | None:
        return str(dsh) if name == "dsh" else None

    command = discover_deepseek_harness(environ={}, which=which)
    assert command == DeepSeekHarnessCommand(
        (str(dsh.resolve()),),
        "installed",
    )


def test_discovery_prefers_prepared_sibling_over_path_binary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sibling = tmp_path / "deepseek-harness"
    _write_source_checkout(sibling, built_cli=True)
    dsh = tmp_path / "dsh"
    node = tmp_path / "node"
    dsh.write_text("", encoding="utf-8")
    node.write_text("", encoding="utf-8")
    monkeypatch.setattr(
        deepseek_host_module,
        "_default_sibling_checkout",
        lambda: sibling,
    )

    command = discover_deepseek_harness(
        environ={},
        which=lambda name: str({"dsh": dsh, "node": node}[name]),
    )

    assert command.provenance == "source-built"
    assert command.source_root == sibling.resolve()


@pytest.mark.parametrize(
    ("built_cli", "provenance"),
    [(True, "source-built"), (False, "source-typescript")],
)
def test_discovery_resolves_prepared_source_checkout(
    tmp_path: Path,
    built_cli: bool,
    provenance: str,
) -> None:
    root = tmp_path / "deepseek-harness"
    _write_source_checkout(root, built_cli=built_cli)
    resources = tmp_path / "Maestro.app" / "Contents" / "Resources"
    _write_bundled_runtime(resources)
    node = tmp_path / "node"
    node.write_text("", encoding="utf-8")
    command = discover_deepseek_harness(
        source_root=root,
        environ={"RESOURCEPATH": str(resources)},
        which=lambda name: str(node) if name == "node" else None,
    )
    assert command.provenance == provenance
    assert command.source_root == root.resolve()
    assert command.argv[0] == str(node.resolve())
    if not built_cli:
        assert command.argv[1] == "--import"
        assert command.argv[-1].endswith("apps/cli/src/bin.ts")


def test_explicit_source_without_web_build_fails_before_spawn(tmp_path: Path) -> None:
    root = tmp_path / "deepseek-harness"
    root.mkdir()
    (root / "package.json").write_text(
        json.dumps({"name": "@deepseek-ai/dsh-root"}),
        encoding="utf-8",
    )
    with pytest.raises(DeepSeekHarnessDiscoveryError, match="pnpm run build"):
        discover_deepseek_harness(
            environ={DSH_SOURCE_ENV: str(root)},
            which=lambda name: "/usr/bin/node" if name == "node" else None,
        )


def test_version_probe_reports_drift_without_starting_profile(tmp_path: Path) -> None:
    calls: list[tuple[list[str], dict[str, Any]]] = []

    def runner(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, 0, stdout="0.1.1-rc.2\n")

    command = DeepSeekHarnessCommand(("/usr/local/bin/dsh",), "installed")
    status = probe_deepseek_harness_version(
        command,
        runner=runner,
        environ={DSH_HOME_ENV: str(tmp_path / "dsh-home")},
        cwd=tmp_path,
    )

    assert status.observed == "0.1.1-rc.2"
    assert status.expected == PINNED_DSH_VERSION
    assert not status.compatible
    assert calls[0][0] == ["/usr/local/bin/dsh", "-V"]
    assert calls[0][1]["env"][DSH_HOME_ENV] == str(tmp_path / "dsh-home")
    assert calls[0][1]["cwd"] == str(tmp_path)


def test_parse_readiness_keeps_token_only_in_explicit_launch_url() -> None:
    ready = parse_deepseek_harness_readiness(
        f"dsh web: http://127.0.0.1:49152/?token={TOKEN}\n",
    )
    assert ready is not None
    assert ready.origin == "http://127.0.0.1:49152"
    assert ready.port == 49152
    assert ready.authenticated
    assert ready.launch_url.endswith(TOKEN)
    assert TOKEN not in repr(ready)
    assert TOKEN not in str(ready)


def test_parse_readiness_accepts_current_credential_free_dsh_url() -> None:
    ready = parse_deepseek_harness_readiness(
        "dsh web: http://127.0.0.1:52151\n",
    )
    assert ready is not None
    assert ready.origin == "http://127.0.0.1:52151"
    assert ready.launch_url == ready.origin
    assert not ready.authenticated


def test_parse_readiness_ignores_browser_notice() -> None:
    assert (
        parse_deepseek_harness_readiness(
            "dsh web: opening the default browser; pass --no-open to disable",
        )
        is None
    )


@pytest.mark.parametrize(
    "url",
    [
        f"http://0.0.0.0:4000/?token={TOKEN}",
        f"http://localhost:4000/?token={TOKEN}",
        f"https://127.0.0.1:4000/?token={TOKEN}",
        f"http://127.0.0.1:4000/other?token={TOKEN}",
        f"http://127.0.0.1:4000/?token={TOKEN}&extra=1",
        "http://127.0.0.1:4000/?unexpected=1",
    ],
)
def test_parse_readiness_rejects_noncanonical_endpoint_without_leaking_token(
    url: str,
) -> None:
    with pytest.raises(DeepSeekHarnessProtocolError) as caught:
        parse_deepseek_harness_readiness(f"dsh web: {url}")
    assert TOKEN not in str(caught.value)
    assert url not in str(caught.value)


def test_output_redaction_covers_query_and_plain_token_fields() -> None:
    raw = f"open http://127.0.0.1:1234/?token={TOKEN} token: {TOKEN} token={TOKEN}"
    redacted = redact_deepseek_harness_output(raw)
    assert TOKEN not in redacted
    assert redacted.count("<redacted>") == 3


def test_host_generates_distinct_crypto_opaque_session_ids(tmp_path: Path) -> None:
    first = DeepSeekHarnessHost(
        command=_command(),
        workspace=tmp_path,
        dsh_home=tmp_path / "dsh-home-a",
        maestro_integration=False,
    )
    second = DeepSeekHarnessHost(
        command=_command(),
        workspace=tmp_path,
        dsh_home=tmp_path / "dsh-home-b",
        maestro_integration=False,
    )

    assert first.session_id != second.session_id
    assert len(first.session_id) >= 24
    assert all(
        character.isalnum() or character in "_-" for character in first.session_id
    )


def test_explicit_care_python_does_not_probe_frozen_default(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def forbidden_default() -> tuple[Path, Path | None]:
        raise AssertionError("explicit care_python must bypass frozen discovery")

    monkeypatch.setattr(
        deepseek_host_module,
        "_default_care_python",
        forbidden_default,
    )
    explicit_python = tmp_path / "venv" / "bin" / "python"
    host = DeepSeekHarnessHost(
        command=_command(),
        workspace=tmp_path,
        dsh_home=tmp_path / "dsh-home",
        care_python=explicit_python,
        environ={MAESTRO_PYTHONHOME_ENV: "/untrusted/python-home"},
    )

    assert host._environment[MAESTRO_PYTHON_ENV] == str(explicit_python)
    assert MAESTRO_PYTHONHOME_ENV not in host._environment


def test_host_rejects_unsafe_explicit_session_id(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="session_id"):
        DeepSeekHarnessHost(
            command=_command(),
            workspace=tmp_path,
            dsh_home=tmp_path / "dsh-home",
            session_id="../other-session",
        )


def test_host_spawns_without_shell_and_stops_idempotently(tmp_path: Path) -> None:
    process = FakeProcess(
        "booting\n"
        f"dsh web: http://127.0.0.1:4567/?token={TOKEN}\n"
        f"debug token={TOKEN}\n",
    )
    popen = PopenSpy(process)
    venv_python = tmp_path / "venv" / "bin" / "python"
    venv_python.parent.mkdir(parents=True)
    venv_python.symlink_to("/usr/bin/env")
    host = DeepSeekHarnessHost(
        command=_command(),
        workspace=tmp_path,
        dsh_home=tmp_path / "dsh-home",
        session_id="test-session",
        artifact_root=tmp_path / "private-artifacts",
        care_python=venv_python,
        environ={
            "SAFE": "1",
            DSH_CLIENT_TITLE_ENV: "Other",
            PKG_NATIVE_CACHE_PATH_ENV: "/tmp/untrusted-native-cache",
        },
        popen=popen,
    )

    ready = host.start()

    assert ready.origin == "http://127.0.0.1:4567"
    assert host.is_running
    assert len(popen.calls) == 1
    argv, kwargs = popen.calls[0]
    assert argv[:4] == ["/opt/bin/dsh", "--profile", "maestro-care", "--patch"]
    patch_file = Path(argv[4])
    assert patch_file.is_file()
    assert patch_file.stat().st_mode & 0o077 == 0
    rendered_patch = patch_file.read_text(encoding="utf-8")
    assert "__MAESTRO_POLICY_MODULE_JSON__" not in rendered_patch
    assert "__MAESTRO_JOBS_MODULE_JSON__" not in rendered_patch
    assert "__MAESTRO_BRANDING_MODULE_JSON__" not in rendered_patch
    assert (
        str(
            (
                Path(__file__).resolve().parents[1]
                / "integrations"
                / "deepseek-harness"
                / "policy.mjs"
            ).resolve()
        )
        in rendered_patch
    )
    assert (
        str(
            (
                Path(__file__).resolve().parents[1]
                / "integrations"
                / "deepseek-harness-maestro-branding"
                / "index.mjs"
            ).resolve()
        )
        in rendered_patch
    )
    assert (
        str(
            (
                Path(__file__).resolve().parents[1]
                / "integrations"
                / "deepseek-harness-maestro-jobs"
                / "index.mjs"
            ).resolve()
        )
        in rendered_patch
    )
    assert argv[5:] == [
        "--no-open",
        "--host",
        "127.0.0.1",
        "--port",
        "0",
    ]
    assert kwargs["cwd"] == str(tmp_path.resolve())
    assert kwargs["env"]["SAFE"] == "1"
    assert kwargs["env"][DSH_HOME_ENV] == str((tmp_path / "dsh-home").resolve())
    assert kwargs["env"][DSH_CLIENT_TITLE_ENV] == "MAESTRO"
    assert kwargs["env"][DSH_TELEMETRY_DISABLED_ENV] == "1"
    assert kwargs["env"][PKG_NATIVE_CACHE_PATH_ENV] == str(
        (tmp_path / "dsh-home" / "native-cache").resolve()
    )
    assert (tmp_path / "dsh-home").stat().st_mode & 0o077 == 0
    assert (tmp_path / "dsh-home" / "native-cache").stat().st_mode & 0o077 == 0
    assert kwargs["env"][MAESTRO_PYTHON_ENV] == str(venv_python.absolute())
    assert kwargs["env"][MAESTRO_PYTHON_ENV] != str(venv_python.resolve())
    assert Path(kwargs["env"][MAESTRO_CWD_ENV]).is_absolute()
    assert kwargs["env"][MAESTRO_WORKSPACE_ROOT_ENV] == str(tmp_path.resolve())
    assert kwargs["env"][MAESTRO_SESSION_ID_ENV] == "test-session"
    assert kwargs["env"][MAESTRO_ARTIFACT_ROOT_ENV] == str(
        (tmp_path / "private-artifacts").resolve()
    )
    assert kwargs["env"][MAESTRO_MEMORY_ENABLED_ENV] == "true"
    assert kwargs["env"][MAESTRO_GENERATION_ENABLED_ENV] == "true"
    assert kwargs["env"][MAESTRO_EXECUTION_ENABLED_ENV] == "true"
    assert kwargs["env"][MAESTRO_MEMORY_WRITE_ENABLED_ENV] == "true"
    assert kwargs["env"][MAESTRO_EVOLUTION_ENABLED_ENV] == "true"
    assert (tmp_path / "private-artifacts").stat().st_mode & 0o077 == 0
    assert "MAESTRO_DSH_POLICY_MODULE" not in kwargs["env"]
    assert kwargs["stdin"] is subprocess.DEVNULL
    assert kwargs["stdout"] is subprocess.PIPE
    assert kwargs["stderr"] is subprocess.STDOUT
    assert kwargs["start_new_session"] is True
    assert "shell" not in kwargs

    profile_dir = tmp_path / "dsh-home" / "profiles" / "maestro-care"
    profile_manifest = json.loads(
        (profile_dir / "package.json").read_text(encoding="utf-8")
    )
    assert profile_manifest["dsh"]["profile"] == {
        "bundles": ["@deepseek-ai/dsh-base", "@deepseek-ai/dsh-web-app"],
        "patchReload": "startup",
    }
    assert (profile_dir / "cordis.patch.yml").read_text(encoding="utf-8") == "[]\n"
    assert profile_dir.stat().st_mode & 0o077 == 0

    host.stop()
    host.stop()

    assert process.terminate_calls == 1
    assert process.kill_calls == 0
    assert not host.is_running
    assert not patch_file.exists()
    assert TOKEN not in repr(host.diagnostics)


def test_host_can_explicitly_disable_memory_without_omitting_flag(
    tmp_path: Path,
) -> None:
    process = FakeProcess("dsh web: http://127.0.0.1:4567\n")
    popen = PopenSpy(process)
    host = DeepSeekHarnessHost(
        command=_command(),
        workspace=tmp_path,
        dsh_home=tmp_path / "dsh-home",
        session_id="memory-off-session",
        memory_enabled=False,
        popen=popen,
    )

    host.start()
    host.stop()

    assert popen.calls[0][1]["env"][MAESTRO_MEMORY_ENABLED_ENV] == "false"
    assert popen.calls[0][1]["env"][MAESTRO_MEMORY_WRITE_ENABLED_ENV] == "false"


def test_host_can_explicitly_disable_generation_without_omitting_flag(
    tmp_path: Path,
) -> None:
    process = FakeProcess("dsh web: http://127.0.0.1:4567\n")
    popen = PopenSpy(process)
    host = DeepSeekHarnessHost(
        command=_command(),
        workspace=tmp_path,
        dsh_home=tmp_path / "dsh-home",
        session_id="generation-off-session",
        generation_enabled=False,
        popen=popen,
    )

    host.start()
    host.stop()

    assert popen.calls[0][1]["env"][MAESTRO_GENERATION_ENABLED_ENV] == "false"


def test_host_can_explicitly_disable_execution_and_memory_write(
    tmp_path: Path,
) -> None:
    process = FakeProcess("dsh web: http://127.0.0.1:4567\n")
    popen = PopenSpy(process)
    host = DeepSeekHarnessHost(
        command=_command(),
        workspace=tmp_path,
        dsh_home=tmp_path / "dsh-home",
        session_id="effects-off-session",
        execution_enabled=False,
        memory_write_enabled=False,
        evolution_enabled=False,
        popen=popen,
    )

    host.start()
    host.stop()

    environment = popen.calls[0][1]["env"]
    assert environment[MAESTRO_EXECUTION_ENABLED_ENV] == "false"
    assert environment[MAESTRO_MEMORY_WRITE_ENABLED_ENV] == "false"
    assert environment[MAESTRO_EVOLUTION_ENABLED_ENV] == "false"


def test_host_rejects_memory_write_without_memory(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="requires memory_enabled"):
        DeepSeekHarnessHost(
            command=_command(),
            workspace=tmp_path,
            dsh_home=tmp_path / "dsh-home",
            memory_enabled=False,
            memory_write_enabled=True,
        )


def test_incompatible_native_profile_fails_before_process_spawn(
    tmp_path: Path,
) -> None:
    profile = tmp_path / "dsh-home" / "profiles" / "maestro-care"
    profile.mkdir(parents=True)
    (profile / "package.json").write_text(
        json.dumps(
            {
                "name": "dsh-profile-maestro-care",
                "private": True,
                "dependencies": {},
                "dsh": {
                    "profile": {
                        "bundles": ["@deepseek-ai/dsh-base"],
                        "patchReload": "live",
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    process = FakeProcess("dsh web: http://127.0.0.1:4567\n")
    popen = PopenSpy(process)
    host = DeepSeekHarnessHost(
        command=_command(),
        workspace=tmp_path,
        dsh_home=tmp_path / "dsh-home",
        popen=popen,
    )

    with pytest.raises(DeepSeekHarnessLaunchError, match="incompatible"):
        host.start()

    assert popen.calls == []


def test_protocol_failure_terminates_child_without_token_in_error(
    tmp_path: Path,
) -> None:
    process = FakeProcess(
        f"dsh web: http://192.168.1.20:4567/?token={TOKEN}\n",
    )
    host = DeepSeekHarnessHost(
        command=_command(),
        workspace=tmp_path,
        dsh_home=tmp_path / "dsh-home",
        popen=PopenSpy(process),
    )
    with pytest.raises(DeepSeekHarnessProtocolError) as caught:
        host.start()
    assert TOKEN not in str(caught.value)
    assert TOKEN not in repr(caught.value)
    assert process.terminate_calls == 1


def test_early_exit_reports_only_redacted_diagnostics(tmp_path: Path) -> None:
    process = FakeProcess(
        f"provider token={TOKEN}\nstartup failed\n",
        initial_code=17,
    )
    host = DeepSeekHarnessHost(
        command=_command(),
        workspace=tmp_path,
        dsh_home=tmp_path / "dsh-home",
        popen=PopenSpy(process),
    )
    with pytest.raises(DeepSeekHarnessLaunchError) as caught:
        host.start()
    message = str(caught.value)
    assert "code 17" in message
    assert "startup failed" in message
    assert "<redacted>" in message
    assert TOKEN not in message


def test_timeout_escalates_from_terminate_to_kill(tmp_path: Path) -> None:
    process = FakeProcess("", hang_on_terminate=True)
    host = DeepSeekHarnessHost(
        command=_command(),
        workspace=tmp_path,
        dsh_home=tmp_path / "dsh-home",
        startup_timeout=0.02,
        shutdown_timeout=0.01,
        popen=PopenSpy(process),
    )
    with pytest.raises(DeepSeekHarnessLaunchError, match="did not become ready"):
        host.start()
    assert process.terminate_calls == 1
    assert process.kill_calls == 1


def test_shutdown_kills_remaining_process_group_after_leader_exits(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = FakeProcess("")
    process.pid = 4242  # type: ignore[attr-defined]
    signals: list[int] = []

    def fake_killpg(process_group: int, selected_signal: int) -> None:
        assert process_group == 4242
        signals.append(selected_signal)
        if selected_signal == deepseek_host_module.signal.SIGTERM:
            process.code = 0

    monkeypatch.setattr(deepseek_host_module.os, "killpg", fake_killpg)
    host = DeepSeekHarnessHost(
        command=_command(),
        workspace=tmp_path,
        dsh_home=tmp_path / "dsh-home",
        shutdown_timeout=0.01,
    )

    host._terminate_process(process)

    assert signals[0] == deepseek_host_module.signal.SIGTERM
    assert 0 in signals
    assert signals[-1] == deepseek_host_module.signal.SIGKILL
    assert process.kill_calls == 0


def test_version_drift_fails_before_web_spawn(tmp_path: Path) -> None:
    process = FakeProcess("")
    popen = PopenSpy(process)
    command = DeepSeekHarnessCommand(
        ("/opt/homebrew/bin/dsh",),
        "installed",
        observed_version="0.1.1-rc.2",
    )
    host = DeepSeekHarnessHost(
        command=command,
        workspace=tmp_path,
        dsh_home=tmp_path / "dsh-home",
        popen=popen,
    )

    with pytest.raises(DeepSeekHarnessLaunchError, match="version drift") as caught:
        host.start()

    assert PINNED_DSH_VERSION in str(caught.value)
    assert "0.1.1-rc.2" in str(caught.value)
    assert host.version_status is not None
    assert not host.version_status.compatible
    assert popen.calls == []


def test_host_default_probe_path_passes_isolated_home_and_workspace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: list[tuple[DeepSeekHarnessCommand, dict[str, Any]]] = []

    def probe(
        command: DeepSeekHarnessCommand,
        **kwargs: Any,
    ) -> DeepSeekHarnessVersionStatus:
        observed.append((command, kwargs))
        return DeepSeekHarnessVersionStatus(
            expected=PINNED_DSH_VERSION,
            observed=PINNED_DSH_VERSION,
            compatible=True,
            provenance=command.provenance,
        )

    monkeypatch.setattr(
        deepseek_host_module,
        "probe_deepseek_harness_version",
        probe,
    )
    command = DeepSeekHarnessCommand(("/opt/bin/dsh",), "installed")
    process = FakeProcess("dsh web: http://127.0.0.1:4567\n")
    host = DeepSeekHarnessHost(
        command=command,
        workspace=tmp_path,
        dsh_home=tmp_path / "dsh-home",
        maestro_integration=False,
        environ={},
        popen=PopenSpy(process),
    )

    host.start()
    host.stop()

    assert observed == [
        (
            command,
            {
                    "environ": {
                        DSH_HOME_ENV: str((tmp_path / "dsh-home").resolve()),
                        DSH_CLIENT_TITLE_ENV: "MAESTRO",
                        DSH_TELEMETRY_DISABLED_ENV: "1",
                        PKG_NATIVE_CACHE_PATH_ENV: str(
                            (tmp_path / "dsh-home" / "native-cache").resolve()
                        ),
                    },
                "cwd": tmp_path.resolve(),
            },
        )
    ]


def test_cancel_interrupts_inflight_start_and_terminates_child(tmp_path: Path) -> None:
    process = FakeProcess("")
    process.stdout = BlockingStream()
    popen = PopenSpy(process)
    host = DeepSeekHarnessHost(
        command=_command(),
        workspace=tmp_path,
        dsh_home=tmp_path / "dsh-home",
        popen=popen,
    )
    errors: list[BaseException] = []

    def start() -> None:
        try:
            host.start()
        except BaseException as exc:  # test captures the worker outcome
            errors.append(exc)

    worker = threading.Thread(target=start)
    worker.start()
    assert popen.called.wait(timeout=1.0)

    host.cancel()
    worker.join(timeout=2.0)

    assert not worker.is_alive()
    assert len(errors) == 1
    assert isinstance(errors[0], DeepSeekHarnessCancelled)
    assert process.terminate_calls == 1
    assert process.kill_calls == 0


def test_pre_cancelled_start_does_not_spawn(tmp_path: Path) -> None:
    process = FakeProcess("")
    popen = PopenSpy(process)
    cancelled = threading.Event()
    cancelled.set()
    host = DeepSeekHarnessHost(
        command=_command(),
        workspace=tmp_path,
        dsh_home=tmp_path / "dsh-home",
        popen=popen,
    )
    with pytest.raises(DeepSeekHarnessCancelled):
        host.start(cancel_event=cancelled)
    assert popen.calls == []
