"""Release-critical checks: platform closure, manifest validation and MCP helper."""

import importlib.util
import json
import sys
from pathlib import Path

import pytest

from care.runtime import deepseek_host


def load_release_script():
    path = Path(__file__).resolve().parents[1] / "scripts" / "desktop_release.py"
    spec = importlib.util.spec_from_file_location("desktop_release", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(
    "target,arch,count",
    [
        ("macos", "arm64", 3),
        ("macos", "x64", 3),
        ("linux", "x64", 2),
        ("linux", "arm64", 2),
        ("win", "x64", 2),
    ],
)
def test_prepared_runtime_is_discovered_and_missing_sidecar_fails(
    tmp_path,
    monkeypatch,
    target,
    arch,
    count,
):
    release = load_release_script()
    monkeypatch.setattr(release, "ROOT", tmp_path)
    source = tmp_path / "upstream"
    source.mkdir()
    files = release.runtime_files(target, arch)
    assert len(files) == count
    for name in files:
        (source / name).write_bytes(b"runtime")
    release.prepare(source, target, arch)
    resources = tmp_path / "build" / "desktop-resources"
    command = deepseek_host._bundled_runtime_command({"RESOURCEPATH": str(resources)})
    assert Path(command.argv[0]).name == files[0]
    (resources / "deepseek-harness-runtime" / files[-1]).unlink()
    with pytest.raises(deepseek_host.DeepSeekHarnessDiscoveryError, match="incomplete"):
        deepseek_host._bundled_runtime_command({"RESOURCEPATH": str(resources)})


def test_runtime_manifest_rejects_path_traversal(tmp_path):
    runtime = tmp_path / "deepseek-harness-runtime"
    runtime.mkdir()
    (tmp_path / "desktop-runtime.json").write_text(
        json.dumps(
            {
                "executable": "../../foreign",
                "files": ["../../foreign"],
            }
        )
    )
    with pytest.raises(deepseek_host.DeepSeekHarnessDiscoveryError, match="manifest"):
        deepseek_host._bundled_runtime_command({"RESOURCEPATH": str(tmp_path)})


def test_pyinstaller_uses_its_bundled_mcp_helper(tmp_path, monkeypatch):
    executable = tmp_path / "Maestro"
    helper = tmp_path / (
        "maestro-python.exe" if sys.platform == "win32" else "maestro-python"
    )
    helper.touch()
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path), raising=False)
    monkeypatch.setattr(sys, "executable", str(executable))
    assert deepseek_host._default_care_python() == (helper, None)
    helper.unlink()
    with pytest.raises(deepseek_host.DeepSeekHarnessDiscoveryError, match="helper"):
        deepseek_host._default_care_python()
