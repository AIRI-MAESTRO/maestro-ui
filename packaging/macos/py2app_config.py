"""Declarative py2app configuration for the Maestro macOS bundle.

Keeping this module free of py2app imports lets CI validate the bundle
contract without installing platform-specific packaging dependencies.
"""

from __future__ import annotations

from pathlib import Path
import tomllib


MACOS_DIR = Path(__file__).resolve().parent
REPOSITORY_ROOT = MACOS_DIR.parents[1]
ENTRY_POINT = MACOS_DIR / "Maestro.py"
APP_ICON = MACOS_DIR / "Maestro.icns"
WEB_ASSETS_DIR = REPOSITORY_ROOT / "care" / "web_assets"
DEEPSEEK_INTEGRATION_DIR = REPOSITORY_ROOT / "integrations" / "deepseek-harness"
DEEPSEEK_MAESTRO_PRESET_DIR = (
    REPOSITORY_ROOT / "integrations" / "deepseek-harness-maestro-preset"
)
DEEPSEEK_MAESTRO_JOBS_DIR = (
    REPOSITORY_ROOT / "integrations" / "deepseek-harness-maestro-jobs"
)
DEEPSEEK_MAESTRO_SETTINGS_DIR = (
    REPOSITORY_ROOT / "integrations" / "deepseek-harness-maestro-settings"
)
DEEPSEEK_MAESTRO_BRANDING_DIR = (
    REPOSITORY_ROOT / "integrations" / "deepseek-harness-maestro-branding"
)
DEEPSEEK_MAESTRO_KEYCHAIN_DIR = (
    REPOSITORY_ROOT / "integrations" / "deepseek-harness-maestro-keychain"
)
DEEPSEEK_RUNTIME_SOURCE_DIR = (
    REPOSITORY_ROOT.parent
    / "deepseek-harness"
    / "python"
    / "sdk-runtime"
    / "src"
    / "deepseek_harness_runtime"
    / "runtime"
)
DEEPSEEK_RUNTIME_FILENAMES = (
    "deepseek-harness-sdk-runtime-macos-arm64",
    "deepseek-harness-sdk-runtime-macos-arm64-rg",
    "deepseek-harness-sdk-runtime-macos-arm64-spawn-helper",
)
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
DIST_DIR = REPOSITORY_ROOT / "dist"
BUILD_DIR = REPOSITORY_ROOT / "build" / "py2app"


