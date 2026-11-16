"""Supervise a DeepSeek Harness Web process for a native Maestro shell.

This module owns only the process boundary.  It deliberately does not import
``pywebview`` and does not replace :mod:`care.desktop`: a later composition can
hand :attr:`DeepSeekHarnessReady.launch_url` to the native window after the
MAESTRO profile and plugin bundle have been installed.

The Harness Web startup URL can carry an authentication credential (current
installed builds can also announce a credential-free root URL).  The reader
validates either form as an exact loopback URL, never writes a token-bearing
readiness line to a logger or diagnostic buffer, and keeps the URL out of
``repr`` output.
"""

from __future__ import annotations

import json
import os
import queue
import re
import secrets
import shutil
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
from collections import deque
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final, Literal
from urllib.parse import parse_qs, urlsplit

from care.runtime.credential_broker import (
    BROKER_BOOTSTRAP_FD_ENV,
    CARE_CREDENTIAL_ENV_NAMES,
    CredentialProvider,
    HarnessCredentialBrokerServer,
    encode_harness_credential_bootstrap,
)


LOOPBACK_HOST: Final = "127.0.0.1"
DEFAULT_PROFILE: Final = "maestro-care"
DEFAULT_STARTUP_TIMEOUT: Final = 45.0
DEFAULT_SHUTDOWN_TIMEOUT: Final = 5.0
PINNED_DSH_VERSION: Final = "0.1.2-alpha.3"
BUNDLED_DSH_RUNTIME_DIRECTORY: Final = "deepseek-harness-runtime"
BUNDLED_DSH_RUNTIME_EXECUTABLE: Final = "deepseek-harness-sdk-runtime-macos-arm64"
BUNDLED_DSH_RUNTIME_FILENAMES: Final = (
    BUNDLED_DSH_RUNTIME_EXECUTABLE,
    f"{BUNDLED_DSH_RUNTIME_EXECUTABLE}-rg",
    f"{BUNDLED_DSH_RUNTIME_EXECUTABLE}-spawn-helper",
)
DSH_EXECUTABLE_ENV: Final = "MAESTRO_DSH_EXECUTABLE"
DSH_SOURCE_ENV: Final = "MAESTRO_DSH_SOURCE"
DSH_HOME_ENV: Final = "DSH_HOME"
DSH_CLIENT_TITLE_ENV: Final = "DSH_CLIENT_TITLE"
DSH_TELEMETRY_DISABLED_ENV: Final = "DSH_TELEMETRY_DISABLED"
PKG_NATIVE_CACHE_PATH_ENV: Final = "PKG_NATIVE_CACHE_PATH"
MAESTRO_PYTHON_ENV: Final = "MAESTRO_CARE_PYTHON"
MAESTRO_PYTHONHOME_ENV: Final = "MAESTRO_CARE_PYTHONHOME"
MAESTRO_CWD_ENV: Final = "MAESTRO_CARE_CWD"
MAESTRO_WORKSPACE_ROOT_ENV: Final = "MAESTRO_HARNESS_WORKSPACE_ROOT"
MAESTRO_SESSION_ID_ENV: Final = "MAESTRO_HARNESS_SESSION_ID"
MAESTRO_ARTIFACT_ROOT_ENV: Final = "MAESTRO_HARNESS_ARTIFACT_ROOT"
MAESTRO_MEMORY_ENABLED_ENV: Final = "MAESTRO_HARNESS_MEMORY_ENABLED"
MAESTRO_GENERATION_ENABLED_ENV: Final = "MAESTRO_HARNESS_GENERATION_ENABLED"
MAESTRO_EXECUTION_ENABLED_ENV: Final = "MAESTRO_HARNESS_EXECUTION_ENABLED"
MAESTRO_MEMORY_WRITE_ENABLED_ENV: Final = "MAESTRO_HARNESS_MEMORY_WRITE_ENABLED"
MAESTRO_EVOLUTION_ENABLED_ENV: Final = "MAESTRO_HARNESS_EVOLUTION_ENABLED"
DEFAULT_DSH_HOME: Final = Path("~/.config/care/deepseek-harness").expanduser()
_MAESTRO_PROFILE_BUNDLES: Final = (
    "@deepseek-ai/dsh-base",
    "@deepseek-ai/dsh-web-app",
)
_MAESTRO_PROFILE_MANIFEST = {
    "name": "dsh-profile-maestro-care",
    "private": True,
    "dependencies": {},
    "dsh": {
        "profile": {
            "bundles": list(_MAESTRO_PROFILE_BUNDLES),
            "patchReload": "startup",
        }
    },
}
_MAESTRO_PROFILE_WORKSPACE = (
    "packages:\n"
    "  - .\n\n"
    "nodeLinker: hoisted\n"
    "autoInstallPeers: false\n"
)

_PROFILE_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_SESSION_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
_READY_PATTERN = re.compile(r"^dsh web:\s+(https?://\S+)")
_ANSI_ESCAPE_PATTERN = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_QUERY_TOKEN_PATTERN = re.compile(
    r"([?&]token=)([^&#\s)]+)",
    flags=re.IGNORECASE,
)
_TOKEN_FIELD_PATTERN = re.compile(
    r"(\btoken\s*[:=]\s*)([^\s,;&)]+)",
    flags=re.IGNORECASE,
)
_CREDENTIAL_ENV_NAME_PATTERN = re.compile(
    r"(?:api_?key|authorization|credential|password|passwd|secret|token)",
    flags=re.IGNORECASE,
)
_BOOTSTRAP_TOKEN_PATTERN = re.compile(r"^[A-Za-z0-9_-]{16,256}$")
_VERSION_PATTERN = re.compile(r"^[0-9A-Za-z][0-9A-Za-z.+-]{0,63}$")
_REDACTED = "<redacted>"
_DIAGNOSTIC_LINES = 128
_DIAGNOSTIC_LINE_CHARS = 500


def _scrub_harness_environment(source: Mapping[str, str]) -> dict[str, str]:
    """Keep ordinary process coordinates while dropping credential-shaped vars."""

    return {
        name: value
        for name, value in source.items()
        if _CREDENTIAL_ENV_NAME_PATTERN.search(name) is None
    }


class DeepSeekHarnessError(RuntimeError):
    """Base class for actionable Harness-host failures."""


class DeepSeekHarnessDiscoveryError(DeepSeekHarnessError):
    """Raised when neither an installed command nor a usable checkout exists."""


class DeepSeekHarnessLaunchError(DeepSeekHarnessError):
    """Raised when the child cannot reach valid Web readiness."""


class DeepSeekHarnessProtocolError(DeepSeekHarnessLaunchError):
    """Raised when a readiness line violates the loopback credential boundary."""


class DeepSeekHarnessCancelled(DeepSeekHarnessLaunchError):
    """Raised when startup is cancelled before readiness."""


class DeepSeekHarnessStateError(DeepSeekHarnessError):
    """Raised for an invalid supervisor lifecycle transition."""


@dataclass(frozen=True, slots=True)
class DeepSeekHarnessCommand:
    """A shell-free command prefix for one resolved Harness installation."""

    argv: tuple[str, ...]
    provenance: Literal[
        "bundled-runtime",
        "installed",
        "source-built",
        "source-typescript",
    ]
    source_root: Path | None = None
    observed_version: str | None = None

    def __post_init__(self) -> None:
        if not self.argv or any(
            not isinstance(part, str) or not part for part in self.argv
        ):
            raise ValueError(
                "DeepSeek Harness command argv must contain non-empty strings"
            )
        if self.observed_version is not None and not _VERSION_PATTERN.fullmatch(
            self.observed_version
        ):
            raise ValueError("observed DeepSeek Harness version is invalid")


