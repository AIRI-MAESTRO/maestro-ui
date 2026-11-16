"""Verify that a built ``Maestro.app`` matches the current checkout.

py2app copies CARE's Python sources and web assets into the application bundle.
That makes it possible to fail closed when a developer accidentally launches or
ships a stale bundle after changing the source tree.
"""

from __future__ import annotations

import argparse
import hashlib
import os
from pathlib import Path
import plistlib
import shutil
import subprocess
import sys
import tempfile
import tomllib


MACOS_DIR = Path(__file__).resolve().parent
REPOSITORY_ROOT = MACOS_DIR.parents[1]
DEFAULT_BUNDLE = REPOSITORY_ROOT / "dist" / "Maestro.app"
KEYCHAIN_RESOURCE_RELATIVE_FILES = (
    Path("README.md"),
    Path("cordis.patch.yml"),
    Path("index.mjs"),
    Path("keychain-provider.json"),
    Path("native/keychain.cc"),
    Path("package.json"),
    Path("scripts/build-native.mjs"),
)
KEYCHAIN_FORBIDDEN_NATIVE_SUFFIXES = frozenset({".dylib", ".node", ".so"})
DEEPSEEK_RUNTIME_FILENAMES = (
    "deepseek-harness-sdk-runtime-macos-arm64",
    "deepseek-harness-sdk-runtime-macos-arm64-rg",
    "deepseek-harness-sdk-runtime-macos-arm64-spawn-helper",
)
DEFAULT_DEEPSEEK_RUNTIME_ROOT = (
    REPOSITORY_ROOT.parent
    / "deepseek-harness"
    / "python"
    / "sdk-runtime"
    / "src"
    / "deepseek_harness_runtime"
    / "runtime"
)


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _equivalent_after_adhoc_codesign(source: Path, bundled: Path) -> bool:
    """Compare Mach-O payloads after applying the same deterministic signature.

    py2app re-signs nested Mach-O executables while sealing the application.
    Consequently, a byte-for-byte comparison reports a false stale-resource
    error for the bundled ``rg`` and PTY helper even when their executable
    payloads are unchanged.  Re-signing private temporary copies with the same
    basename normalizes that expected packaging-only difference.  Non-Mach-O
    files fail ``codesign`` and remain subject to the ordinary digest check.
    """

    codesign = Path("/usr/bin/codesign")
    if sys.platform != "darwin" or not codesign.is_file():
        return False

    with tempfile.TemporaryDirectory(prefix="maestro-bundle-verify-") as temp_dir:
        temp_root = Path(temp_dir)
        source_copy = temp_root / "source" / source.name
        bundled_copy = temp_root / "bundled" / bundled.name
        source_copy.parent.mkdir()
        bundled_copy.parent.mkdir()
        shutil.copyfile(source, source_copy)
        shutil.copyfile(bundled, bundled_copy)

        for candidate in (source_copy, bundled_copy):
            result = subprocess.run(
                [str(codesign), "--force", "--sign", "-", str(candidate)],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            if result.returncode != 0:
                return False

        return _digest(source_copy) == _digest(bundled_copy)


def _relative_files(root: Path, pattern: str = "*") -> list[Path]:
    return sorted(
        path.relative_to(root)
        for path in root.rglob(pattern)
        if path.is_file() and "__pycache__" not in path.parts
    )


def _compare_tree(
    *,
    source: Path,
    bundled: Path,
    pattern: str,
    label: str,
) -> tuple[list[str], int]:
    errors: list[str] = []
    checked = 0
    if not bundled.is_dir():
        return [f"missing {label} directory: {bundled}"], checked

    for relative in _relative_files(source, pattern):
        source_path = source / relative
        bundled_path = bundled / relative
        if not bundled_path.is_file():
            errors.append(f"missing {label} file: {relative}")
            continue
        checked += 1
        if _digest(source_path) != _digest(bundled_path):
            errors.append(f"stale {label} file: {relative}")
    return errors, checked


def _compare_source_resource_manifest(
    *,
    source: Path,
    bundled: Path,
    relatives: tuple[Path, ...],
    label: str,
    normalize_adhoc_codesign: bool = False,
) -> tuple[list[str], int]:
    """Compare an exact source-only resource manifest and reject extras."""

    errors: list[str] = []
    checked = 0
    if not bundled.is_dir():
        return [f"missing {label} directory: {bundled}"], checked

    expected = set(relatives)
    for relative in relatives:
        source_path = source / relative
        bundled_path = bundled / relative
        if not source_path.is_file():
            errors.append(f"missing source {label} file: {relative}")
            continue
        if not bundled_path.is_file():
            errors.append(f"missing {label} file: {relative}")
            continue
        checked += 1
        if (
            _digest(source_path) != _digest(bundled_path)
            and not (
                normalize_adhoc_codesign
                and _equivalent_after_adhoc_codesign(source_path, bundled_path)
            )
        ):
            errors.append(f"stale {label} file: {relative}")

    for relative in _relative_files(bundled):
        if relative.suffix.lower() in KEYCHAIN_FORBIDDEN_NATIVE_SUFFIXES:
            errors.append(f"forbidden native {label} file: {relative}")
        elif relative not in expected:
            errors.append(f"unexpected {label} file: {relative}")

    return errors, checked


def verify_bundle(
    bundle: Path = DEFAULT_BUNDLE,
    *,
    repository_root: Path = REPOSITORY_ROOT,
    deepseek_runtime_root: Path | None = None,
) -> tuple[list[str], int]:
    """Return verification errors and the number of byte-compared files."""

    bundle = bundle.expanduser().resolve()
    repository_root = repository_root.expanduser().resolve()
    runtime_root = (
        DEFAULT_DEEPSEEK_RUNTIME_ROOT
        if deepseek_runtime_root is None
        else deepseek_runtime_root
    ).expanduser().resolve()
    errors: list[str] = []
    checked = 0

    contents = bundle / "Contents"
    resources = contents / "Resources"
    executable = contents / "MacOS" / "Maestro"
    python_executable = contents / "MacOS" / "python"
    plist_path = contents / "Info.plist"

    if not bundle.is_dir():
        return [f"bundle does not exist: {bundle}"], checked
    if not executable.is_file():
        errors.append(f"missing bundle executable: {executable}")
    if not python_executable.is_file():
        errors.append(f"missing bundled Python helper: {python_executable}")

    if not plist_path.is_file():
        errors.append(f"missing bundle plist: {plist_path}")
    else:
        try:
            with plist_path.open("rb") as stream:
                plist = plistlib.load(stream)
            project = tomllib.loads(
                (repository_root / "pyproject.toml").read_text(encoding="utf-8")
            )
            expected_version = str(project["project"]["version"])
            expected = {
                "CFBundleName": "Maestro",
                "CFBundleIdentifier": "org.airi.maestro",
                "CFBundleShortVersionString": expected_version,
                "CFBundleVersion": expected_version,
                "LSMinimumSystemVersion": "26.0",
                "NSFaceIDUsageDescription": (
                    "MAESTRO uses device biometrics only when you enable App Lock."
                ),
            }
            for key, value in expected.items():
                if plist.get(key) != value:
                    errors.append(
                        f"unexpected plist {key}: {plist.get(key)!r} != {value!r}"
                    )
            python_info = plist.get("PythonInfoDict", {})
            if isinstance(python_info, dict) and python_info.get("PythonExecutable"):
                errors.append(
                    "bundle plist leaks its build interpreter through "
                    "PythonInfoDict.PythonExecutable"
                )
        except (OSError, ValueError, KeyError, TypeError) as exc:
            errors.append(f"cannot validate bundle plist: {exc}")

    python_care_roots = sorted((resources / "lib").glob("python*/care"))
    local_auth_extensions = sorted(
        (resources / "lib").glob(
            "python*/LocalAuthentication/_LocalAuthentication*.so"
        )
    )
    if len(local_auth_extensions) != 1:
        errors.append(
            "expected exactly one bundled LocalAuthentication extension, found "
            f"{len(local_auth_extensions)}"
        )
    if len(python_care_roots) != 1:
        errors.append(
            "expected exactly one bundled Python CARE package, found "
            f"{len(python_care_roots)}"
        )
    else:
        compare_errors, compare_count = _compare_tree(
            source=repository_root / "care",
            bundled=python_care_roots[0],
            pattern="*.py",
            label="Python source",
        )
        errors.extend(compare_errors)
        checked += compare_count

        packaged_asset_errors, packaged_asset_count = _compare_tree(
            source=repository_root / "care" / "web_assets",
            bundled=python_care_roots[0] / "web_assets",
            pattern="*",
            label="packaged web asset",
        )
        errors.extend(packaged_asset_errors)
        checked += packaged_asset_count

    resource_asset_errors, resource_asset_count = _compare_tree(
        source=repository_root / "care" / "web_assets",
        bundled=resources / "web_assets",
        pattern="*",
        label="resource web asset",
    )
    errors.extend(resource_asset_errors)
    checked += resource_asset_count

    integration_errors, integration_count = _compare_tree(
        source=repository_root / "integrations" / "deepseek-harness",
        bundled=resources / "deepseek-harness",
        pattern="*",
        label="DeepSeek Harness integration resource",
    )
    errors.extend(integration_errors)
    checked += integration_count

    preset_errors, preset_count = _compare_tree(
        source=(
            repository_root
            / "integrations"
            / "deepseek-harness-maestro-preset"
        ),
        bundled=resources / "deepseek-harness-maestro-preset",
        pattern="*",
        label="DeepSeek Harness MAESTRO preset resource",
    )
    errors.extend(preset_errors)
    checked += preset_count

    jobs_errors, jobs_count = _compare_tree(
        source=(
            repository_root
            / "integrations"
            / "deepseek-harness-maestro-jobs"
        ),
        bundled=resources / "deepseek-harness-maestro-jobs",
        pattern="*",
        label="DeepSeek Harness MAESTRO jobs resource",
    )
    errors.extend(jobs_errors)
    checked += jobs_count

    settings_errors, settings_count = _compare_tree(
        source=(
            repository_root
            / "integrations"
            / "deepseek-harness-maestro-settings"
        ),
        bundled=resources / "deepseek-harness-maestro-settings",
        pattern="*",
        label="DeepSeek Harness MAESTRO settings resource",
    )
    errors.extend(settings_errors)
    checked += settings_count

    branding_errors, branding_count = _compare_tree(
        source=(
            repository_root
            / "integrations"
            / "deepseek-harness-maestro-branding"
        ),
        bundled=resources / "deepseek-harness-maestro-branding",
        pattern="*",
        label="DeepSeek Harness MAESTRO branding resource",
    )
    errors.extend(branding_errors)
    checked += branding_count

    runtime_errors, runtime_count = _compare_source_resource_manifest(
        source=runtime_root,
        bundled=resources / "deepseek-harness-runtime",
        relatives=tuple(Path(name) for name in DEEPSEEK_RUNTIME_FILENAMES),
        label="DeepSeek Harness runtime",
        normalize_adhoc_codesign=True,
    )
    errors.extend(runtime_errors)
    checked += runtime_count
    for name in DEEPSEEK_RUNTIME_FILENAMES:
        runtime_file = resources / "deepseek-harness-runtime" / name
        if runtime_file.is_file() and not os.access(runtime_file, os.X_OK):
            errors.append(f"non-executable DeepSeek Harness runtime file: {name}")

    keychain_errors, keychain_count = _compare_source_resource_manifest(
        source=(
            repository_root
            / "integrations"
            / "deepseek-harness-maestro-keychain"
        ),
        bundled=resources / "deepseek-harness-maestro-keychain",
        relatives=KEYCHAIN_RESOURCE_RELATIVE_FILES,
        label="DeepSeek Harness MAESTRO Keychain resource",
    )
    errors.extend(keychain_errors)
    checked += keychain_count

    return errors, checked


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Verify that Maestro.app matches the current checkout."
    )
    parser.add_argument(
        "bundle",
        nargs="?",
        type=Path,
        default=DEFAULT_BUNDLE,
        help="path to Maestro.app (default: dist/Maestro.app)",
    )
    args = parser.parse_args(argv)

    errors, checked = verify_bundle(args.bundle)
    if errors:
        print("Maestro.app verification failed:", file=sys.stderr)
        for error in errors:
            print(f"- {error}", file=sys.stderr)
        return 1

    print(f"Maestro.app is fresh: {checked} source and asset files match.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