def _project_version() -> str:
    metadata = tomllib.loads(
        (REPOSITORY_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    )
    return str(metadata["project"]["version"])


VERSION = _project_version()


def _keychain_resource_entries(
    source: Path = DEEPSEEK_MAESTRO_KEYCHAIN_DIR,
) -> list[tuple[str, list[str]]]:
    """Return a source-only py2app manifest for the optional provider bundle."""

    native_artifacts = sorted(
        path.relative_to(source)
        for path in source.rglob("*")
        if path.is_file()
        and path.suffix.lower() in KEYCHAIN_FORBIDDEN_NATIVE_SUFFIXES
    )
    if native_artifacts:
        formatted = ", ".join(str(path) for path in native_artifacts)
        raise RuntimeError(
            "refusing to package native Keychain artifacts; remove them and "
            f"build on the target Mac instead: {formatted}"
        )

    grouped: dict[Path, list[str]] = {}
    for relative in KEYCHAIN_RESOURCE_RELATIVE_FILES:
        source_path = source / relative
        if not source_path.is_file():
            raise RuntimeError(f"missing Keychain provider source: {source_path}")
        destination = Path("deepseek-harness-maestro-keychain") / relative.parent
        grouped.setdefault(destination, []).append(str(source_path))

    return [
        (str(destination), files)
        for destination, files in grouped.items()
    ]


KEYCHAIN_RESOURCE_ENTRIES = _keychain_resource_entries()


def _deepseek_runtime_resource_entries(
    source: Path = DEEPSEEK_RUNTIME_SOURCE_DIR,
) -> list[tuple[str, list[str]]]:
    return [
        (
            "deepseek-harness-runtime",
            [str(source / name) for name in DEEPSEEK_RUNTIME_FILENAMES],
        )
    ]


def validate_deepseek_runtime_resources(
    source: Path = DEEPSEEK_RUNTIME_SOURCE_DIR,
) -> None:
    """Fail a release build unless the exact pinned macOS carrier exists."""

    missing = [name for name in DEEPSEEK_RUNTIME_FILENAMES if not (source / name).is_file()]
    if missing:
        formatted = ", ".join(missing)
        raise RuntimeError(
            "missing pinned DeepSeek Harness runtime artifacts: "
            f"{formatted}; build commit dd6322d604e00eec1ba5e0c8541159906a21094a "
            "with scripts/build-exe-for-python-sdk.ts"
        )


DEEPSEEK_RUNTIME_RESOURCE_ENTRIES = _deepseek_runtime_resource_entries()

MACOS_MINIMUM_VERSION = "26.0"

PLIST = {
    "CFBundleName": "Maestro",
    "CFBundleDisplayName": "Maestro",
    "CFBundleIdentifier": "org.airi.maestro",
    "CFBundleShortVersionString": VERSION,
    "CFBundleVersion": VERSION,
    # The Homebrew Python framework used by this local build has a macOS 26
    # deployment target.  Advertise the real floor so LaunchServices never
    # starts a launcher whose embedded Python cannot be loaded.
    "LSMinimumSystemVersion": MACOS_MINIMUM_VERSION,
    "NSHumanReadableCopyright": "© 2026 MAESTRO",
    "LSApplicationCategoryType": "public.app-category.developer-tools",
    "NSHighResolutionCapable": True,
    "NSFaceIDUsageDescription": (
        "MAESTRO uses device biometrics only when you enable App Lock."
    ),
    # Explicitly opt into the system appearance, including macOS Dark Mode.
    "NSRequiresAquaSystemAppearance": False,
    # The desktop shell only talks to its loopback HTTP server. Do not weaken
    # ATS globally: NSAllowsArbitraryLoads must remain absent.
    "NSAppTransportSecurity": {"NSAllowsLocalNetworking": True},
}

PY2APP_OPTIONS = {
    # argv emulation is unnecessary for a windowed application and can delay
    # startup while macOS event handling is initialised.
    "argv_emulation": False,
    # Preserve an intentional terminal cwd. The entry point separately moves
    # Finder launches from `/` to a user workspace; never chdir into the
    # signed Contents/Resources directory.
    "no_chdir": True,
    "iconfile": str(APP_ICON),
    # Pillow is used only by the Textual logo renderer. Bundling it into the
    # native shell also introduces the wheel's private liblzma; py2app 0.28.10
    # rewrites that dylib's install name into an invalid __LINKEDIT layout and
    # then silently fails ad-hoc signing. The desktop UI renders its images in
    # WebKit and does not import Pillow.
    "excludes": ["PIL"],
    # Do not mutate already-signed Homebrew/wheel Mach-O files with ``strip``.
    # py2app re-signs relocated binaries after collection.
    "strip": False,
    # ``care.desktop`` and the generator/runtime adapters deliberately use
    # lazy imports.  Name the complete runtime boundary explicitly so
    # modulegraph cannot produce a bundle that opens but fails on its first
    # generation or execution request.
    "packages": [
        # AnyIO selects ``anyio._backends._asyncio`` dynamically at runtime;
        # modulegraph cannot infer that import from the MCP stdio server.
        "anyio",
        "care",
        "gigaevo_client",
        "LocalAuthentication",
        "mcp",
        "mmar_carl",
        "mmar_mage",
        "openai",
        "webview",
    ],
    # Copy static UI, the pinned single-file DSH runtime, and MAESTRO profile
    # integration files. The runtime carrier includes its closed Node
    # dependency tree, so the application does not need a system Node/dsh.
    # The Keychain provider is an inert, source-only resource: packaging it neither
    # builds its native addon nor activates it in a DeepSeek Harness profile.
    # py2app preserves each directory resource under its basename.
    "resources": [
        str(WEB_ASSETS_DIR),
        str(DEEPSEEK_INTEGRATION_DIR),
        str(DEEPSEEK_MAESTRO_PRESET_DIR),
        str(DEEPSEEK_MAESTRO_JOBS_DIR),
        str(DEEPSEEK_MAESTRO_SETTINGS_DIR),
        str(DEEPSEEK_MAESTRO_BRANDING_DIR),
        *DEEPSEEK_RUNTIME_RESOURCE_ENTRIES,
        *KEYCHAIN_RESOURCE_ENTRIES,
    ],
    "plist": PLIST,
}

SETUP_KWARGS = {
    "name": "Maestro",
    "version": VERSION,
    "app": [str(ENTRY_POINT)],
    # ``setup.py`` deliberately runs with ``packaging/macos`` as its working
    # directory so setuptools does not merge the repository's PEP 621 runtime
    # dependencies into this standalone py2app distribution. Keep the public
    # artifact paths rooted at the repository to match the documented command.
    "options": {
        "bdist": {
            "dist_dir": str(DIST_DIR),
            "bdist_base": str(BUILD_DIR),
        },
        "py2app": PY2APP_OPTIONS,
    },
    "setup_requires": ["py2app==0.28.10"],
}