@dataclass(frozen=True, slots=True)
class DeepSeekHarnessVersionStatus:
    """Observed CLI version and the pinned bundle-compatibility verdict."""

    expected: str
    observed: str | None
    compatible: bool
    provenance: str
    error: str | None = None


@dataclass(frozen=True, slots=True)
class MaestroHarnessIntegration:
    """Absolute files injected into the stock Web profile for MAESTRO."""

    directory: Path
    direct_patch_template: Path
    policy_module: Path
    preset_root_module: Path
    jobs_module: Path
    settings_module: Path
    branding_module: Path


@dataclass(frozen=True, slots=True)
class DeepSeekHarnessReady:
    """Validated loopback endpoint returned by a running Harness.

    ``launch_url`` is intentionally excluded from dataclass representations:
    depending on the installed build it can contain a process-scoped bootstrap
    token required by the Web UI.
    """

    origin: str
    port: int
    authenticated: bool
    launch_url: str = field(repr=False)

    def __repr__(self) -> str:
        return (
            "DeepSeekHarnessReady("
            f"origin={self.origin!r}, port={self.port!r}, "
            f"authenticated={self.authenticated!r})"
        )

    __str__ = __repr__


@dataclass(frozen=True, slots=True)
class _ReaderFailure:
    error: BaseException


_READER_EOF = object()


def _resolve_executable(
    value: str | os.PathLike[str],
    *,
    which: Callable[[str], str | None],
) -> str:
    raw = os.fspath(value).strip()
    if not raw:
        raise DeepSeekHarnessDiscoveryError("DeepSeek Harness executable is empty")
    if Path(raw).name != raw or Path(raw).is_absolute():
        candidate = Path(raw).expanduser().resolve()
        if not candidate.is_file() or not os.access(candidate, os.X_OK):
            raise DeepSeekHarnessDiscoveryError(
                f"DeepSeek Harness executable is unavailable: {candidate}",
            )
        return str(candidate)
    resolved = which(raw)
    if resolved is None:
        raise DeepSeekHarnessDiscoveryError(
            f"DeepSeek Harness executable `{raw}` was not found on PATH",
        )
    return str(Path(resolved).expanduser().resolve())


def _load_source_manifest(root: Path) -> bool:
    manifest = root / "package.json"
    try:
        payload = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return False
    return isinstance(payload, dict) and payload.get("name") == "@deepseek-ai/dsh-root"


def _source_command(
    source_root: str | os.PathLike[str],
    *,
    which: Callable[[str], str | None],
) -> DeepSeekHarnessCommand:
    root = Path(source_root).expanduser().resolve()
    if not root.is_dir() or not _load_source_manifest(root):
        raise DeepSeekHarnessDiscoveryError(
            f"not a DeepSeek Harness source checkout: {root}",
        )

    node = which("node")
    if node is None:
        raise DeepSeekHarnessDiscoveryError(
            "Node.js is required to run a DeepSeek Harness source checkout",
        )
    node = str(Path(node).expanduser().resolve())

    # Web startup resolves the built frontend package at runtime.  Detect this
    # before spawning so a checkout that has only been cloned fails with an
    # actionable build hint rather than timing out in the native shell.
    frontend = root / "apps" / "web" / "dist" / "index.html"
    if not frontend.is_file():
        raise DeepSeekHarnessDiscoveryError(
            f"DeepSeek Harness Web assets are not built under {root}; "
            "run `pnpm run build` in that checkout",
        )

    built_entry = root / "apps" / "cli" / "lib" / "bin.js"
    if built_entry.is_file():
        return DeepSeekHarnessCommand(
            argv=(node, str(built_entry)),
            provenance="source-built",
            source_root=root,
        )

    source_entry = root / "apps" / "cli" / "src" / "bin.ts"
    tsx_loader = root / "node_modules" / "tsx" / "dist" / "esm" / "index.mjs"
    if source_entry.is_file() and tsx_loader.is_file():
        return DeepSeekHarnessCommand(
            argv=(node, "--import", str(tsx_loader), str(source_entry)),
            provenance="source-typescript",
            source_root=root,
        )
    raise DeepSeekHarnessDiscoveryError(
        f"DeepSeek Harness CLI is not built under {root}; run `pnpm install` "
        "and `pnpm run build` in that checkout",
    )


def _default_sibling_checkout() -> Path:
    # runtime/deepseek_host.py -> care package -> CARE repository -> workspace.
    return _source_care_repository().parent / "deepseek-harness"


def _bundled_runtime_command(
    environ: Mapping[str, str],
) -> DeepSeekHarnessCommand | None:
    """Resolve the sealed runtime embedded in a py2app Resources directory.

    Absence is not an error because source launches have no ``RESOURCEPATH``
    runtime.  Once the runtime directory exists, however, it is authoritative:
    falling back to a sibling checkout or a PATH binary after detecting a
    partial or unsafe app bundle would silently cross a release trust boundary.
    """

    resource_path = environ.get("RESOURCEPATH", "").strip()
    if not resource_path:
        return None
    runtime_root = Path(
        os.path.abspath(
            os.fspath(Path(resource_path).expanduser() / BUNDLED_DSH_RUNTIME_DIRECTORY)
        )
    )
    try:
        root_stat = runtime_root.lstat()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise DeepSeekHarnessDiscoveryError(
            f"could not inspect bundled DeepSeek Harness runtime: {runtime_root}",
        ) from exc
    if not stat.S_ISDIR(root_stat.st_mode):
        raise DeepSeekHarnessDiscoveryError(
            f"bundled DeepSeek Harness runtime is not a normal directory: "
            f"{runtime_root}",
        )

    try:
        observed_names = {entry.name for entry in runtime_root.iterdir()}
    except OSError as exc:
        raise DeepSeekHarnessDiscoveryError(
            f"could not inspect bundled DeepSeek Harness runtime: {runtime_root}",
        ) from exc
    runtime_executable = BUNDLED_DSH_RUNTIME_EXECUTABLE
    runtime_filenames = BUNDLED_DSH_RUNTIME_FILENAMES
    manifest = Path(resource_path) / "desktop-runtime.json"
    if manifest.is_file():
        try:
            metadata = json.loads(manifest.read_text(encoding="utf-8"))
            runtime_executable = metadata["executable"]
            runtime_filenames = tuple(metadata["files"])
            admitted = {
                f"deepseek-harness-sdk-runtime-{target}-{arch}{suffix}"
                for target in ("macos", "linux", "win")
                for arch in ("arm64", "x64")
                for suffix in ((".exe",) if target == "win" else ("",))
            }
            expected_files = (
                (runtime_executable, runtime_executable[:-4] + "-rg.exe")
                if runtime_executable.endswith(".exe")
                else (runtime_executable, runtime_executable + "-rg")
            )
            if "-macos-" in runtime_executable:
                expected_files += (runtime_executable + "-spawn-helper",)
            if runtime_executable not in admitted or runtime_filenames != expected_files:
                raise ValueError("invalid desktop runtime manifest")
        except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
            raise DeepSeekHarnessDiscoveryError(
                "invalid bundled DeepSeek Harness runtime manifest",
            ) from exc
    expected_names = set(runtime_filenames)
    if observed_names != expected_names:
        raise DeepSeekHarnessDiscoveryError(
            "bundled DeepSeek Harness runtime is incomplete or contains "
            f"unexpected files: {runtime_root}",
        )

    for filename in runtime_filenames:
        candidate = runtime_root / filename
        try:
            candidate_stat = candidate.lstat()
        except OSError as exc:
            raise DeepSeekHarnessDiscoveryError(
                f"bundled DeepSeek Harness runtime is incomplete: {runtime_root}",
            ) from exc
        if (
            not stat.S_ISREG(candidate_stat.st_mode)
            or (os.name != "nt" and candidate_stat.st_mode & 0o111 == 0)
            or not os.access(candidate, os.X_OK)
        ):
            raise DeepSeekHarnessDiscoveryError(
                "bundled DeepSeek Harness runtime contains an unsafe or "
                f"non-executable file: {candidate}",
            )

    return DeepSeekHarnessCommand(
        argv=(str(runtime_root / runtime_executable),),
        provenance="bundled-runtime",
    )


