from __future__ import annotations

import importlib.util
from pathlib import Path
import runpy
import shutil
import sys
from types import ModuleType

import pytest

from care.runtime import deepseek_host


ROOT = Path(__file__).resolve().parents[1]
MACOS_DIR = ROOT / "packaging" / "macos"


def _load_config():
    path = MACOS_DIR / "py2app_config.py"
    spec = importlib.util.spec_from_file_location("maestro_py2app_config", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_verifier():
    path = MACOS_DIR / "verify_bundle.py"
    spec = importlib.util.spec_from_file_location("maestro_bundle_verifier", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_finalizer():
    path = MACOS_DIR / "bundle_finalize.py"
    spec = importlib.util.spec_from_file_location("maestro_bundle_finalizer", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _fake_runtime_source(tmp_path: Path) -> Path:
    config = _load_config()
    runtime_source = tmp_path / "deepseek-runtime-source"
    runtime_source.mkdir(parents=True, exist_ok=True)
    for index, name in enumerate(config.DEEPSEEK_RUNTIME_FILENAMES):
        artifact = runtime_source / name
        artifact.write_bytes(f"fake runtime artifact {index}\n".encode())
        artifact.chmod(0o755)
    return runtime_source


def _fake_fresh_bundle(tmp_path: Path) -> Path:
    bundle = tmp_path / "Maestro.app"
    contents = bundle / "Contents"
    resources = contents / "Resources"
    package = resources / "lib" / "python3.12" / "care"
    shutil.copytree(ROOT / "care", package)
    local_auth = (
        resources
        / "lib"
        / "python3.12"
        / "LocalAuthentication"
        / "_LocalAuthentication.cpython-312-darwin.so"
    )
    local_auth.parent.mkdir(parents=True)
    local_auth.write_bytes(b"fake LocalAuthentication extension")
    shutil.copytree(ROOT / "care" / "web_assets", resources / "web_assets")
    shutil.copytree(
        ROOT / "integrations" / "deepseek-harness",
        resources / "deepseek-harness",
    )
    shutil.copytree(
        ROOT / "integrations" / "deepseek-harness-maestro-preset",
        resources / "deepseek-harness-maestro-preset",
    )
    shutil.copytree(
        ROOT / "integrations" / "deepseek-harness-maestro-jobs",
        resources / "deepseek-harness-maestro-jobs",
    )
    shutil.copytree(
        ROOT / "integrations" / "deepseek-harness-maestro-settings",
        resources / "deepseek-harness-maestro-settings",
    )
    shutil.copytree(
        ROOT / "integrations" / "deepseek-harness-maestro-branding",
        resources / "deepseek-harness-maestro-branding",
    )
    config = _load_config()
    runtime_source = _fake_runtime_source(tmp_path)
    runtime_resource = resources / "deepseek-harness-runtime"
    runtime_resource.mkdir(parents=True)
    for name in config.DEEPSEEK_RUNTIME_FILENAMES:
        shutil.copy2(runtime_source / name, runtime_resource / name)
    keychain_source = (
        ROOT / "integrations" / "deepseek-harness-maestro-keychain"
    )
    keychain_resource = resources / "deepseek-harness-maestro-keychain"
    for relative in config.KEYCHAIN_RESOURCE_RELATIVE_FILES:
        destination = keychain_resource / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(keychain_source / relative, destination)
    executable = contents / "MacOS" / "Maestro"
    executable.parent.mkdir(parents=True)
    executable.write_bytes(b"fake executable")
    (executable.parent / "python").write_bytes(b"fake Python helper")

    import plistlib

    with (contents / "Info.plist").open("wb") as stream:
        plistlib.dump(config.PLIST, stream)
    return bundle


def _verify_fake_bundle(verifier, bundle: Path) -> tuple[list[str], int]:
    return verifier.verify_bundle(
        bundle,
        repository_root=ROOT,
        deepseek_runtime_root=bundle.parent / "deepseek-runtime-source",
    )


def test_entry_invokes_desktop_cli(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []
    prepare_calls: list[bool] = []
    fake_care = ModuleType("care")
    fake_care.__path__ = []  # type: ignore[attr-defined]
    fake_cli = ModuleType("care.cli")
    fake_desktop = ModuleType("care.desktop")

    def fake_main(argv: list[str]) -> int:
        calls.append(argv)
        return 0

    fake_cli.main = fake_main  # type: ignore[attr-defined]
    fake_desktop.prepare_desktop_working_directory = (  # type: ignore[attr-defined]
        lambda: prepare_calls.append(True)
    )
    monkeypatch.setitem(sys.modules, "care", fake_care)
    monkeypatch.setitem(sys.modules, "care.cli", fake_cli)
    monkeypatch.setitem(sys.modules, "care.desktop", fake_desktop)

    with pytest.raises(SystemExit) as exc_info:
        runpy.run_path(str(MACOS_DIR / "Maestro.py"), run_name="__main__")

    assert exc_info.value.code == 0
    assert prepare_calls == [True]
    assert calls == [["app"]]


def test_py2app_bundle_identity_and_security_contract() -> None:
    config = _load_config()
    finalizer = _load_finalizer()
    plist = config.PLIST

    assert plist["CFBundleName"] == "Maestro"
    assert plist["CFBundleDisplayName"] == "Maestro"
    assert plist["CFBundleIdentifier"] == "org.airi.maestro"
    assert plist["LSMinimumSystemVersion"] == "26.0"
    assert finalizer.MACOS_MINIMUM_VERSION == config.MACOS_MINIMUM_VERSION
    assert plist["NSHumanReadableCopyright"] == "© 2026 MAESTRO"
    assert plist["LSApplicationCategoryType"] == ("public.app-category.developer-tools")
    assert plist["NSHighResolutionCapable"] is True
    assert plist["NSFaceIDUsageDescription"] == (
        "MAESTRO uses device biometrics only when you enable App Lock."
    )
    assert plist["NSRequiresAquaSystemAppearance"] is False
    assert plist["NSAppTransportSecurity"] == {"NSAllowsLocalNetworking": True}
    assert "NSAllowsArbitraryLoads" not in plist["NSAppTransportSecurity"]


def test_optional_legacy_workbench_uses_maestro_product_branding() -> None:
    html = (ROOT / "care" / "web_assets" / "index.html").read_text(
        encoding="utf-8"
    )

    assert "<title>MAESTRO</title>" in html
    assert '<span class="brand-care">HARNESS</span>' in html
    for stale_copy in (
        "Maestro Care",
        "MAESTRO CARE",
        ">CARE<",
        "CARE's advanced config",
        "CARE_*",
    ):
        assert stale_copy not in html


def test_py2app_packages_native_shell_and_pins_builder() -> None:
    config = _load_config()

    assert config.SETUP_KWARGS["name"] == "Maestro"
    assert config.SETUP_KWARGS["app"] == [str(MACOS_DIR / "Maestro.py")]
    assert config.SETUP_KWARGS["setup_requires"] == ["py2app==0.28.10"]
    assert config.SETUP_KWARGS["options"]["bdist"] == {
        "dist_dir": str(ROOT / "dist"),
        "bdist_base": str(ROOT / "build" / "py2app"),
    }
    assert config.PY2APP_OPTIONS["argv_emulation"] is False
    assert config.PY2APP_OPTIONS["no_chdir"] is True
    assert config.PY2APP_OPTIONS["excludes"] == ["PIL"]
    assert config.PY2APP_OPTIONS["strip"] is False
    icon = Path(config.PY2APP_OPTIONS["iconfile"])
    assert icon == MACOS_DIR / "Maestro.icns"
    assert icon.read_bytes()[:4] == b"icns"
    assert set(config.PY2APP_OPTIONS["packages"]) == {
        "anyio",
        "care",
        "gigaevo_client",
        "LocalAuthentication",
        "mcp",
        "mmar_carl",
        "mmar_mage",
        "openai",
        "webview",
    }


def test_web_and_deepseek_assets_are_configured_as_bundle_resources() -> None:
    config = _load_config()
    asset_dir = (ROOT / "care" / "web_assets").resolve()
    integration_dir = (ROOT / "integrations" / "deepseek-harness").resolve()
    preset_dir = (
        ROOT / "integrations" / "deepseek-harness-maestro-preset"
    ).resolve()
    jobs_dir = (
        ROOT / "integrations" / "deepseek-harness-maestro-jobs"
    ).resolve()
    settings_dir = (
        ROOT / "integrations" / "deepseek-harness-maestro-settings"
    ).resolve()
    branding_dir = (
        ROOT / "integrations" / "deepseek-harness-maestro-branding"
    ).resolve()
    keychain_dir = (
        ROOT / "integrations" / "deepseek-harness-maestro-keychain"
    ).resolve()

    assert config.PY2APP_OPTIONS["resources"] == [
        str(asset_dir),
        str(integration_dir),
        str(preset_dir),
        str(jobs_dir),
        str(settings_dir),
        str(branding_dir),
        *config.DEEPSEEK_RUNTIME_RESOURCE_ENTRIES,
        *config.KEYCHAIN_RESOURCE_ENTRIES,
    ]
    assert {
        "app.js",
        "ASSET_PROVENANCE.md",
        "index.html",
        "maestro-mark.svg",
        "styles.css",
    } <= {path.name for path in asset_dir.iterdir() if path.is_file()}

    mascot_dir = asset_dir / "mascot"
    assert {path.name for path in mascot_dir.iterdir() if path.is_file()} == {
        "evoc-cheers.webp",
        "evoc-coffee.webp",
        "evoc-generate.webp",
        "evoc-surprised.webp",
        "evoc-think.webp",
        "evoc-work.webp",
        "hero-mascot.gif",
        "mascot.webp",
    }
    assert {
        "cordis.direct.patch.yml.in",
        "maestro-harness.json",
        "policy.mjs",
    } <= {path.name for path in integration_dir.iterdir() if path.is_file()}
    assert {
        "cordis.patch.yml",
        "package.json",
        "preset-root.mjs",
        "upstream-standard.json",
    } <= {path.name for path in preset_dir.iterdir() if path.is_file()}
    assert {
        "cordis.patch.yml",
        "index.mjs",
        "package.json",
    } <= {path.name for path in jobs_dir.iterdir() if path.is_file()}
    assert {
        "README.md",
        "index.mjs",
        "package.json",
    } <= {path.name for path in settings_dir.iterdir() if path.is_file()}
    assert (settings_dir / "lib" / "client.js").is_file()
    assert {
        "ASSET_PROVENANCE.md",
        "README.md",
        "index.mjs",
        "package.json",
    } <= {path.name for path in branding_dir.iterdir() if path.is_file()}
    assert (branding_dir / "lib" / "client.js").is_file()

    configured_keychain_files = {
        Path(destination).relative_to("deepseek-harness-maestro-keychain")
        / Path(source).name
        for destination, sources in config.KEYCHAIN_RESOURCE_ENTRIES
        for source in sources
    }
    assert configured_keychain_files == set(
        config.KEYCHAIN_RESOURCE_RELATIVE_FILES
    )
    assert all(
        (keychain_dir / relative).is_file()
        for relative in config.KEYCHAIN_RESOURCE_RELATIVE_FILES
    )
    assert not any(
        relative.suffix in config.KEYCHAIN_FORBIDDEN_NATIVE_SUFFIXES
        for relative in configured_keychain_files
    )
    assert not any("tests" in relative.parts for relative in configured_keychain_files)

    assert config.DEEPSEEK_RUNTIME_RESOURCE_ENTRIES == [
        (
            "deepseek-harness-runtime",
            [
                str(config.DEEPSEEK_RUNTIME_SOURCE_DIR / name)
                for name in config.DEEPSEEK_RUNTIME_FILENAMES
            ],
        )
    ]


def test_runtime_resource_validation_requires_all_pinned_artifacts(
    tmp_path: Path,
) -> None:
    config = _load_config()
    runtime_source = tmp_path / "runtime"
    runtime_source.mkdir()

    with pytest.raises(RuntimeError, match="missing pinned DeepSeek Harness runtime"):
        config.validate_deepseek_runtime_resources(runtime_source)

    for name in config.DEEPSEEK_RUNTIME_FILENAMES:
        (runtime_source / name).write_bytes(b"artifact")

    config.validate_deepseek_runtime_resources(runtime_source)


def test_keychain_resource_manifest_fails_closed_on_native_artifact(
    tmp_path: Path,
) -> None:
    config = _load_config()
    artifact = tmp_path / "build" / "Release" / "maestro_keychain.node"
    artifact.parent.mkdir(parents=True)
    artifact.write_bytes(b"not a real native addon")

    with pytest.raises(RuntimeError, match="native Keychain artifacts"):
        config._keychain_resource_entries(tmp_path)


def test_wheel_include_keeps_nested_web_assets() -> None:
    project = (ROOT / "pyproject.toml").read_text(encoding="utf-8")

    assert '"care/web_assets/**/*"' in project


def test_bundle_verifier_accepts_fresh_source_and_asset_copies(
    tmp_path: Path,
) -> None:
    verifier = _load_verifier()
    bundle = _fake_fresh_bundle(tmp_path)

    errors, checked = _verify_fake_bundle(verifier, bundle)

    assert errors == []
    assert checked > 0


def test_bundle_verifier_requires_bundled_python_helper(tmp_path: Path) -> None:
    verifier = _load_verifier()
    bundle = _fake_fresh_bundle(tmp_path)
    helper = bundle / "Contents" / "MacOS" / "python"
    helper.unlink()

    errors, _checked = _verify_fake_bundle(verifier, bundle)

    assert f"missing bundled Python helper: {helper}" in errors


def test_bundle_verifier_requires_local_authentication_extension(
    tmp_path: Path,
) -> None:
    verifier = _load_verifier()
    bundle = _fake_fresh_bundle(tmp_path)
    extension = next(
        (bundle / "Contents" / "Resources" / "lib").glob(
            "python*/LocalAuthentication/_LocalAuthentication*.so"
        )
    )
    extension.unlink()

    errors, _checked = _verify_fake_bundle(verifier, bundle)

    assert (
        "expected exactly one bundled LocalAuthentication extension, found 0"
        in errors
    )


def test_bundle_finalizer_removes_builder_python_and_resigns(tmp_path: Path) -> None:
    import plistlib

    finalizer = _load_finalizer()
    bundle = _fake_fresh_bundle(tmp_path)
    plist_path = bundle / "Contents" / "Info.plist"
    with plist_path.open("rb") as stream:
        plist = plistlib.load(stream)
    plist["PythonInfoDict"] = {
        "PythonExecutable": "/private/build/.venv/bin/python",
        "PythonLongVersion": "3.12.13",
    }
    with plist_path.open("wb") as stream:
        plistlib.dump(plist, stream)
    calls: list[tuple[tuple[str, ...], bool]] = []

    def runner(command, *, check):
        calls.append((tuple(command), check))

    assert finalizer.finalize_local_bundle(bundle, runner=runner) is True
    with plist_path.open("rb") as stream:
        finalized = plistlib.load(stream)
    assert "PythonExecutable" not in finalized["PythonInfoDict"]
    assert finalized["PythonInfoDict"]["PythonLongVersion"] == "3.12.13"
    assert calls == [
        (("codesign", "--force", "--deep", "--sign", "-", str(bundle)), True)
    ]


def test_bundle_verifier_rejects_builder_python_path(tmp_path: Path) -> None:
    import plistlib

    verifier = _load_verifier()
    bundle = _fake_fresh_bundle(tmp_path)
    plist_path = bundle / "Contents" / "Info.plist"
    with plist_path.open("rb") as stream:
        plist = plistlib.load(stream)
    plist["PythonInfoDict"] = {
        "PythonExecutable": "/private/build/.venv/bin/python",
    }
    with plist_path.open("wb") as stream:
        plistlib.dump(plist, stream)

    errors, _checked = _verify_fake_bundle(verifier, bundle)

    assert (
        "bundle plist leaks its build interpreter through "
        "PythonInfoDict.PythonExecutable"
    ) in errors


def test_packager_and_verifier_share_keychain_source_manifest() -> None:
    config = _load_config()
    verifier = _load_verifier()

    assert verifier.KEYCHAIN_RESOURCE_RELATIVE_FILES == (
        config.KEYCHAIN_RESOURCE_RELATIVE_FILES
    )
    assert verifier.KEYCHAIN_FORBIDDEN_NATIVE_SUFFIXES == (
        config.KEYCHAIN_FORBIDDEN_NATIVE_SUFFIXES
    )
    assert verifier.DEEPSEEK_RUNTIME_FILENAMES == (
        config.DEEPSEEK_RUNTIME_FILENAMES
    )
    assert deepseek_host.BUNDLED_DSH_RUNTIME_FILENAMES == (
        config.DEEPSEEK_RUNTIME_FILENAMES
    )


def test_bundle_verifier_rejects_a_stale_python_source(tmp_path: Path) -> None:
    verifier = _load_verifier()
    bundle = _fake_fresh_bundle(tmp_path)
    bundled_web = (
        bundle / "Contents" / "Resources" / "lib" / "python3.12" / "care" / "web.py"
    )
    bundled_web.write_text("# stale bundle\n", encoding="utf-8")

    errors, _checked = _verify_fake_bundle(verifier, bundle)

    assert "stale Python source file: web.py" in errors


def test_bundle_verifier_rejects_stale_deepseek_integration_resource(
    tmp_path: Path,
) -> None:
    verifier = _load_verifier()
    bundle = _fake_fresh_bundle(tmp_path)
    bundled_policy = (
        bundle / "Contents" / "Resources" / "deepseek-harness" / "policy.mjs"
    )
    bundled_policy.write_text("// stale policy\n", encoding="utf-8")

    errors, _checked = _verify_fake_bundle(verifier, bundle)

    assert "stale DeepSeek Harness integration resource file: policy.mjs" in errors


def test_bundle_verifier_rejects_stale_keychain_resource(tmp_path: Path) -> None:
    verifier = _load_verifier()
    bundle = _fake_fresh_bundle(tmp_path)
    bundled_provider = (
        bundle
        / "Contents"
        / "Resources"
        / "deepseek-harness-maestro-keychain"
        / "index.mjs"
    )
    bundled_provider.write_text("// stale provider\n", encoding="utf-8")

    errors, _checked = _verify_fake_bundle(verifier, bundle)

    assert (
        "stale DeepSeek Harness MAESTRO Keychain resource file: index.mjs"
        in errors
    )


def test_bundle_verifier_rejects_packaged_keychain_native_artifact(
    tmp_path: Path,
) -> None:
    verifier = _load_verifier()
    bundle = _fake_fresh_bundle(tmp_path)
    bundled_artifact = (
        bundle
        / "Contents"
        / "Resources"
        / "deepseek-harness-maestro-keychain"
        / "build"
        / "Release"
        / "maestro_keychain.node"
    )
    bundled_artifact.parent.mkdir(parents=True)
    bundled_artifact.write_bytes(b"not a real native addon")

    errors, _checked = _verify_fake_bundle(verifier, bundle)

    assert (
        "forbidden native DeepSeek Harness MAESTRO Keychain resource file: "
        "build/Release/maestro_keychain.node"
    ) in errors


def test_bundle_verifier_rejects_missing_runtime_artifact(tmp_path: Path) -> None:
    verifier = _load_verifier()
    bundle = _fake_fresh_bundle(tmp_path)
    missing_name = verifier.DEEPSEEK_RUNTIME_FILENAMES[0]
    (
        bundle
        / "Contents"
        / "Resources"
        / "deepseek-harness-runtime"
        / missing_name
    ).unlink()

    errors, _checked = _verify_fake_bundle(verifier, bundle)

    assert f"missing DeepSeek Harness runtime file: {missing_name}" in errors


def test_bundle_verifier_rejects_stale_runtime_artifact(tmp_path: Path) -> None:
    verifier = _load_verifier()
    bundle = _fake_fresh_bundle(tmp_path)
    stale_name = verifier.DEEPSEEK_RUNTIME_FILENAMES[1]
    bundled_artifact = (
        bundle
        / "Contents"
        / "Resources"
        / "deepseek-harness-runtime"
        / stale_name
    )
    bundled_artifact.write_bytes(b"tampered runtime")

    errors, _checked = _verify_fake_bundle(verifier, bundle)

    assert f"stale DeepSeek Harness runtime file: {stale_name}" in errors


@pytest.mark.skipif(sys.platform != "darwin", reason="requires macOS codesign")
def test_bundle_verifier_normalizes_nested_macho_signatures(
    tmp_path: Path,
) -> None:
    verifier = _load_verifier()
    source = tmp_path / "source" / "runtime-helper"
    bundled = tmp_path / "bundled" / "runtime-helper"
    source.parent.mkdir()
    bundled.parent.mkdir()
    shutil.copyfile("/usr/bin/true", source)
    shutil.copyfile(source, bundled)

    result = verifier.subprocess.run(
        ["/usr/bin/codesign", "--force", "--sign", "-", str(bundled)],
        check=False,
        capture_output=True,
    )
    assert result.returncode == 0, result.stderr.decode(errors="replace")
    assert verifier._digest(source) != verifier._digest(bundled)
    assert verifier._equivalent_after_adhoc_codesign(source, bundled)


def test_bundle_verifier_requires_executable_runtime_artifacts(
    tmp_path: Path,
) -> None:
    verifier = _load_verifier()
    bundle = _fake_fresh_bundle(tmp_path)
    non_executable_name = verifier.DEEPSEEK_RUNTIME_FILENAMES[2]
    bundled_artifact = (
        bundle
        / "Contents"
        / "Resources"
        / "deepseek-harness-runtime"
        / non_executable_name
    )
    bundled_artifact.chmod(0o644)

    errors, _checked = _verify_fake_bundle(verifier, bundle)

    assert (
        "non-executable DeepSeek Harness runtime file: "
        f"{non_executable_name}"
    ) in errors