def _source_care_repository() -> Path:
    return Path(__file__).resolve().parents[2]


def _default_care_python() -> tuple[Path, Path | None]:
    """Select the trusted interpreter used by the resident MCP subprocess.

    A source/venv launch can reuse ``sys.executable``.  A py2app process cannot:
    its executable is the GUI launcher, while py2app ships a separate Python
    helper beside it.  The helper needs the bundle Resources directory as
    ``PYTHONHOME`` so it imports the packaged dependency closure rather than a
    user workspace or the machine's global site-packages.
    """

    executable = Path(sys.executable).expanduser()
    if not bool(getattr(sys, "frozen", False)):
        return executable, None

    if hasattr(sys, "_MEIPASS"):
        helper = executable.with_name(
            "maestro-python.exe" if os.name == "nt" else "maestro-python"
        )
        if not helper.is_file():
            raise DeepSeekHarnessDiscoveryError("bundled resident MCP helper is unavailable")
        return helper, None

    helper = executable.with_name("python")
    resources = executable.parent.parent / "Resources"
    if not helper.is_file() or not (resources / "lib").is_dir():
        raise DeepSeekHarnessDiscoveryError(
            "the Maestro.app bundled Python helper is unavailable",
        )
    return helper, resources


def _integration_from_directory(directory: Path) -> MaestroHarnessIntegration | None:
    resolved = directory.expanduser().resolve()
    direct_patch_template = resolved / "cordis.direct.patch.yml.in"
    policy_module = resolved / "policy.mjs"
    preset_root_module = (
        resolved.parent
        / "deepseek-harness-maestro-preset"
        / "preset-root.mjs"
    )
    jobs_module = (
        resolved.parent
        / "deepseek-harness-maestro-jobs"
        / "index.mjs"
    )
    settings_module = (
        resolved.parent
        / "deepseek-harness-maestro-settings"
        / "index.mjs"
    )
    branding_module = (
        resolved.parent
        / "deepseek-harness-maestro-branding"
        / "index.mjs"
    )
    if (
        not direct_patch_template.is_file()
        or not policy_module.is_file()
        or not preset_root_module.is_file()
        or not jobs_module.is_file()
        or not settings_module.is_file()
        or not branding_module.is_file()
    ):
        return None
    return MaestroHarnessIntegration(
        directory=resolved,
        direct_patch_template=direct_patch_template,
        policy_module=policy_module,
        preset_root_module=preset_root_module,
        jobs_module=jobs_module,
        settings_module=settings_module,
        branding_module=branding_module,
    )


def resolve_maestro_harness_integration(
    *,
    integration_dir: str | os.PathLike[str] | None = None,
    environ: Mapping[str, str] | None = None,
) -> MaestroHarnessIntegration:
    """Resolve MAESTRO's DSH patch and policy from source or app resources."""

    if integration_dir is not None:
        requested = Path(integration_dir)
        integration = _integration_from_directory(requested)
        if integration is None:
            raise DeepSeekHarnessDiscoveryError(
                f"MAESTRO DeepSeek Harness integration is incomplete: "
                f"{requested.expanduser().resolve()}",
            )
        return integration

    env = os.environ if environ is None else environ
    candidates: list[Path] = []
    resource_path = env.get("RESOURCEPATH", "").strip()
    if resource_path:
        # py2app copies a directory resource under its basename.
        candidates.append(Path(resource_path) / "deepseek-harness")
    repository = _source_care_repository()
    candidates.extend(
        (
            repository / "integrations" / "deepseek-harness",
            Path(__file__).resolve().parents[1] / "integrations" / "deepseek-harness",
        )
    )
    for candidate in candidates:
        integration = _integration_from_directory(candidate)
        if integration is not None:
            return integration
    raise DeepSeekHarnessDiscoveryError(
        "MAESTRO DeepSeek Harness patch/policy files were not found in the "
        "CARE checkout or application resources",
    )


def discover_deepseek_harness(
    *,
    executable: str | os.PathLike[str] | None = None,
    source_root: str | os.PathLike[str] | None = None,
    environ: Mapping[str, str] | None = None,
    which: Callable[[str], str | None] = shutil.which,
) -> DeepSeekHarnessCommand:
    """Resolve an installed ``dsh`` command or a prepared local checkout.

    Explicit function arguments are authoritative and fail loudly.  Without
    them, MAESTRO-specific environment overrides are tried first, followed by
    the runtime sealed into ``Maestro.app``, a prepared sibling checkout, and
    finally ``dsh`` on ``PATH``.  Values are executable paths or source
    directories, never shell command strings.  The host performs a separate
    pinned-version probe before it starts Web.
    """

    env = os.environ if environ is None else environ
    explicit_executable = executable
    if explicit_executable is None:
        value = env.get(DSH_EXECUTABLE_ENV, "").strip()
        explicit_executable = value or None
    if explicit_executable is not None:
        return DeepSeekHarnessCommand(
            argv=(_resolve_executable(explicit_executable, which=which),),
            provenance="installed",
        )

    explicit_source = source_root
    if explicit_source is None:
        value = env.get(DSH_SOURCE_ENV, "").strip()
        explicit_source = value or None
    if explicit_source is not None:
        return _source_command(explicit_source, which=which)

    bundled_runtime = _bundled_runtime_command(env)
    if bundled_runtime is not None:
        return bundled_runtime

    sibling = _default_sibling_checkout()
    sibling_error: DeepSeekHarnessDiscoveryError | None = None
    if sibling.is_dir():
        try:
            return _source_command(sibling, which=which)
        except DeepSeekHarnessDiscoveryError as exc:
            sibling_error = exc
    installed = which("dsh")
    if installed is not None:
        return DeepSeekHarnessCommand(
            argv=(str(Path(installed).expanduser().resolve()),),
            provenance="installed",
        )
    if sibling_error is not None:
        raise sibling_error
    raise DeepSeekHarnessDiscoveryError(
        "DeepSeek Harness was not found; install `dsh`, set "
        f"{DSH_EXECUTABLE_ENV}, or point {DSH_SOURCE_ENV} at a built checkout",
    )


def probe_deepseek_harness_version(
    command: DeepSeekHarnessCommand,
    *,
    runner: Callable[..., Any] = subprocess.run,
    timeout: float = 5.0,
    environ: Mapping[str, str] | None = None,
    cwd: str | os.PathLike[str] | None = None,
) -> DeepSeekHarnessVersionStatus:
    """Read ``dsh -V`` without starting a profile or accepting model work."""

    if command.observed_version is not None:
        observed = command.observed_version
        return DeepSeekHarnessVersionStatus(
            expected=PINNED_DSH_VERSION,
            observed=observed,
            compatible=observed == PINNED_DSH_VERSION,
            provenance=command.provenance,
        )
    try:
        completed = runner(
            [*command.argv, "-V"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
            env=None if environ is None else dict(environ),
            cwd=None if cwd is None else os.fspath(cwd),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return DeepSeekHarnessVersionStatus(
            expected=PINNED_DSH_VERSION,
            observed=None,
            compatible=False,
            provenance=command.provenance,
            error=f"version probe failed: {type(exc).__name__}",
        )
    output = str(getattr(completed, "stdout", "")).strip()
    first_line = output.splitlines()[0].strip() if output else ""
    observed = first_line if _VERSION_PATTERN.fullmatch(first_line) else None
    return DeepSeekHarnessVersionStatus(
        expected=PINNED_DSH_VERSION,
        observed=observed,
        compatible=(
            getattr(completed, "returncode", 1) == 0 and observed == PINNED_DSH_VERSION
        ),
        provenance=command.provenance,
        error=(
            None
            if observed is not None and getattr(completed, "returncode", 1) == 0
            else "version probe returned no valid version"
        ),
    )


def build_deepseek_harness_argv(
    command: DeepSeekHarnessCommand,
    *,
    profile: str = DEFAULT_PROFILE,
    patch_file: str | os.PathLike[str] | None = None,
) -> tuple[str, ...]:
    """Build the fixed safe Web invocation owned by the native shell."""

    if not _PROFILE_PATTERN.fullmatch(profile):
        raise ValueError(
            "profile must be 1-64 ASCII letters, digits, dots, underscores, or hyphens",
        )
    selector = ("web",) if profile == "web" else ("--profile", profile)
    patch = () if patch_file is None else ("--patch", str(Path(patch_file).resolve()))
    return (
        *command.argv,
        *selector,
        *patch,
        "--no-open",
        "--host",
        LOOPBACK_HOST,
        "--port",
        "0",
    )


def redact_deepseek_harness_output(line: str) -> str:
    """Remove bootstrap tokens from bounded child-process diagnostics."""

    cleaned = _ANSI_ESCAPE_PATTERN.sub("", str(line)).replace("\x00", "")
    cleaned = _QUERY_TOKEN_PATTERN.sub(rf"\1{_REDACTED}", cleaned)
    cleaned = _TOKEN_FIELD_PATTERN.sub(rf"\1{_REDACTED}", cleaned)
    return cleaned.strip()[:_DIAGNOSTIC_LINE_CHARS]


def parse_deepseek_harness_readiness(line: str) -> DeepSeekHarnessReady | None:
    """Parse one ``dsh web:`` line under a strict loopback/token contract.

    Ordinary output and the separate browser-opening notice return ``None``.
    A URL-shaped readiness line that violates the expected security boundary
    raises without including the credential-bearing URL in its message.
    """

    normalized = _ANSI_ESCAPE_PATTERN.sub("", str(line)).strip()
    match = _READY_PATTERN.match(normalized)
    if match is None:
        return None
    launch_url = match.group(1)
    try:
        parsed = urlsplit(launch_url)
        port = parsed.port
    except ValueError as exc:
        raise DeepSeekHarnessProtocolError(
            "DeepSeek Harness emitted an invalid readiness URL",
        ) from exc
    if (
        parsed.scheme != "http"
        or parsed.hostname != LOOPBACK_HOST
        or parsed.username is not None
        or parsed.password is not None
        or port is None
        or not 1 <= port <= 65_535
        or parsed.path not in {"", "/"}
        or parsed.fragment
    ):
        raise DeepSeekHarnessProtocolError(
            "DeepSeek Harness readiness URL must be a loopback root URL",
        )
    try:
        query = parse_qs(
            parsed.query,
            keep_blank_values=True,
            strict_parsing=True,
        )
    except ValueError as exc:
        raise DeepSeekHarnessProtocolError(
            "DeepSeek Harness readiness URL has an invalid query",
        ) from exc
    authenticated = False
    if query:
        tokens = query.get("token", [])
        if (
            set(query) != {"token"}
            or len(tokens) != 1
            or _BOOTSTRAP_TOKEN_PATTERN.fullmatch(tokens[0]) is None
        ):
            raise DeepSeekHarnessProtocolError(
                "DeepSeek Harness readiness URL has an invalid bootstrap token",
            )
        authenticated = True
    origin = f"http://{LOOPBACK_HOST}:{port}"
    return DeepSeekHarnessReady(
        origin=origin,
        port=port,
        authenticated=authenticated,
        launch_url=launch_url,
    )


def _diagnostic_suffix(lines: Iterable[str]) -> str:
    rows = [row for row in lines if row]
    if not rows:
        return ""
    return "\nRecent DeepSeek Harness output (credentials redacted):\n" + "\n".join(
        rows
    )


def _write_private_profile_file(path: Path, content: str) -> None:
    """Create one MAESTRO-owned profile file without a permissive umask gap."""

    descriptor: int | None = None
    try:
        descriptor = os.open(
            path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            descriptor = None
            stream.write(content)
            stream.flush()
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _prepare_maestro_profile(dsh_home: Path) -> Path:
    """Create or validate the native startup-frozen Web profile.

    The stock ``web`` profile watches user patch layers.  A native app does
    not need live profile recomposition, and on macOS those Chokidar handles
    can exhaust the GUI process's watcher budget after readiness.  MAESTRO
    therefore owns a dedicated base+web profile with ``patchReload=startup``.
    Additional installed plugin bundles are allowed after the two required
    base layers, so ``dsh plugin --profile maestro-care add ...`` remains a
    supported extension path.
    """

    profile_dir = dsh_home / "profiles" / DEFAULT_PROFILE
    if profile_dir.exists() and profile_dir.is_symlink():
        raise DeepSeekHarnessLaunchError(
            "the MAESTRO DeepSeek Harness profile directory must not be a symlink"
        )
    try:
        profile_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(profile_dir, 0o700)
    except OSError as exc:
        raise DeepSeekHarnessLaunchError(
            "could not initialize the private MAESTRO DeepSeek Harness profile"
        ) from exc

    manifest_path = profile_dir / "package.json"
    if manifest_path.is_symlink() or (
        manifest_path.exists() and not manifest_path.is_file()
    ):
        raise DeepSeekHarnessLaunchError(
            "the MAESTRO DeepSeek Harness profile manifest is unsafe"
        )
    if not manifest_path.exists():
        try:
            _write_private_profile_file(
                manifest_path,
                json.dumps(_MAESTRO_PROFILE_MANIFEST, indent=2) + "\n",
            )
        except FileExistsError:
            pass
        except OSError as exc:
            raise DeepSeekHarnessLaunchError(
                "could not create the MAESTRO DeepSeek Harness profile manifest"
            ) from exc

    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        profile = manifest["dsh"]["profile"]
        bundles = profile["bundles"]
        compatible = (
            manifest.get("name") == "dsh-profile-maestro-care"
            and manifest.get("private") is True
            and isinstance(manifest.get("dependencies"), dict)
            and isinstance(bundles, list)
            and bundles[: len(_MAESTRO_PROFILE_BUNDLES)]
            == list(_MAESTRO_PROFILE_BUNDLES)
            and all(isinstance(item, str) and item for item in bundles)
            and profile.get("patchReload") == "startup"
        )
    except (OSError, UnicodeError, json.JSONDecodeError, KeyError, TypeError):
        compatible = False
    if not compatible:
        raise DeepSeekHarnessLaunchError(
            "the existing MAESTRO DeepSeek Harness profile is incompatible"
        )

    for name, content in (
        ("cordis.patch.yml", "[]\n"),
        ("pnpm-workspace.yaml", _MAESTRO_PROFILE_WORKSPACE),
    ):
        target = profile_dir / name
        if target.is_symlink() or (target.exists() and not target.is_file()):
            raise DeepSeekHarnessLaunchError(
                "the MAESTRO DeepSeek Harness profile contains an unsafe file"
            )
        if not target.exists():
            try:
                _write_private_profile_file(target, content)
            except FileExistsError:
                pass
            except OSError as exc:
                raise DeepSeekHarnessLaunchError(
                    "could not complete the MAESTRO DeepSeek Harness profile"
                ) from exc
    return profile_dir


class DeepSeekHarnessHost:
    """One-shot process supervisor for the native Harness Web backend."""

    def __init__(
        self,
        *,
        profile: str = DEFAULT_PROFILE,
        command: DeepSeekHarnessCommand | None = None,
        executable: str | os.PathLike[str] | None = None,
        source_root: str | os.PathLike[str] | None = None,
        workspace: str | os.PathLike[str] | None = None,
        dsh_home: str | os.PathLike[str] = DEFAULT_DSH_HOME,
        maestro_integration: bool = True,
        integration_dir: str | os.PathLike[str] | None = None,
        care_python: str | os.PathLike[str] | None = None,
        care_cwd: str | os.PathLike[str] | None = None,
        session_id: str | None = None,
        artifact_root: str | os.PathLike[str] | None = None,
        memory_enabled: bool = True,
        generation_enabled: bool = True,
        execution_enabled: bool = True,
        memory_write_enabled: bool | None = None,
        evolution_enabled: bool = True,
        credential_provider: CredentialProvider | None = None,
        session_id_factory: Callable[[], str] | None = None,
        environ: Mapping[str, str] | None = None,
        startup_timeout: float = DEFAULT_STARTUP_TIMEOUT,
        shutdown_timeout: float = DEFAULT_SHUTDOWN_TIMEOUT,
        require_pinned_version: bool = True,
        version_probe: (
            Callable[[DeepSeekHarnessCommand], DeepSeekHarnessVersionStatus] | None
        ) = None,
        popen: Callable[..., Any] = subprocess.Popen,
        which: Callable[[str], str | None] = shutil.which,
    ) -> None:
        if startup_timeout <= 0:
            raise ValueError("startup_timeout must be positive")
        if shutdown_timeout <= 0:
            raise ValueError("shutdown_timeout must be positive")
        # Validate before any process can be spawned.
        if not _PROFILE_PATTERN.fullmatch(profile):
            raise ValueError(
                "profile must be 1-64 ASCII letters, digits, dots, underscores, or hyphens",
            )
        self._profile = profile
        self._command = command
        self._executable = executable
        self._source_root = source_root
        self._workspace = (
            Path.cwd().resolve()
            if workspace is None
            else Path(workspace).expanduser().resolve()
        )
        if not self._workspace.is_dir():
            raise ValueError(f"workspace is not a directory: {self._workspace}")
        self._dsh_home = Path(dsh_home).expanduser().resolve()
        if self._dsh_home == Path(self._dsh_home.anchor):
            raise ValueError("dsh_home must be a normal user-data directory")
        self._native_cache = self._dsh_home / "native-cache"
        if self._native_cache.exists() and self._native_cache.is_symlink():
            raise ValueError("DeepSeek Harness native cache must not be a symlink")
        self._maestro_integration = bool(maestro_integration)
        self._integration_dir = integration_dir
        default_care_cwd = _source_care_repository()
        if not (default_care_cwd / "pyproject.toml").is_file():
            default_care_cwd = self._workspace
        # Do not resolve this path through symlinks: virtual environments use
        # ``bin/python`` symlinks, and executing the base interpreter target
        # directly drops the venv's site-packages. ``abspath`` normalizes a
        # relative override while preserving the interpreter entry point.
        if care_python is None:
            selected_care_python, default_python_home = _default_care_python()
        else:
            selected_care_python = Path(care_python).expanduser()
            default_python_home = None
        self._care_python = Path(os.path.abspath(os.fspath(selected_care_python)))
        self._care_python_home = default_python_home if care_python is None else None
        self._care_cwd = (
            Path(default_care_cwd if care_cwd is None else care_cwd)
            .expanduser()
            .resolve()
        )
        if not isinstance(memory_enabled, bool):
            raise ValueError("memory_enabled must be a boolean")
        if not isinstance(generation_enabled, bool):
            raise ValueError("generation_enabled must be a boolean")
        if not isinstance(execution_enabled, bool):
            raise ValueError("execution_enabled must be a boolean")
        if memory_write_enabled is None:
            memory_write_enabled = memory_enabled
        elif not isinstance(memory_write_enabled, bool):
            raise ValueError("memory_write_enabled must be a boolean or None")
        if memory_write_enabled and not memory_enabled:
            raise ValueError("memory_write_enabled requires memory_enabled")
        if not isinstance(evolution_enabled, bool):
            raise ValueError("evolution_enabled must be a boolean")
        self._memory_enabled = memory_enabled
        self._generation_enabled = generation_enabled
        self._execution_enabled = execution_enabled
        self._memory_write_enabled = memory_write_enabled
        self._evolution_enabled = evolution_enabled
        self._credential_provider = credential_provider
        make_session_id = session_id_factory or (
            lambda: f"msh_{secrets.token_urlsafe(24)}"
        )
        resolved_session_id = (
            session_id if session_id is not None else make_session_id()
        )
        if (
            not isinstance(resolved_session_id, str)
            or _SESSION_ID_PATTERN.fullmatch(resolved_session_id) is None
        ):
            raise ValueError(
                "session_id must contain 1-128 letters, digits, underscores, or hyphens"
            )
        self._session_id = resolved_session_id
        artifact_candidate = (
            self._dsh_home / "artifacts"
            if artifact_root is None
            else Path(artifact_root).expanduser()
        )
        if artifact_candidate.exists() and artifact_candidate.is_symlink():
            raise ValueError("artifact_root must not be a symlink")
        self._artifact_root = artifact_candidate.resolve()
        if self._artifact_root == Path(self._artifact_root.anchor):
            raise ValueError("artifact_root must be a normal private directory")
        self._environment = _scrub_harness_environment(
            os.environ if environ is None else environ
        )
        if self._credential_provider is not None:
            # Literal credentials loaded from the checkout's .env must not be
            # inherited by DeepSeek Harness or arbitrary plugin processes. The
            # private broker is the only CARE credential boundary in the
            # resident application.
            for name in CARE_CREDENTIAL_ENV_NAMES:
                self._environment.pop(name, None)
        # The native host must never attach to an unrelated global profile.
        self._environment[DSH_HOME_ENV] = str(self._dsh_home)
        self._environment[DSH_CLIENT_TITLE_ENV] = "MAESTRO"
        self._environment[DSH_TELEMETRY_DISABLED_ENV] = "1"
        # SEA-packaged native modules are extracted at runtime.  Keep that
        # executable content in the same private, host-owned tree as the DSH
        # profile instead of inheriting an arbitrary cache path or falling back
        # to the user's shared ~/.cache directory.
        self._environment[PKG_NATIVE_CACHE_PATH_ENV] = str(self._native_cache)
        if self._maestro_integration:
            self._environment[MAESTRO_PYTHON_ENV] = str(self._care_python)
            if self._care_python_home is None:
                self._environment.pop(MAESTRO_PYTHONHOME_ENV, None)
            else:
                self._environment[MAESTRO_PYTHONHOME_ENV] = str(
                    self._care_python_home
                )
            self._environment[MAESTRO_CWD_ENV] = str(self._care_cwd)
            self._environment[MAESTRO_WORKSPACE_ROOT_ENV] = str(self._workspace)
            self._environment[MAESTRO_SESSION_ID_ENV] = self._session_id
            self._environment[MAESTRO_ARTIFACT_ROOT_ENV] = str(self._artifact_root)
            self._environment[MAESTRO_MEMORY_ENABLED_ENV] = (
                "true" if self._memory_enabled else "false"
            )
            self._environment[MAESTRO_GENERATION_ENABLED_ENV] = (
                "true" if self._generation_enabled else "false"
            )
            self._environment[MAESTRO_EXECUTION_ENABLED_ENV] = (
                "true" if self._execution_enabled else "false"
            )
            self._environment[MAESTRO_MEMORY_WRITE_ENABLED_ENV] = (
                "true" if self._memory_write_enabled else "false"
            )
            self._environment[MAESTRO_EVOLUTION_ENABLED_ENV] = (
                "true" if self._evolution_enabled else "false"
            )
        self._startup_timeout = float(startup_timeout)
        self._shutdown_timeout = float(shutdown_timeout)
        self._require_pinned_version = bool(require_pinned_version)
        self._version_probe = version_probe
        self._popen = popen
        self._which = which

        self._lock = threading.RLock()
        self._cancel_event = threading.Event()
        self._events: queue.Queue[object] = queue.Queue()
        self._diagnostics: deque[str] = deque(maxlen=_DIAGNOSTIC_LINES)
        self._process: Any | None = None
        self._reader: threading.Thread | None = None
        self._ready: DeepSeekHarnessReady | None = None
        self._version_status: DeepSeekHarnessVersionStatus | None = None
        self._patch_tempdir: tempfile.TemporaryDirectory[str] | None = None
        self._credential_broker: HarnessCredentialBrokerServer | None = None
        self._credential_channel: socket.socket | None = None
        self._credential_reader: threading.Thread | None = None
        self._starting = False
        self._closed = False

    @property
    def ready(self) -> DeepSeekHarnessReady | None:
        with self._lock:
            return self._ready

    @property
    def session_id(self) -> str:
        return self._session_id

    @property
    def artifact_root(self) -> Path:
        return self._artifact_root

    @property
    def is_running(self) -> bool:
        with self._lock:
            process = self._process
        return process is not None and process.poll() is None

    @property
    def diagnostics(self) -> tuple[str, ...]:
        """Return only bounded, token-redacted diagnostic lines."""

        with self._lock:
            return tuple(self._diagnostics)

    @property
    def version_status(self) -> DeepSeekHarnessVersionStatus | None:
        with self._lock:
            return self._version_status

    def _resolve_command(self) -> DeepSeekHarnessCommand:
        if self._command is not None:
            return self._command
        return discover_deepseek_harness(
            executable=self._executable,
            source_root=self._source_root,
            environ=self._environment,
            which=self._which,
        )

    def _materialize_integration_patch(
        self,
        integration: MaestroHarnessIntegration,
    ) -> Path:
        from care.runtime.deepseek_bundle import (
            DeepSeekBundleMaterializationError,
            materialize_direct_patch,
        )

        try:
            template = integration.direct_patch_template.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            raise DeepSeekHarnessLaunchError(
                "could not read the MAESTRO DeepSeek Harness patch template",
            ) from exc
        try:
            rendered = materialize_direct_patch(
                template,
                policy_module=integration.policy_module,
                preset_root_module=integration.preset_root_module,
                jobs_module=integration.jobs_module,
                settings_module=integration.settings_module,
                branding_module=integration.branding_module,
            )
        except DeepSeekBundleMaterializationError as exc:
            raise DeepSeekHarnessLaunchError(
                "MAESTRO DeepSeek Harness patch template is invalid",
            ) from exc
        temporary = tempfile.TemporaryDirectory(prefix="maestro-dsh-patch-")
        patch_file = Path(temporary.name) / "cordis.patch.yml"
        descriptor: int | None = None
        try:
            descriptor = os.open(
                patch_file,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
            )
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                descriptor = None
                stream.write(rendered)
                stream.flush()
            # Windows reports synthetic POSIX bits; the non-secret patch
            # inherits the current user's temporary-directory ACL instead.
            if os.name != "nt" and patch_file.stat().st_mode & 0o077:
                raise OSError("materialized patch permissions are not owner-only")
        except BaseException:
            if descriptor is not None:
                os.close(descriptor)
            temporary.cleanup()
            raise
        with self._lock:
            self._patch_tempdir = temporary
        return patch_file

    def _read_output(self, stream: Any) -> None:
        try:
            for raw_line in stream:
                try:
                    ready = parse_deepseek_harness_readiness(raw_line)
                except DeepSeekHarnessProtocolError as exc:
                    self._events.put(_ReaderFailure(exc))
                    return
                if ready is not None:
                    # The credential-bearing raw line is never retained.
                    self._events.put(ready)
                    continue
                safe_line = redact_deepseek_harness_output(raw_line)
                if safe_line:
                    with self._lock:
                        self._diagnostics.append(safe_line)
            self._events.put(_READER_EOF)
        except (OSError, UnicodeError, ValueError) as exc:
            self._events.put(_ReaderFailure(exc))

    def _read_credential_channel(self, channel: socket.socket) -> None:
        """Drain redacted MCP stderr from the duplex bootstrap channel."""

        try:
            with channel.makefile(
                "r",
                encoding="utf-8",
                errors="replace",
            ) as stream:
                for raw_line in stream:
                    safe_line = redact_deepseek_harness_output(raw_line)
                    if safe_line:
                        with self._lock:
                            self._diagnostics.append(safe_line)
        except (OSError, UnicodeError, ValueError):
            # Shutdown closes the socket to wake this daemon reader. MCP stderr
            # is diagnostic-only and must never destabilize the app lifecycle.
            return

    def start(
        self,
        *,
        cancel_event: threading.Event | None = None,
    ) -> DeepSeekHarnessReady:
        """Spawn Harness and wait for its validated loopback readiness."""

        if cancel_event is not None and cancel_event.is_set():
            raise DeepSeekHarnessCancelled("DeepSeek Harness startup was cancelled")
        with self._lock:
            if self._closed:
                raise DeepSeekHarnessStateError("DeepSeek Harness host is closed")
            if self._cancel_event.is_set():
                raise DeepSeekHarnessStateError(
                    "DeepSeek Harness host has already been stopped",
                )
            if self._ready is not None and self.is_running:
                return self._ready
            if self._starting or self._process is not None:
                raise DeepSeekHarnessStateError(
                    "DeepSeek Harness host has already been started",
                )
            self._starting = True

        try:
            try:
                self._dsh_home.mkdir(mode=0o700, parents=True, exist_ok=True)
                os.chmod(self._dsh_home, 0o700)
            except OSError as exc:
                raise DeepSeekHarnessLaunchError(
                    f"DeepSeek Harness home is unavailable: {self._dsh_home}",
                ) from exc
            if not self._dsh_home.is_dir() or not os.access(
                self._dsh_home,
                os.W_OK,
            ):
                raise DeepSeekHarnessLaunchError(
                    f"DeepSeek Harness home is not writable: {self._dsh_home}",
                )
            try:
                if self._native_cache.is_symlink():
                    raise OSError("native cache is a symlink")
                self._native_cache.mkdir(mode=0o700, parents=False, exist_ok=True)
                os.chmod(self._native_cache, 0o700)
            except OSError as exc:
                raise DeepSeekHarnessLaunchError(
                    "DeepSeek Harness native cache is unavailable: "
                    f"{self._native_cache}",
                ) from exc
            if not self._native_cache.is_dir() or not os.access(
                self._native_cache,
                os.W_OK,
            ):
                raise DeepSeekHarnessLaunchError(
                    "DeepSeek Harness native cache is not writable: "
                    f"{self._native_cache}",
                )
            if self._maestro_integration and self._profile == DEFAULT_PROFILE:
                _prepare_maestro_profile(self._dsh_home)
            if self._maestro_integration:
                try:
                    self._artifact_root.mkdir(mode=0o700, parents=True, exist_ok=True)
                    os.chmod(self._artifact_root, 0o700)
                except OSError as exc:
                    raise DeepSeekHarnessLaunchError(
                        "MAESTRO harness artifact root is unavailable: "
                        f"{self._artifact_root}",
                    ) from exc
                if not self._artifact_root.is_dir() or not os.access(
                    self._artifact_root,
                    os.W_OK,
                ):
                    raise DeepSeekHarnessLaunchError(
                        "MAESTRO harness artifact root is not writable: "
                        f"{self._artifact_root}",
                    )
            command = self._resolve_command()
            try:
                version_status = (
                    probe_deepseek_harness_version(
                        command,
                        environ=self._environment,
                        cwd=self._workspace,
                    )
                    if self._version_probe is None
                    else self._version_probe(command)
                )
            except Exception as exc:  # noqa: BLE001 - injected probe boundary
                raise DeepSeekHarnessLaunchError(
                    "could not inspect the DeepSeek Harness version",
                ) from exc
            with self._lock:
                self._version_status = version_status
            if not version_status.compatible:
                observed = version_status.observed or "unknown"
                message = (
                    f"DeepSeek Harness version drift: found {observed}, but the "
                    f"MAESTRO integration is pinned to {version_status.expected} "
                    f"({version_status.provenance})"
                )
                if version_status.error:
                    message += f"; {version_status.error}"
                if self._require_pinned_version:
                    raise DeepSeekHarnessLaunchError(message)
                with self._lock:
                    self._diagnostics.append(message)
            patch_file: Path | None = None
            if self._maestro_integration:
                if not self._care_python.is_file():
                    raise DeepSeekHarnessLaunchError(
                        f"MAESTRO Python executable is unavailable: {self._care_python}",
                    )
                if not self._care_cwd.is_dir():
                    raise DeepSeekHarnessLaunchError(
                        f"MAESTRO CARE working directory is unavailable: {self._care_cwd}",
                    )
                integration = resolve_maestro_harness_integration(
                    integration_dir=self._integration_dir,
                    environ=self._environment,
                )
                patch_file = self._materialize_integration_patch(integration)
            argv = build_deepseek_harness_argv(
                command,
                profile=self._profile,
                patch_file=patch_file,
            )
            child_credential_channel: socket.socket | None = None
            if self._credential_provider is not None:
                parent_channel: socket.socket | None = None
                if os.name == "nt":
                    from care.runtime.windows_credentials import (
                        BROKER_PID_ENV, BROKER_PORT_ENV, WindowsCredentialBrokerServer,
                    )
                    broker = WindowsCredentialBrokerServer(
                        session_id=self._session_id, provider=self._credential_provider,
                        helper=self._care_python,
                    )
                else:
                    broker = HarnessCredentialBrokerServer(
                        session_id=self._session_id, provider=self._credential_provider,
                    )
                try:
                    broker.start()
                    if os.name == "nt":
                        self._environment[BROKER_PORT_ENV] = str(broker.bootstrap.tcp_port)
                        self._environment[BROKER_PID_ENV] = str(os.getpid())
                    else:
                        parent_channel, child_credential_channel = socket.socketpair(
                            socket.AF_UNIX, socket.SOCK_STREAM,
                        )
                        bootstrap_frame = encode_harness_credential_bootstrap(broker.bootstrap)
                        try:
                            parent_channel.sendall(bootstrap_frame)
                        finally:
                            for index in range(len(bootstrap_frame)):
                                bootstrap_frame[index] = 0
                except Exception as exc:  # noqa: BLE001 - keep path/detail private
                    if parent_channel is not None:
                        parent_channel.close()
                    if child_credential_channel is not None:
                        child_credential_channel.close()
                        child_credential_channel = None
                    broker.close()
                    raise DeepSeekHarnessLaunchError(
                        "could not start the private MAESTRO credential broker"
                    ) from exc
                with self._lock:
                    self._credential_broker = broker
                    if parent_channel is None and os.name != "nt":
                        raise AssertionError("credential channel was not created")
                    self._credential_channel = parent_channel
                if child_credential_channel is not None:
                    self._environment[BROKER_BOOTSTRAP_FD_ENV] = str(child_credential_channel.fileno())
            spawn_kwargs: dict[str, Any] = {
                "cwd": str(self._workspace),
                "env": self._environment,
                "stdin": subprocess.DEVNULL,
                "stdout": subprocess.PIPE,
                "stderr": subprocess.STDOUT,
                "text": True,
                "encoding": "utf-8",
                "errors": "replace",
                "bufsize": 1,
                "start_new_session": os.name == "posix",
            }
            if os.name == "nt":
                spawn_kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
            if child_credential_channel is not None:
                spawn_kwargs["pass_fds"] = (child_credential_channel.fileno(),)
            try:
                process = self._popen(list(argv), **spawn_kwargs)
                if os.name == "nt" and self._credential_broker is not None:
                    self._credential_broker.bind_harness(process)
            except OSError as exc:
                raise DeepSeekHarnessLaunchError(
                    f"could not start DeepSeek Harness: {exc}",
                ) from exc
            finally:
                if child_credential_channel is not None:
                    child_credential_channel.close()
            if process.stdout is None:
                self._terminate_process(process)
                raise DeepSeekHarnessLaunchError(
                    "DeepSeek Harness process has no readable startup stream",
                )
            with self._lock:
                self._process = process
                reader = threading.Thread(
                    target=self._read_output,
                    args=(process.stdout,),
                    name="maestro-deepseek-harness-output",
                    daemon=True,
                )
                self._reader = reader
                reader.start()
                if self._credential_channel is not None:
                    credential_reader = threading.Thread(
                        target=self._read_credential_channel,
                        args=(self._credential_channel,),
                        name="maestro-credential-channel-output",
                        daemon=True,
                    )
                    self._credential_reader = credential_reader
                    credential_reader.start()

            deadline = time.monotonic() + self._startup_timeout
            while True:
                if self._cancel_event.is_set() or (
                    cancel_event is not None and cancel_event.is_set()
                ):
                    raise DeepSeekHarnessCancelled(
                        "DeepSeek Harness startup was cancelled",
                    )
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise DeepSeekHarnessLaunchError(
                        "DeepSeek Harness did not become ready within "
                        f"{self._startup_timeout:g}s"
                        + _diagnostic_suffix(self.diagnostics),
                    )
                try:
                    event = self._events.get(timeout=min(0.1, remaining))
                except queue.Empty:
                    if self._cancel_event.is_set() or (
                        cancel_event is not None and cancel_event.is_set()
                    ):
                        raise DeepSeekHarnessCancelled(
                            "DeepSeek Harness startup was cancelled",
                        )
                    code = process.poll()
                    if code is not None:
                        raise DeepSeekHarnessLaunchError(
                            f"DeepSeek Harness exited before readiness (code {code})"
                            + _diagnostic_suffix(self.diagnostics),
                        )
                    continue
                # ``stop`` terminates the child and closes its stream.  The
                # reader can therefore publish EOF in the same scheduling
                # slice as cancellation; explicit cancellation must win over
                # the derived "process exited" diagnostic.
                if self._cancel_event.is_set() or (
                    cancel_event is not None and cancel_event.is_set()
                ):
                    raise DeepSeekHarnessCancelled(
                        "DeepSeek Harness startup was cancelled",
                    )
                if isinstance(event, DeepSeekHarnessReady):
                    if process.poll() is not None:
                        raise DeepSeekHarnessLaunchError(
                            "DeepSeek Harness exited while publishing readiness"
                            + _diagnostic_suffix(self.diagnostics),
                        )
                    with self._lock:
                        self._ready = event
                    return event
                if isinstance(event, _ReaderFailure):
                    if isinstance(event.error, DeepSeekHarnessProtocolError):
                        raise event.error
                    raise DeepSeekHarnessLaunchError(
                        "could not read DeepSeek Harness startup output"
                        + _diagnostic_suffix(self.diagnostics),
                    ) from event.error
                if event is _READER_EOF:
                    code = process.poll()
                    if code is not None:
                        raise DeepSeekHarnessLaunchError(
                            f"DeepSeek Harness exited before readiness (code {code})"
                            + _diagnostic_suffix(self.diagnostics),
                        )
        except BaseException:
            self.stop()
            raise
        finally:
            with self._lock:
                self._starting = False

    def _terminate_process(self, process: Any) -> None:
        pid = getattr(process, "pid", None)
        windows_children = []
        if os.name == "nt" and isinstance(pid, int):
            import psutil

            try:
                windows_children = psutil.Process(pid).children(recursive=True)
            except psutil.Error:
                pass
        process_group = (
            pid
            if os.name == "posix" and isinstance(pid, int) and pid > 0
            else None
        )
        deadline = time.monotonic() + self._shutdown_timeout
        if process.poll() is None:
            try:
                if process_group is not None:
                    os.killpg(process_group, signal.SIGTERM)
                else:
                    process.terminate()
            except ProcessLookupError:
                pass
            except (OSError, ValueError):
                process.terminate()
        try:
            process.wait(timeout=max(0.001, deadline - time.monotonic()))
        except (subprocess.TimeoutExpired, TimeoutError):
            pass

        # ``wait`` only proves that the DSH leader exited.  Its reconnecting
        # MCP child can still be alive in the same session, so keep the group
        # under observation for the remainder of the grace period and then
        # kill the whole group if anything remains.
        group_alive = False
        if process_group is not None:
            while time.monotonic() < deadline:
                try:
                    os.killpg(process_group, 0)
                except ProcessLookupError:
                    break
                except PermissionError:
                    group_alive = True
                except OSError:
                    break
                else:
                    group_alive = True
                time.sleep(min(0.02, max(0.0, deadline - time.monotonic())))
            else:
                group_alive = True

        if group_alive:
            try:
                os.killpg(process_group, signal.SIGKILL)
            except ProcessLookupError:
                group_alive = False
            except (OSError, ValueError):
                # Fall through to the direct Popen handle when the process no
                # longer owns the expected session.
                group_alive = False

        if process.poll() is None and not group_alive:
            try:
                process.kill()
            except ProcessLookupError:
                pass
            except (OSError, ValueError):
                process.kill()
        try:
            process.wait(timeout=self._shutdown_timeout)
        except (subprocess.TimeoutExpired, TimeoutError, ProcessLookupError):
            # Cleanup is best-effort during app shutdown.  The child is in a
            # new process session and has already received the strongest Popen
            # termination primitive available on this platform.
            pass

        if windows_children:
            for child in windows_children:
                try:
                    child.terminate()
                except psutil.Error:
                    pass
            _, alive = psutil.wait_procs(windows_children, timeout=1)
            for child in alive:
                try:
                    child.kill()
                except psutil.Error:
                    pass
            psutil.wait_procs(alive, timeout=1)

    def stop(self) -> None:
        """Idempotently terminate Harness, escalating to kill after a bound."""

        self._cancel_event.set()
        with self._lock:
            process = self._process
            reader = self._reader
            patch_tempdir = self._patch_tempdir
            credential_broker = self._credential_broker
            credential_provider = self._credential_provider
            credential_channel = self._credential_channel
            credential_reader = self._credential_reader
            self._process = None
            self._reader = None
            self._ready = None
            self._patch_tempdir = None
            self._credential_broker = None
            self._credential_provider = None
            self._credential_channel = None
            self._credential_reader = None
            self._environment.pop(BROKER_BOOTSTRAP_FD_ENV, None)
            self._environment.pop("MAESTRO_HARNESS_CREDENTIAL_BROKER_PORT", None)
            self._environment.pop("MAESTRO_HARNESS_CREDENTIAL_BROKER_PID", None)
        if process is not None:
            self._terminate_process(process)
            stream = getattr(process, "stdout", None)
            if stream is not None:
                try:
                    stream.close()
                except (OSError, ValueError):
                    pass
        if reader is not None and reader is not threading.current_thread():
            reader.join(timeout=self._shutdown_timeout)
        if credential_channel is not None:
            try:
                credential_channel.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            credential_channel.close()
        if (
            credential_reader is not None
            and credential_reader is not threading.current_thread()
        ):
            credential_reader.join(timeout=self._shutdown_timeout)
        if patch_tempdir is not None:
            patch_tempdir.cleanup()
        if credential_broker is not None:
            credential_broker.close()
        elif credential_provider is not None:
            close = getattr(credential_provider, "close", None)
            if callable(close):
                close()

    def cancel(self) -> None:
        """Cancel an in-flight startup or stop an already-running backend."""

        self.stop()

    def close(self) -> None:
        with self._lock:
            self._closed = True
        self.stop()

    def __enter__(self) -> DeepSeekHarnessHost:
        self.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()


__all__ = [
    "BUNDLED_DSH_RUNTIME_DIRECTORY",
    "BUNDLED_DSH_RUNTIME_EXECUTABLE",
    "BUNDLED_DSH_RUNTIME_FILENAMES",
    "DEFAULT_DSH_HOME",
    "DEFAULT_PROFILE",
    "DSH_CLIENT_TITLE_ENV",
    "DSH_EXECUTABLE_ENV",
    "DSH_HOME_ENV",
    "DSH_SOURCE_ENV",
    "DSH_TELEMETRY_DISABLED_ENV",
    "PKG_NATIVE_CACHE_PATH_ENV",
    "MAESTRO_CWD_ENV",
    "MAESTRO_WORKSPACE_ROOT_ENV",
    "MAESTRO_ARTIFACT_ROOT_ENV",
    "MAESTRO_MEMORY_ENABLED_ENV",
    "MAESTRO_GENERATION_ENABLED_ENV",
    "MAESTRO_EXECUTION_ENABLED_ENV",
    "MAESTRO_EVOLUTION_ENABLED_ENV",
    "MAESTRO_MEMORY_WRITE_ENABLED_ENV",
    "MAESTRO_PYTHON_ENV",
    "MAESTRO_PYTHONHOME_ENV",
    "MAESTRO_SESSION_ID_ENV",
    "PINNED_DSH_VERSION",
    "DeepSeekHarnessCancelled",
    "DeepSeekHarnessCommand",
    "DeepSeekHarnessDiscoveryError",
    "DeepSeekHarnessError",
    "DeepSeekHarnessHost",
    "DeepSeekHarnessLaunchError",
    "DeepSeekHarnessProtocolError",
    "DeepSeekHarnessReady",
    "DeepSeekHarnessStateError",
    "DeepSeekHarnessVersionStatus",
    "MaestroHarnessIntegration",
    "build_deepseek_harness_argv",
    "discover_deepseek_harness",
    "parse_deepseek_harness_readiness",
    "probe_deepseek_harness_version",
    "redact_deepseek_harness_output",
    "resolve_maestro_harness_integration",
]
