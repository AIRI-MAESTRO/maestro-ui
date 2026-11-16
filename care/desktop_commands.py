"""Safe in-process bridge from the Maestro desktop UI to the CLI surface.

The desktop application exposes the same argparse command tree as ``maestro``
without starting a shell or a subprocess.  This module deliberately reuses
``care.cli._build_parser`` and the handlers stored on the parsed namespace so
CLI and desktop parsing cannot silently diverge.

Commands that can write state, affect an external service, or invoke a model /
tool require a short-lived, command-bound, one-use confirmation token.  The
confirmation check lives here on the server side; a UI confirmation dialog is
only presentation and is never the security boundary.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import importlib.util
import inspect
import io
import math
import os
import re
import secrets
import shlex
import sys
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, TextIO

from care import cli as care_cli
from care.desktop_domain_commands import (
    DesktopDomainCommands,
    DomainCommandError,
    DomainCommandResult,
    DomainRisk,
)
from care.desktop_domain_commands_second import DesktopSecondDomainCommands
from care.desktop_remaining_commands import DesktopRemainingCommands
from care.desktop_tui_parity_commands import DesktopTuiParityCommands
from care.redaction import redact_sensitive_text
from care.runtime.workspace_context import bind_workspace_root


MAX_COMMAND_CHARS = 16_384
MAX_COMMAND_TOKENS = 512
MAX_COMMAND_OUTPUT_CHARS = 128 * 1024
DEFAULT_CONFIRMATION_TTL_SECONDS = 90.0
DEFAULT_MAX_CONFIRMATIONS = 128
DEFAULT_LOG_LINE_LIMIT = 100
MAX_LOG_LINE_LIMIT = 500
DEFAULT_COST_RUN_LIMIT = 1_000
MAX_COST_RUN_LIMIT = 5_000
MAX_PROFILE_ROWS = 500
MAX_TRUST_ROWS = 500
MAX_ROLLUP_ROWS = 500


def _default_list_profiles() -> list[Any]:
    from care.runtime.profiles import list_profiles

    return list_profiles()


def _default_active_profile_name() -> str:
    from care.runtime.profiles import active_profile_name

    return active_profile_name()


def _default_active_log_path() -> Path | None:
    from care.runtime.log_discovery import active_log_path

    return active_log_path()


def _default_tail_log_lines(path: Path, **kwargs: Any) -> list[str]:
    from care.runtime.log_discovery import tail_log_lines

    return tail_log_lines(path, **kwargs)


def _default_load_local_runs(**kwargs: Any) -> list[Any]:
    from care.runtime.local_run_history import load_local_runs

    return load_local_runs(**kwargs)


def _default_list_trusted_skills() -> list[Any]:
    from care.sandbox.trust import SkillTrustStore

    return SkillTrustStore.load().list_trusted()


def _default_detect_voice_backend() -> str | None:
    for package in ("whisper", "faster_whisper"):
        try:
            spec = importlib.util.find_spec(package)
        except (ImportError, ValueError):
            spec = None
        if spec is not None:
            return package
    return None


@dataclass(frozen=True)
class DesktopCommandSources:
    """Injectable read-only data seams for desktop-only slash adapters."""

    list_profiles: Callable[[], list[Any]] = _default_list_profiles
    active_profile_name: Callable[[], str] = _default_active_profile_name
    active_log_path: Callable[[], Path | None] = _default_active_log_path
    tail_log_lines: Callable[..., list[str]] = _default_tail_log_lines
    load_local_runs: Callable[..., list[Any]] = _default_load_local_runs
    list_trusted_skills: Callable[[], list[Any]] = _default_list_trusted_skills
    detect_voice_backend: Callable[[], str | None] = _default_detect_voice_backend


class CommandRisk(str, Enum):
    """User-visible side-effect class for a parsed CLI invocation."""

    READ_ONLY = "read_only"
    FILESYSTEM_MUTATION = "filesystem_write_or_mutation"
    EXTERNAL_SIDE_EFFECT = "external_side_effect"
    PROVIDER_EXECUTION = "provider_or_tool_execution"


class CommandRequestError(ValueError):
    """Stable validation error raised before argparse or handler execution."""

    def __init__(self, code: str, message: str, *, status: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.status = status


_RISK_REASONS: dict[CommandRisk, str] = {
    CommandRisk.FILESYSTEM_MUTATION: (
        "This command can write files or mutate Maestro state."
    ),
    CommandRisk.EXTERNAL_SIDE_EFFECT: (
        "This command can change an external service or remote state."
    ),
    CommandRisk.PROVIDER_EXECUTION: (
        "This command can call a model provider or execute agent tools."
    ),
}


@dataclass(frozen=True)
class CommandDispatch:
    """HTTP-neutral result returned by :class:`CommandBridge`."""

    status: int
    payload: dict[str, Any]


@dataclass(frozen=True)
class _Confirmation:
    digest: str
    risk: CommandRisk
    reason: str
    expires_at: float


@dataclass(frozen=True)
class _RoutePolicy:
    default_risk: CommandRisk
    confirmation: str
    detail: str = ""


_POLICIES: dict[tuple[str, ...], _RoutePolicy] = {
    ("catalog",): _RoutePolicy(CommandRisk.READ_ONLY, "never"),
    ("validate",): _RoutePolicy(CommandRisk.READ_ONLY, "never"),
    ("import",): _RoutePolicy(
        CommandRisk.READ_ONLY,
        "conditional",
        "--apply mutates Memory; preview is read-only.",
    ),
    ("memory", "ls"): _RoutePolicy(CommandRisk.READ_ONLY, "never"),
    ("memory", "history"): _RoutePolicy(CommandRisk.READ_ONLY, "never"),
    ("memory", "show"): _RoutePolicy(CommandRisk.READ_ONLY, "never"),
    ("evolve",): _RoutePolicy(CommandRisk.EXTERNAL_SIDE_EFFECT, "always"),
    ("help",): _RoutePolicy(CommandRisk.READ_ONLY, "never"),
    ("run",): _RoutePolicy(
        CommandRisk.READ_ONLY,
        "conditional",
        "--execute invokes the runtime; export/log/save flags write state.",
    ),
    ("generate",): _RoutePolicy(CommandRisk.PROVIDER_EXECUTION, "always"),
    ("app",): _RoutePolicy(CommandRisk.READ_ONLY, "never"),
    ("tui",): _RoutePolicy(CommandRisk.READ_ONLY, "never"),
    ("web",): _RoutePolicy(CommandRisk.READ_ONLY, "never"),
    ("replay",): _RoutePolicy(CommandRisk.READ_ONLY, "never"),
    ("lineage",): _RoutePolicy(CommandRisk.READ_ONLY, "never"),
    ("search",): _RoutePolicy(CommandRisk.READ_ONLY, "never"),
    ("diff",): _RoutePolicy(CommandRisk.READ_ONLY, "never"),
    ("favourite",): _RoutePolicy(CommandRisk.FILESYSTEM_MUTATION, "always"),
    ("versions",): _RoutePolicy(CommandRisk.READ_ONLY, "never"),
    ("rollback",): _RoutePolicy(CommandRisk.FILESYSTEM_MUTATION, "always"),
    ("promote",): _RoutePolicy(CommandRisk.FILESYSTEM_MUTATION, "always"),
    ("forget",): _RoutePolicy(
        CommandRisk.READ_ONLY,
        "conditional",
        "--force mutates Memory; preview is read-only.",
    ),
    ("revise",): _RoutePolicy(CommandRisk.PROVIDER_EXECUTION, "always"),
    ("export",): _RoutePolicy(CommandRisk.FILESYSTEM_MUTATION, "always"),
    ("compose",): _RoutePolicy(
        CommandRisk.READ_ONLY,
        "conditional",
        "--yes saves a composed chain; preview is read-only.",
    ),
    ("deploy",): _RoutePolicy(CommandRisk.EXTERNAL_SIDE_EFFECT, "always"),
    ("deployments",): _RoutePolicy(CommandRisk.READ_ONLY, "never"),
    ("metrics",): _RoutePolicy(CommandRisk.READ_ONLY, "never"),
    ("dataset", "list"): _RoutePolicy(CommandRisk.READ_ONLY, "never"),
    ("dataset", "add"): _RoutePolicy(
        CommandRisk.FILESYSTEM_MUTATION,
        "always",
    ),
    ("dataset", "run"): _RoutePolicy(CommandRisk.PROVIDER_EXECUTION, "always"),
    ("dataset", "export"): _RoutePolicy(
        CommandRisk.FILESYSTEM_MUTATION,
        "always",
    ),
    ("remember",): _RoutePolicy(CommandRisk.FILESYSTEM_MUTATION, "always"),
    ("notes",): _RoutePolicy(CommandRisk.READ_ONLY, "never"),
    ("marketplace",): _RoutePolicy(CommandRisk.READ_ONLY, "never"),
    ("init",): _RoutePolicy(CommandRisk.FILESYSTEM_MUTATION, "always"),
    ("doctor",): _RoutePolicy(CommandRisk.READ_ONLY, "never"),
    ("migrate-secrets",): _RoutePolicy(
        CommandRisk.FILESYSTEM_MUTATION,
        "conditional",
        "--dry-run is read-only; applying the migration writes config/keystore.",
    ),
}


# The complete audited TUI slash namespace.  It is metadata, not an alternate
# route table: equal spelling does not prove equal semantics.  Only entries
# classified ``cli`` may cross into argparse.  ``backend`` entries use the
# small read-only adapters in this module; ``local`` entries are handled by the
# desktop frontend.  ``unavailable`` entries require a dedicated domain/UI
# adapter before the native app may claim them.
_SLASH_LOCAL: dict[str, tuple[str, str]] = {
    "artifacts": (
        "adapted",
        "Inspect bounded artifacts attached to the current desktop run; this is not the persisted TUI artifact store.",
    ),
    "blocks": (
        "adapted",
        "List or copy fenced blocks from the visible desktop run, or atomically save one below the current workspace.",
    ),
    "branch": (
        "adapted",
        "Save, list, switch, or delete process-local run-ID checkpoints; no transcript files are persisted.",
    ),
    "clear": ("exact", "Clear the current persistent desktop session."),
    "edit": (
        "adapted",
        "List or prefill prompts from process-local desktop runs; submitting creates a fresh run.",
    ),
    "exit": ("exact", "Alias of /quit; close the native Maestro window."),
    "help": ("adapted", "Show desktop and CLI command help."),
    "history": (
        "adapted",
        "Open bounded desktop run history with an optional positive row limit; persisted conversations remain available through /sessions.",
    ),
    "mode": (
        "exact",
        "Switch the native session between interactive Chat and production Agent mode; Fast/Deep remains a separate generation setting.",
    ),
    "multi": ("adapted", "Open the desktop multi-line composer."),
    "new": (
        "exact",
        "Archive the current conversation and start a persistent session.",
    ),
    "quit": ("exact", "Close the native Maestro window."),
    "runs": (
        "adapted",
        "Alias of desktop /history with the same optional positive row limit over process-local run telemetry.",
    ),
    "resume": (
        "adapted",
        "Resume the latest or one exact workspace-scoped persistent desktop session.",
    ),
    "sessions": (
        "adapted",
        "List workspace-scoped persistent desktop conversations; run telemetry remains separately process-local.",
    ),
    "settings": (
        "adapted",
        "Open the native provider, model, Keychain credential, service, and defaults editor.",
    ),
    "subagents": (
        "adapted",
        "Inspect or clear process-local chain-step and tool event cards.",
    ),
    "theme": ("adapted", "Select one of the desktop appearance themes."),
    "tour": (
        "adapted",
        "Show a five-step native desktop walkthrough rather than the TUI overlay.",
    ),
}

_SLASH_CLI_TARGETS: dict[str, tuple[tuple[str, ...], str]] = {
    "dataset": (
        ("dataset",),
        "CLI list/add/run/export forms are accepted; TUI-only rubric UI is omitted.",
    ),
    "metrics": (
        ("metrics",),
        "The CLI exposes the same read-only deployment metrics lookup.",
    ),
    "status": (
        ("doctor",),
        "The TUI explicitly uses the same health-probe family as `maestro doctor`.",
    ),
}

_SLASH_BACKEND: dict[str, str] = {
    "cost": ("Read bounded local run history and return aggregate token/cost rollups."),
    "log": (
        "Read a bounded, filtered tail of the active local log with secrets redacted."
    ),
    "logs": (
        "Read a bounded, filtered tail of the active local log; no live file watcher."
    ),
    "profile": (
        "List profile metadata and the active profile name; credentials are never read."
    ),
}

# These commands use the same Memory, LTM, and hub services as their TUI
# handlers.  Keep them separate from both CLI aliases (same spelling is not
# semantic parity) and the small read-only desktop diagnostics above.
_SLASH_DOMAIN: dict[str, tuple[str, str, str, str]] = {
    "deploy": (
        "exact",
        "Resolve an id or unique name, run the TUI deploy gate, then confirm hub autostart/deployment and return the per-agent credential once.",
        CommandRisk.EXTERNAL_SIDE_EFFECT.value,
        "always",
    ),
    "deployments": (
        "adapted",
        "List, undeploy, reload, or return a safe docs URL through the TUI's hub service; the native app does not open a browser.",
        "mixed",
        "conditional",
    ),
    "evolution": (
        "adapted",
        "List and inspect evolution runs, configure or launch an evolution, read a bounded event batch, and confirmation-gate accepting an individual.",
        "mixed",
        "conditional",
    ),
    "export": (
        "exact",
        "Export the current native transcript as a redacted md, mdx, json, or html file inside the workspace exports directory.",
        CommandRisk.FILESYSTEM_MUTATION.value,
        "always",
    ),
    "forget": (
        "exact",
        "Preview or soft-delete one exact chain id plus every linked dataset card through the TUI's Memory facade.",
        "mixed",
        "conditional",
    ),
    "library": (
        "adapted",
        "Browse or search the same Memory-backed chain rows as the TUI Library screen in a structured command result.",
        CommandRisk.READ_ONLY.value,
        "never",
    ),
    "marketplace": (
        "adapted",
        "Browse recent shared skills, search capabilities, or confirmation-gate the TUI-supported install operation.",
        "mixed",
        "conditional",
    ),
    "imgpreview": (
        "adapted",
        "Validate and preview a bounded image from the current workspace through the authenticated native server.",
        CommandRisk.READ_ONLY.value,
        "never",
    ),
    "memory": (
        "adapted",
        "Show or forget long-term memory and safely open/save the configured CARE.md document through the native editor.",
        "mixed",
        "conditional",
    ),
    "sandbox": (
        "exact",
        "List SHA-pinned skill approvals or confirmation-gate revocation by one exact SHA-256.",
        "mixed",
        "conditional",
    ),
    "voice": (
        "adapted",
        "Report the configured local transcription backend or transcribe one validated workspace audio file without uploading it.",
        "mixed",
        "conditional",
    ),
    "rollback": (
        "exact",
        "Resolve a chain id or unique name and repoint a channel to an explicit or inferred previous version.",
        CommandRisk.FILESYSTEM_MUTATION.value,
        "always",
    ),
    "promote": (
        "exact",
        "Resolve the candidate, run the TUI artifact/baseline/eval release gate (or explicit --force), then move the target channel after confirmation.",
        "mixed",
        "always",
    ),
    "remember": (
        "adapted",
        "Use the TUI long-term-memory merge path; call the configured model only after confirmation, with the same bounded raw-note fallback.",
        "mixed",
        "always",
    ),
    "revise": (
        "adapted",
        "Preview a provider-generated revision for one exact chain id, then explicitly save or discard the expiring draft.",
        "mixed",
        "always",
    ),
    "run": (
        "adapted",
        "Inspect and preflight one exact saved chain; --execute uses the CLI/TUI runtime path after provider/tool confirmation.",
        "mixed",
        "conditional",
    ),
    "upload": (
        "exact",
        "Upload one exact saved chain to the configured HTTPS-or-loopback endpoint after confirmation.",
        CommandRisk.EXTERNAL_SIDE_EFFECT.value,
        "always",
    ),
    "versions": (
        "exact",
        "Resolve a chain id or unique name, then list channel-marked versions or diff two version ids.",
        CommandRisk.READ_ONLY.value,
        "never",
    ),
}

_SLASH_UNAVAILABLE: dict[str, tuple[str, str | None]] = {}

_SLASH_NAMES = frozenset(
    {
        *_SLASH_LOCAL,
        *_SLASH_CLI_TARGETS,
        *_SLASH_BACKEND,
        *_SLASH_DOMAIN,
        *_SLASH_UNAVAILABLE,
    }
)


_DESKTOP_UNAVAILABLE: dict[tuple[str, ...], tuple[str, str]] = {
    ("app",): ("launcher", "Maestro is already running in this desktop window."),
    ("tui",): (
        "launcher",
        "TUI is a compatibility launcher; run `maestro tui` from a terminal.",
    ),
    ("web",): (
        "developer",
        "The browser server is a developer-only surface; use this Maestro window.",
    ),
}


# Source audit baseline before the explicit ``tui`` compatibility launcher was
# introduced.  Keep this separate from live parser counts so adding the launcher
# does not erase the reviewed 32-command / 37-leaf parity invariant.
_LEGACY_TOP_LEVEL = frozenset(
    {
        "catalog",
        "validate",
        "import",
        "memory",
        "evolve",
        "help",
        "run",
        "generate",
        "app",
        "web",
        "replay",
        "lineage",
        "search",
        "diff",
        "favourite",
        "versions",
        "rollback",
        "promote",
        "forget",
        "revise",
        "export",
        "compose",
        "deploy",
        "deployments",
        "metrics",
        "dataset",
        "remember",
        "notes",
        "marketplace",
        "init",
        "doctor",
        "migrate-secrets",
    }
)
_LEGACY_LEAF_COUNT = 37


_SECRET_FLAG_WORDS = frozenset(
    {
        "api-key",
        "apikey",
        "api-token",
        "access-token",
        "refresh-token",
        "client-secret",
        "password",
        "passwd",
        "secret",
        "token",
    }
)


_STDIO_CAPTURE_LOCK = threading.RLock()


class _BoundedTextIO(io.StringIO):
    """StringIO-compatible sink that never retains more than its cap."""

    def __init__(self, limit: int = MAX_COMMAND_OUTPUT_CHARS) -> None:
        super().__init__()
        self.limit = limit
        self.truncated = False

    def write(self, value: str) -> int:
        text = str(value)
        remaining = max(0, self.limit - super().tell())
        if len(text) > remaining:
            self.truncated = True
        if remaining:
            super().write(text[:remaining])
        # TextIO.write reports the consumed input length.  Returning the full
        # length keeps print/json.dump callers from retrying discarded text.
        return len(text)

    def bounded_value(self) -> str:
        value = super().getvalue()
        if not self.truncated:
            return value
        marker = "\n… [output truncated]\n"
        if len(marker) >= self.limit:
            return marker[: self.limit]
        return value[: self.limit - len(marker)] + marker


class CommandBridge:
    """Discover and execute Maestro CLI commands inside the desktop process."""

    def __init__(
        self,
        *,
        parser_factory: Callable[[], argparse.ArgumentParser] = care_cli._build_parser,
        handler_overrides: Mapping[str, Callable[..., int]] | None = None,
        desktop_sources: DesktopCommandSources | None = None,
        domain_commands: DesktopDomainCommands | None = None,
        second_domain_commands: DesktopSecondDomainCommands | None = None,
        remaining_commands: DesktopRemainingCommands | None = None,
        tui_parity_commands: DesktopTuiParityCommands | None = None,
        workspace_root: str | os.PathLike[str] | None = None,
        confirmation_ttl_seconds: float = DEFAULT_CONFIRMATION_TTL_SECONDS,
        max_confirmations: int = DEFAULT_MAX_CONFIRMATIONS,
        clock: Callable[[], float] = time.monotonic,
        token_factory: Callable[[], str] | None = None,
    ) -> None:
        if confirmation_ttl_seconds <= 0:
            raise ValueError("confirmation_ttl_seconds must be positive")
        if max_confirmations <= 0:
            raise ValueError("max_confirmations must be positive")
        self._parser_factory = parser_factory
        self._handler_overrides = dict(handler_overrides or {})
        self._desktop_sources = desktop_sources or DesktopCommandSources()
        self._domain_commands = domain_commands or DesktopDomainCommands()
        self._second_domain_commands = (
            second_domain_commands or DesktopSecondDomainCommands()
        )
        self._remaining_commands = remaining_commands or DesktopRemainingCommands()
        self._tui_parity_commands = tui_parity_commands or DesktopTuiParityCommands()
        if workspace_root is None:
            self._workspace_root: Path | None = None
        else:
            workspace = Path(workspace_root).expanduser()
            if not workspace.is_absolute():
                raise ValueError("workspace_root must be an absolute path")
            if workspace.is_symlink() or not workspace.is_dir():
                raise ValueError(
                    "workspace_root must be an existing, non-symlink directory"
                )
            self._workspace_root = workspace.resolve()
        self._ttl = float(confirmation_ttl_seconds)
        self._max_confirmations = int(max_confirmations)
        self._clock = clock
        self._token_factory = token_factory or (lambda: secrets.token_urlsafe(32))
        self._confirmation_lock = threading.Lock()
        self._confirmations: dict[str, _Confirmation] = {}
        self._registry = _build_registry(self._parser_factory())

    def registry(self) -> dict[str, Any]:
        """Return a fresh JSON-safe copy of the argparse command registry."""

        # Registry values are plain nested containers.  Round-tripping through
        # a small recursive copier avoids handing shared mutable state to HTTP.
        return _copy_json_value(self._registry)

    def execute(
        self,
        command: Any,
        *,
        confirmation_token: Any = None,
        transcript: Any = None,
    ) -> CommandDispatch:
        """Parse, risk-check, and synchronously call one CLI handler."""

        with bind_workspace_root(self._workspace_root):
            return self._execute_bound(
                command,
                confirmation_token=confirmation_token,
                transcript=transcript,
            )

    def _execute_bound(
        self,
        command: Any,
        *,
        confirmation_token: Any = None,
        transcript: Any = None,
    ) -> CommandDispatch:
        """Execute while this bridge's validated workspace is context-bound."""

        started = time.perf_counter()
        try:
            argv, supplied_secrets, desktop_adapter = _normalise_command(command)
        except CommandRequestError as exc:
            return _error_dispatch(exc.status, exc.code, str(exc))
        except ValueError as exc:
            return _error_dispatch(400, "invalid_command", str(exc))

        positional_secrets = tuple(_private_positional_secrets(argv))
        supplied_secrets = tuple(
            dict.fromkeys((*supplied_secrets, *positional_secrets))
        )
        safe_argv = _redact_argv(argv)
        invocation = redact_sensitive_text(
            shlex.join(safe_argv),
            supplied_secrets,
        )
        if desktop_adapter is not None:
            return self._execute_desktop_adapter(
                desktop_adapter,
                argv[1:],
                invocation=invocation,
                supplied_secrets=supplied_secrets,
                confirmation_token=confirmation_token,
                transcript=transcript,
                started=started,
            )
        stdout = _BoundedTextIO()
        stderr = _BoundedTextIO()
        stdin = io.StringIO("")

        with _STDIO_CAPTURE_LOCK:
            parser = self._parser_factory()
            try:
                with (
                    _redirect_stdin(stdin),
                    contextlib.redirect_stdout(stdout),
                    contextlib.redirect_stderr(stderr),
                ):
                    namespace = parser.parse_args(argv)
            except SystemExit as exc:
                exit_code = int(exc.code or 0)
                payload = _result_payload(
                    ok=exit_code == 0,
                    invocation=invocation,
                    route=(),
                    risk=CommandRisk.READ_ONLY,
                    exit_code=exit_code,
                    stdout=stdout,
                    stderr=stderr,
                    duration_ms=_duration_ms(started),
                    executed=False,
                    secrets_to_remove=supplied_secrets,
                    error_code=None if exit_code == 0 else "parse_error",
                )
                return CommandDispatch(200 if exit_code == 0 else 400, payload)
            except BaseException as exc:  # keep malformed parsers inside HTTP
                return _exception_dispatch(exc, supplied_secrets)

        route = _route_from_namespace(parser, namespace)
        handler = getattr(namespace, "_handler", None)
        if not route or handler is None:
            return _error_dispatch(
                400,
                "incomplete_command",
                "command must select an executable leaf route",
                invocation=invocation,
            )

        # Registry discovery is intentionally dynamic, but execution policy is
        # explicit.  A future argparse leaf must never become executable merely
        # because the parser learned its name: fail closed until reviewed.
        if route not in _POLICIES:
            return _error_dispatch(
                501,
                "command_policy_missing",
                "this command is unavailable until a desktop execution policy is reviewed",
                invocation=invocation,
            )

        risk = _classify_risk(route, namespace)
        command_digest = _command_digest(argv)
        if risk is not CommandRisk.READ_ONLY:
            confirmation = self._confirm(
                confirmation_token,
                digest=command_digest,
                risk=risk,
            )
            if confirmation is not None:
                confirmation.payload["command"] = _command_meta(
                    invocation,
                    route,
                    risk,
                )
                return confirmation

        route_name = " ".join(route)
        special = _special_desktop_command(route, invocation, risk, started)
        if special is not None:
            return special
        handler = self._handler_overrides.get(route_name, handler)

        # Handlers receive their own bounded streams.  Global redirects cover
        # legacy print()/argparse paths; the lock protects those process-global
        # bindings without locking the HTTP server or RunManager itself.
        stdout = _BoundedTextIO()
        stderr = _BoundedTextIO()
        stdin = io.StringIO("")
        try:
            with (
                _STDIO_CAPTURE_LOCK,
                _redirect_stdin(stdin),
                contextlib.redirect_stdout(stdout),
                contextlib.redirect_stderr(stderr),
            ):
                kwargs: dict[str, Any] = {}
                try:
                    if "stdin" in inspect.signature(handler).parameters:
                        kwargs["stdin"] = stdin
                except (TypeError, ValueError):
                    pass
                exit_code = int(handler(namespace, stdout, stderr, **kwargs) or 0)
        except SystemExit as exc:
            exit_code = int(exc.code or 0)
        except BaseException as exc:
            return _exception_dispatch(
                exc,
                supplied_secrets,
                invocation=invocation,
                route=route,
                risk=risk,
                stdout=stdout,
                stderr=stderr,
                started=started,
            )

        ok = exit_code == 0
        payload = _result_payload(
            ok=ok,
            invocation=invocation,
            route=route,
            risk=risk,
            exit_code=exit_code,
            stdout=stdout,
            stderr=stderr,
            duration_ms=_duration_ms(started),
            executed=True,
            secrets_to_remove=supplied_secrets,
            error_code=None if ok else "command_failed",
        )
        return CommandDispatch(200, payload)

    def _execute_desktop_adapter(
        self,
        name: str,
        args: list[str],
        *,
        invocation: str,
        supplied_secrets: tuple[str, ...],
        confirmation_token: Any,
        transcript: Any,
        started: float,
    ) -> CommandDispatch:
        try:
            if name in _SLASH_DOMAIN:
                return self._execute_domain_adapter(
                    name,
                    args,
                    invocation=invocation,
                    supplied_secrets=supplied_secrets,
                    confirmation_token=confirmation_token,
                    transcript=transcript,
                    started=started,
                )
            if name == "profile":
                return self._desktop_profile(args, invocation, started)
            if name in {"log", "logs"}:
                return self._desktop_logs(
                    name,
                    args,
                    invocation,
                    supplied_secrets,
                    started,
                )
            if name == "cost":
                return self._desktop_cost(args, invocation, started)
            if name == "sandbox":
                return self._desktop_sandbox(args, invocation, started)
            if name == "voice":
                return self._desktop_voice(args, invocation, started)
        except CommandRequestError as exc:
            return _desktop_error_dispatch(
                exc.status,
                exc.code,
                str(exc),
                name=name,
                invocation=invocation,
            )
        except BaseException:
            # Data-layer exceptions can contain paths, environment values, or
            # malformed persisted content.  Never reflect them into HTTP.
            return _desktop_error_dispatch(
                200,
                "command_exception",
                f"Desktop /{name} data is temporarily unavailable.",
                name=name,
                invocation=invocation,
                exit_code=2,
                executed=True,
                started=started,
            )
        return _desktop_error_dispatch(
            501,
            "slash_command_unavailable",
            f"Desktop /{name} does not have a reviewed adapter.",
            name=name,
            invocation=invocation,
        )

    def _execute_domain_adapter(
        self,
        name: str,
        args: list[str],
        *,
        invocation: str,
        supplied_secrets: tuple[str, ...],
        confirmation_token: Any,
        transcript: Any,
        started: float,
    ) -> CommandDispatch:
        """Execute one TUI-domain plan behind the shared confirmation gate."""

        try:
            if name in DesktopTuiParityCommands.NAMES and (
                name != "memory" or (args and args[0].lower() == "edit")
            ):
                plan = self._tui_parity_commands.plan(name, args)
            elif name in DesktopRemainingCommands.NAMES:
                plan = self._remaining_commands.plan(
                    name,
                    args,
                    transcript=transcript if name == "export" else None,
                )
            else:
                service = (
                    self._second_domain_commands
                    if name in DesktopSecondDomainCommands.NAMES
                    else self._domain_commands
                )
                plan = service.plan(name, args)
        except DomainCommandError as exc:
            return _desktop_error_dispatch(
                exc.status,
                exc.code,
                str(exc),
                name=name,
                invocation=invocation,
                source="desktop_domain",
            )

        risk = _domain_risk(plan.risk)
        if risk is not CommandRisk.READ_ONLY:
            # Bind the token to both the typed command and the exact resolved
            # state/targets.  If a channel head, LTM value, deployment, or
            # linked-card set changes while the dialog is open, recomputing the
            # plan changes this digest and the old token fails closed.
            command_digest = _command_digest(
                [f"/{name}", *args, f"domain-scope:{plan.confirmation_scope}"]
            )
            confirmation = self._confirm(
                confirmation_token,
                digest=command_digest,
                risk=risk,
            )
            if confirmation is not None:
                if (
                    confirmation.payload.get("error", {}).get("code")
                    != "confirmation_required"
                ):
                    confirmation.payload["command"] = _desktop_command_meta(
                        name,
                        invocation,
                        risk=risk,
                        source="desktop_domain",
                    )
                    return confirmation
                reason = plan.reason or _RISK_REASONS[risk]
                effects = list(plan.effects)
                confirmation.payload["error"]["message"] = reason
                confirmation.payload["confirmation"].update(
                    {"reason": reason, "effects": effects}
                )
                confirmation.payload.update(
                    {
                        "effects": effects,
                        "preview": _copy_json_value(plan.preview),
                        "command": _desktop_command_meta(
                            name,
                            invocation,
                            risk=risk,
                            source="desktop_domain",
                            effects=effects,
                        ),
                    }
                )
                return confirmation

        try:
            result = plan.execute()
        except DomainCommandError as exc:
            return _desktop_error_dispatch(
                exc.status,
                exc.code,
                str(exc),
                name=name,
                invocation=invocation,
                exit_code=2,
                executed=True,
                started=started,
                risk=risk,
                source="desktop_domain",
            )
        except BaseException:
            # Raw client failures may carry credentials and local paths.  The
            # domain layer normally converts them, but keep this boundary
            # fail-closed even for a faulty injected adapter.
            return _desktop_error_dispatch(
                502,
                "domain_operation_failed",
                f"Desktop /{name} operation failed.",
                name=name,
                invocation=invocation,
                exit_code=2,
                executed=True,
                started=started,
                risk=risk,
                source="desktop_domain",
            )
        return _desktop_domain_dispatch(
            name,
            invocation,
            result,
            risk=risk,
            supplied_secrets=supplied_secrets,
            started=started,
        )

    def _desktop_profile(
        self,
        args: list[str],
        invocation: str,
        started: float,
    ) -> CommandDispatch:
        if args and [part.lower() for part in args] != ["list"]:
            raise CommandRequestError(
                "slash_action_unsupported",
                "Desktop /profile supports only `/profile` and `/profile list`.",
            )
        active = _safe_profile_name(self._desktop_sources.active_profile_name())
        rows: list[dict[str, Any]] = []
        source_rows = self._desktop_sources.list_profiles()
        truncated = len(source_rows) > MAX_PROFILE_ROWS
        for profile in source_rows[:MAX_PROFILE_ROWS]:
            name = _safe_profile_name(getattr(profile, "name", ""))
            if not name:
                continue
            rows.append(
                {
                    "name": name,
                    "active": bool(active and name == active),
                    "size_bytes": _nonnegative_int(getattr(profile, "size_bytes", 0)),
                    "modified_at": _finite_number(getattr(profile, "mtime", None)),
                }
            )
        payload = {
            "active_profile": active or None,
            "default_profile_active": not bool(active),
            "profiles": rows,
            "count": len(rows),
            "truncated": truncated,
        }
        lines = [f"Active profile: {active or '(default)'}"]
        lines.extend(
            f"- {row['name']}{' (active)' if row['active'] else ''}" for row in rows
        )
        if truncated:
            lines.append(f"… showing first {MAX_PROFILE_ROWS} profiles")
        return _desktop_success_dispatch(
            "profile",
            invocation,
            "\n".join(lines) + "\n",
            payload,
            started,
        )

    def _desktop_logs(
        self,
        name: str,
        args: list[str],
        invocation: str,
        supplied_secrets: tuple[str, ...],
        started: float,
    ) -> CommandDispatch:
        level, module, limit = _parse_log_query(args)
        path = self._desktop_sources.active_log_path()
        query = {"level": level, "module": module or None, "limit": limit}
        if path is None:
            return _desktop_success_dispatch(
                name,
                invocation,
                "No active Maestro log file.\n",
                {
                    "active": False,
                    "file_name": None,
                    "query": query,
                    "lines": [],
                    "count": 0,
                    "truncated": False,
                },
                started,
            )
        safe_lines, truncated = _bounded_redacted_lines(
            self._desktop_sources.tail_log_lines(
                path,
                max_lines=limit,
                level_floor=level,
                module_substr=module,
            ),
            supplied_secrets,
        )
        file_name = _safe_metadata_text(Path(path).name, max_chars=255)
        payload = {
            "active": True,
            "file_name": file_name,
            "query": query,
            "lines": safe_lines,
            "count": len(safe_lines),
            "truncated": truncated,
        }
        header = f"{len(safe_lines)} log line(s) from {file_name or 'active log'}"
        rendered = header + ":\n"
        if safe_lines:
            rendered += "\n".join(safe_lines) + "\n"
        return _desktop_success_dispatch(
            name,
            invocation,
            rendered,
            payload,
            started,
        )

    def _desktop_cost(
        self,
        args: list[str],
        invocation: str,
        started: float,
    ) -> CommandDispatch:
        limit = _parse_single_limit(
            args,
            default=DEFAULT_COST_RUN_LIMIT,
            maximum=MAX_COST_RUN_LIMIT,
            command="cost",
        )
        entries = self._desktop_sources.load_local_runs(limit=limit)
        from care.runtime.cost_rollups import (
            compute_overall,
            compute_per_chain,
            compute_per_mode,
            compute_per_provider,
        )

        overall = compute_overall(entries)
        payload = {
            "limit": limit,
            "overall": {
                "runs": _nonnegative_int(overall.runs),
                "successful_runs": _nonnegative_int(overall.successful_runs),
                "failed_runs": _nonnegative_int(overall.failed_runs),
                "tokens_in": _nonnegative_int(overall.tokens_in),
                "tokens_out": _nonnegative_int(overall.tokens_out),
                "tokens_total": _nonnegative_int(overall.tokens_total),
                "cost_usd": _finite_number(overall.cost_usd),
                "total_duration_seconds": _finite_number(
                    overall.total_duration_seconds
                ),
                "success_rate": _finite_number(overall.success_rate),
            },
            "by_provider": _rollup_rows(compute_per_provider(entries)),
            "by_chain": _rollup_rows(compute_per_chain(entries)),
            "by_mode": _rollup_rows(compute_per_mode(entries)),
        }
        stdout = (
            f"Runs: {payload['overall']['runs']}\n"
            f"Tokens: {payload['overall']['tokens_total']}\n"
            f"Cost: ${payload['overall']['cost_usd'] or 0.0:.6f}\n"
        )
        return _desktop_success_dispatch(
            "cost",
            invocation,
            stdout,
            payload,
            started,
        )

    def _desktop_sandbox(
        self,
        args: list[str],
        invocation: str,
        started: float,
    ) -> CommandDispatch:
        if args and [part.lower() for part in args] != ["list"]:
            raise CommandRequestError(
                "slash_action_unsupported",
                "Desktop /sandbox is read-only and supports only `/sandbox` or `/sandbox list`; revoke and trust changes are unavailable.",
            )
        source_rows = self._desktop_sources.list_trusted_skills()
        truncated = len(source_rows) > MAX_TRUST_ROWS
        rows = [
            _trust_record_payload(record) for record in source_rows[:MAX_TRUST_ROWS]
        ]
        payload = {
            "trusted_skills": rows,
            "count": len(rows),
            "truncated": truncated,
            "mutable": False,
        }
        stdout_lines = [f"Trusted skills: {len(rows)}"]
        stdout_lines.extend(f"- {row['name']} ({row['sha256_prefix']})" for row in rows)
        if truncated:
            stdout_lines.append(f"… showing first {MAX_TRUST_ROWS} approvals")
        return _desktop_success_dispatch(
            "sandbox",
            invocation,
            "\n".join(stdout_lines) + "\n",
            payload,
            started,
        )

    def _desktop_voice(
        self,
        args: list[str],
        invocation: str,
        started: float,
    ) -> CommandDispatch:
        if args and [part.lower() for part in args] != ["status"]:
            raise CommandRequestError(
                "slash_action_unsupported",
                "Desktop /voice supports status only; transcription and audio paths are unavailable.",
            )
        detected = self._desktop_sources.detect_voice_backend()
        backend = detected if detected in {"whisper", "faster_whisper"} else None
        payload = {
            "available": backend is not None,
            "backend": backend,
            "status_only": True,
            "transcription_available": False,
        }
        stdout = (
            f"Voice backend detected: {backend}. Status-only adapter.\n"
            if backend
            else "No local Whisper backend detected. Status-only adapter.\n"
        )
        return _desktop_success_dispatch(
            "voice",
            invocation,
            stdout,
            payload,
            started,
        )

    def _confirm(
        self,
        supplied_token: Any,
        *,
        digest: str,
        risk: CommandRisk,
    ) -> CommandDispatch | None:
        now = self._clock()
        reason = _RISK_REASONS[risk]
        with self._confirmation_lock:
            self._prune_confirmations(now)
            if supplied_token is not None:
                if not isinstance(supplied_token, str) or not supplied_token:
                    return _error_dispatch(
                        409,
                        "confirmation_invalid",
                        "confirmation token is invalid or has expired",
                    )
                challenge = self._confirmations.pop(supplied_token, None)
                if (
                    challenge is None
                    or challenge.expires_at <= now
                    or not secrets.compare_digest(challenge.digest, digest)
                    or challenge.risk is not risk
                ):
                    return _error_dispatch(
                        409,
                        "confirmation_invalid",
                        "confirmation token is invalid, expired, replayed, or for another command",
                    )
                return None

            if len(self._confirmations) >= self._max_confirmations:
                oldest = min(
                    self._confirmations,
                    key=lambda token: self._confirmations[token].expires_at,
                )
                self._confirmations.pop(oldest, None)
            token = self._token_factory()
            # Defend against a faulty injected token factory without ever
            # exposing or accepting two challenges under the same token.
            while not token or token in self._confirmations:
                token = self._token_factory()
            self._confirmations[token] = _Confirmation(
                digest=digest,
                risk=risk,
                reason=reason,
                expires_at=now + self._ttl,
            )

        return CommandDispatch(
            409,
            {
                "ok": False,
                "error": {
                    "code": "confirmation_required",
                    "message": reason,
                },
                "confirmation": {
                    "token": token,
                    "ttl_seconds": self._ttl,
                    "reason": reason,
                    "risk": risk.value,
                },
            },
        )

    def _prune_confirmations(self, now: float) -> None:
        expired = [
            token
            for token, challenge in self._confirmations.items()
            if challenge.expires_at <= now
        ]
        for token in expired:
            self._confirmations.pop(token, None)


def _normalise_command(
    command: Any,
) -> tuple[list[str], tuple[str, ...], str | None]:
    if not isinstance(command, str):
        raise ValueError("command must be a string")
    if not command.strip():
        raise ValueError("command must not be empty")
    if len(command) > MAX_COMMAND_CHARS:
        raise ValueError(f"command exceeds {MAX_COMMAND_CHARS} characters")
    if "\x00" in command:
        raise ValueError("command contains a NUL byte")
    try:
        argv = shlex.split(command, posix=True)
    except ValueError as exc:
        raise ValueError(f"command quoting is invalid: {exc}") from exc
    if argv and argv[0].lower() in {"care", "maestro"}:
        argv = argv[1:]
    if not argv:
        raise ValueError("command must include a Maestro subcommand")
    if len(argv) > MAX_COMMAND_TOKENS:
        raise ValueError(f"command exceeds {MAX_COMMAND_TOKENS} arguments")

    if argv[0].startswith("/"):
        slash_name = argv[0][1:].lower()
        if not slash_name:
            raise ValueError("slash command name must not be empty")
        if slash_name in _SLASH_LOCAL:
            raise CommandRequestError(
                "local_command",
                f"/{slash_name} is handled locally by the desktop interface",
                status=409,
            )
        if slash_name in _SLASH_UNAVAILABLE:
            reason, _related = _SLASH_UNAVAILABLE[slash_name]
            raise CommandRequestError(
                "slash_command_unavailable",
                reason,
                status=501,
            )
        if slash_name in _SLASH_BACKEND or slash_name in _SLASH_DOMAIN:
            argv[0] = f"/{slash_name}"
            return argv, tuple(_secret_values(argv)), slash_name
        target = _SLASH_CLI_TARGETS.get(slash_name, ((slash_name,), ""))[0]
        argv = [*target, *argv[1:]]

    secret_values = tuple(_secret_values(argv))
    return argv, secret_values, None


def _is_secret_flag(flag: str) -> bool:
    name = flag.lstrip("-").split("=", 1)[0].lower().replace("_", "-")
    return name in _SECRET_FLAG_WORDS or name.endswith("-api-key")


def _secret_values(argv: list[str]) -> list[str]:
    values: list[str] = []
    redact_next = False
    for token in argv:
        if redact_next:
            if token:
                values.append(token)
            redact_next = False
            continue
        if token.startswith("-") and "=" in token:
            flag, value = token.split("=", 1)
            if _is_secret_flag(flag) and value:
                values.append(value)
        elif token.startswith("-") and _is_secret_flag(token):
            redact_next = True
    return values


def _private_positional_secrets(argv: list[str]) -> list[str]:
    """Hide opaque document transport that generic credential rules cannot see."""

    if (
        len(argv) == 5
        and argv[0].lower() == "/memory"
        and argv[1].lower() == "edit"
        and argv[2].lower() == "save"
        and argv[4]
    ):
        return [argv[4]]
    return []


def _redact_argv(argv: list[str]) -> list[str]:
    redacted: list[str] = []
    redact_next = False
    for token in argv:
        if redact_next:
            redacted.append("[redacted]")
            redact_next = False
            continue
        if token.startswith("-") and "=" in token:
            flag, _value = token.split("=", 1)
            if _is_secret_flag(flag):
                redacted.append(f"{flag}=[redacted]")
                continue
        redacted.append(token)
        if token.startswith("-") and _is_secret_flag(token):
            redact_next = True
    return redacted


def _redact_text(value: str, secrets_to_remove: tuple[str, ...]) -> str:
    text = value
    for secret in sorted(set(secrets_to_remove), key=len, reverse=True):
        if secret:
            text = text.replace(secret, "[redacted]")
    return text


_PROFILE_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_LOG_MODULE_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")
_LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")


def _redact_sensitive_text(
    value: str,
    supplied_secrets: tuple[str, ...] = (),
) -> str:
    """Backward-compatible private alias for older desktop helpers/tests."""

    return redact_sensitive_text(value, supplied_secrets)


def _safe_metadata_text(value: Any, *, max_chars: int) -> str:
    text = _redact_sensitive_text(str(value or "")).strip()
    text = re.sub(r"[\x00-\x1f\x7f]+", " ", text)
    return text[:max_chars]


def _safe_profile_name(value: Any) -> str:
    name = str(value or "")
    return name if _PROFILE_NAME_RE.fullmatch(name) else ""


def _finite_number(value: Any) -> float | None:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _nonnegative_int(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError, OverflowError):
        return 0


def _bounded_redacted_lines(
    lines: Any,
    supplied_secrets: tuple[str, ...],
) -> tuple[list[str], bool]:
    output: list[str] = []
    remaining = MAX_COMMAND_OUTPUT_CHARS
    truncated = False
    for index, raw in enumerate(lines):
        if index >= MAX_LOG_LINE_LIMIT:
            truncated = True
            break
        line = _redact_sensitive_text(str(raw), supplied_secrets).rstrip("\r\n")
        if len(line) + 1 > remaining:
            if remaining > 1:
                output.append(line[: remaining - 1])
            truncated = True
            break
        output.append(line)
        remaining -= len(line) + 1
    return output, truncated


def _option_value(
    args: list[str],
    index: int,
    option: str,
) -> tuple[str, int]:
    token = args[index]
    prefix = option + "="
    if token.startswith(prefix):
        value = token[len(prefix) :]
        if not value:
            raise CommandRequestError(
                "slash_arguments_invalid",
                f"{option} requires a value.",
            )
        return value, index + 1
    if token == option:
        if index + 1 >= len(args):
            raise CommandRequestError(
                "slash_arguments_invalid",
                f"{option} requires a value.",
            )
        return args[index + 1], index + 2
    raise AssertionError("option parser called for a different option")


def _bounded_integer(
    raw: str,
    *,
    option: str,
    minimum: int,
    maximum: int,
) -> int:
    try:
        value = int(raw, 10)
    except ValueError as exc:
        raise CommandRequestError(
            "slash_arguments_invalid",
            f"{option} must be an integer from {minimum} to {maximum}.",
        ) from exc
    if not minimum <= value <= maximum:
        raise CommandRequestError(
            "slash_arguments_invalid",
            f"{option} must be from {minimum} to {maximum}.",
        )
    return value


def _parse_log_query(args: list[str]) -> tuple[str | None, str, int]:
    level: str | None = None
    module = ""
    limit = DEFAULT_LOG_LINE_LIMIT
    seen: set[str] = set()
    positional: list[str] = []
    index = 0
    while index < len(args):
        token = args[index]
        matched = next(
            (
                option
                for option in ("--level", "--module", "--limit")
                if token == option or token.startswith(option + "=")
            ),
            None,
        )
        if matched is None:
            if token.startswith("-"):
                raise CommandRequestError(
                    "slash_arguments_invalid",
                    "Desktop /log(s) accepts only --level, --module, and --limit.",
                )
            positional.append(token)
            index += 1
            continue
        if matched in seen:
            raise CommandRequestError(
                "slash_arguments_invalid",
                f"{matched} may be supplied only once.",
            )
        seen.add(matched)
        raw, index = _option_value(args, index, matched)
        if matched == "--level":
            level = raw.upper()
        elif matched == "--module":
            module = raw
        else:
            limit = _bounded_integer(
                raw,
                option="--limit",
                minimum=1,
                maximum=MAX_LOG_LINE_LIMIT,
            )

    if positional:
        if seen:
            raise CommandRequestError(
                "slash_arguments_invalid",
                "Do not mix positional /log filters with explicit options.",
            )
        if positional[0].upper() in _LOG_LEVELS:
            level = positional[0].upper()
            if len(positional) > 2:
                raise CommandRequestError(
                    "slash_arguments_invalid",
                    "Use `/log [LEVEL] [MODULE]` or explicit filter options.",
                )
            module = positional[1] if len(positional) == 2 else ""
        else:
            if len(positional) != 1:
                raise CommandRequestError(
                    "slash_arguments_invalid",
                    "Use `/log [LEVEL] [MODULE]` or explicit filter options.",
                )
            module = positional[0]

    if level is not None and level not in _LOG_LEVELS:
        raise CommandRequestError(
            "slash_arguments_invalid",
            f"--level must be one of {', '.join(_LOG_LEVELS)}.",
        )
    if module and not _LOG_MODULE_RE.fullmatch(module):
        raise CommandRequestError(
            "slash_arguments_invalid",
            "--module must use only letters, digits, '.', '_', ':', or '-' (max 128).",
        )
    return level, module, limit


def _parse_single_limit(
    args: list[str],
    *,
    default: int,
    maximum: int,
    command: str,
) -> int:
    if not args:
        return default
    if len(args) not in {1, 2}:
        raise CommandRequestError(
            "slash_arguments_invalid",
            f"Desktop /{command} accepts only `--limit N`.",
        )
    token = args[0]
    if token != "--limit" and not token.startswith("--limit="):
        raise CommandRequestError(
            "slash_arguments_invalid",
            f"Desktop /{command} accepts only `--limit N`.",
        )
    raw, next_index = _option_value(args, 0, "--limit")
    if next_index != len(args):
        raise CommandRequestError(
            "slash_arguments_invalid",
            f"Desktop /{command} accepts only one `--limit N` option.",
        )
    return _bounded_integer(
        raw,
        option="--limit",
        minimum=1,
        maximum=maximum,
    )


def _rollup_rows(rows: Any) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    for row in list(rows)[:MAX_ROLLUP_ROWS]:
        output.append(
            {
                "key": _safe_metadata_text(getattr(row, "key", ""), max_chars=128),
                "label": _safe_metadata_text(getattr(row, "label", ""), max_chars=256),
                "runs": _nonnegative_int(getattr(row, "runs", 0)),
                "tokens_in": _nonnegative_int(getattr(row, "tokens_in", 0)),
                "tokens_out": _nonnegative_int(getattr(row, "tokens_out", 0)),
                "tokens_total": _nonnegative_int(getattr(row, "tokens_total", 0)),
                "cost_usd": _finite_number(getattr(row, "cost_usd", None)),
            }
        )
    return output


def _trust_record_payload(record: Any) -> dict[str, Any]:
    sha = str(getattr(record, "sha256", "")).lower()
    sha_prefix = sha[:12] if re.fullmatch(r"[0-9a-f]{64}", sha) else "invalid"
    approved_at = getattr(record, "approved_at", None)
    if hasattr(approved_at, "isoformat"):
        approved_text = approved_at.isoformat()
    else:
        approved_text = ""
    tools = getattr(record, "allowed_tools", ()) or ()
    return {
        "name": _safe_metadata_text(getattr(record, "name", ""), max_chars=128),
        "sha256_prefix": sha_prefix,
        "approved_at": _safe_metadata_text(approved_text, max_chars=64) or None,
        "trust_policy": _safe_metadata_text(
            getattr(record, "trust_policy", ""), max_chars=32
        ),
        "allowed_tools": [
            _safe_metadata_text(tool, max_chars=128) for tool in list(tools)[:64]
        ],
    }


def _command_digest(argv: list[str]) -> str:
    payload = "\x00".join(argv).encode("utf-8", errors="surrogatepass")
    return hashlib.sha256(payload).hexdigest()


def _subcommands(
    parser: argparse.ArgumentParser,
) -> list[tuple[str, argparse.ArgumentParser, str]]:
    for action in parser._actions:
        if not isinstance(action, argparse._SubParsersAction):
            continue
        help_by_name = {
            choice.dest: choice.help or "" for choice in action._choices_actions
        }
        return [
            (name, child, help_by_name.get(name, ""))
            for name, child in action.choices.items()
        ]
    return []


def _walk_leaves(
    parser: argparse.ArgumentParser,
    *,
    prefix: tuple[str, ...] = (),
) -> list[tuple[tuple[str, ...], str]]:
    children = _subcommands(parser)
    if not children:
        return [(prefix, "")]
    leaves: list[tuple[tuple[str, ...], str]] = []
    for name, child, description in children:
        nested = _subcommands(child)
        if not nested:
            leaves.append(((*prefix, name), description))
            continue
        for route, nested_description in _walk_leaves(
            child,
            prefix=(*prefix, name),
        ):
            leaves.append((route, nested_description))
    return leaves


def _build_registry(parser: argparse.ArgumentParser) -> dict[str, Any]:
    top = _subcommands(parser)
    leaves = _walk_leaves(parser)
    leaf_routes = {route for route, _description in leaves}
    missing_policy = sorted(leaf_routes - set(_POLICIES))

    # Unknown future handlers fail closed in execution, but advertise that the
    # registry needs a policy rather than silently calling them read-only.
    def route_entry(route: tuple[str, ...], description: str) -> dict[str, Any]:
        policy = _POLICIES.get(
            route,
            _RoutePolicy(
                CommandRisk.PROVIDER_EXECUTION,
                "always",
                "No explicit policy is registered; execution fails closed.",
            ),
        )
        unavailable = _DESKTOP_UNAVAILABLE.get(route)
        entry = {
            "name": route[-1],
            "command_id": ".".join(route),
            "canonical_name": " ".join(route),
            "invocation": " ".join(route),
            "description": description,
            "source": "cli",
            "risk": policy.default_risk.value,
            "confirmation": policy.confirmation,
            "risk_detail": policy.detail,
            "available": route in _POLICIES and unavailable is None,
        }
        if unavailable is not None:
            scope, reason = unavailable
            entry.update({"scope": scope, "unavailable_reason": reason})
        return entry

    flat = [route_entry(route, description) for route, description in leaves]
    by_top: dict[str, list[dict[str, Any]]] = {}
    for entry in flat:
        top_name = entry["invocation"].split(" ", 1)[0]
        by_top.setdefault(top_name, []).append(entry)

    commands: list[dict[str, Any]] = []
    for name, _child, description in top:
        top_leaves = by_top.get(name, [])
        risks = {leaf["risk"] for leaf in top_leaves}
        confirmations = {leaf["confirmation"] for leaf in top_leaves}
        command = {
            "name": name,
            "command_id": name,
            "canonical_name": name,
            "invocation": name,
            "description": description,
            "source": "cli",
            "risk": next(iter(risks)) if len(risks) == 1 else "mixed",
            "confirmation": (
                next(iter(confirmations)) if len(confirmations) == 1 else "mixed"
            ),
            "available": bool(top_leaves)
            and all(leaf["available"] for leaf in top_leaves),
            "leaf_routes": top_leaves,
        }
        if len(top_leaves) == 1:
            for field in ("scope", "unavailable_reason"):
                if field in top_leaves[0]:
                    command[field] = top_leaves[0][field]
        commands.append(command)

    slash_commands = _build_slash_registry(flat)
    # Compatibility field consumed by the current command palette.  It contains
    # only reviewed slash-to-CLI adapters; the complete 44-name audit lives in
    # ``slash_commands`` and must not be inferred from equal spelling.
    aliases = [
        {
            "name": entry["name"],
            "command_id": entry["target_command_id"],
            "canonical_name": entry["target"],
            "invocation": entry["invocation"],
            "target": entry["target"],
            "description": entry["description"],
            "source": "tui_alias",
            "risk": entry["risk"],
            "available": entry["available"],
        }
        for entry in slash_commands
        if entry["classification"] == "cli"
    ]
    top_names = {name for name, _child, _description in top}
    overlap = top_names & _SLASH_NAMES
    return {
        "commands": commands,
        "leaf_routes": flat,
        "aliases": aliases,
        "slash_commands": slash_commands,
        "counts": {
            "top_level": len(top),
            "leaf_routes": len(flat),
            "aliases": len(aliases),
            "slash_commands": len(slash_commands),
            "slash_local": len(_SLASH_LOCAL),
            "slash_cli": len(_SLASH_CLI_TARGETS),
            "slash_backend": len(_SLASH_BACKEND),
            "slash_domain": len(_SLASH_DOMAIN),
            "slash_unavailable": len(_SLASH_UNAVAILABLE),
        },
        "legacy_counts": {
            "top_level": len(_LEGACY_TOP_LEVEL),
            "leaf_routes": _LEGACY_LEAF_COUNT,
        },
        "policy_complete": not missing_policy,
        "unclassified_routes": [" ".join(route) for route in missing_policy],
        "slash_audit": {
            "registered": len(_SLASH_NAMES),
            "exact_name_overlap": len(overlap),
            "slash_only": len(_SLASH_NAMES - top_names),
            "cli_only": len(top_names - _SLASH_NAMES),
            "union": len(_SLASH_NAMES | top_names),
        },
    }


def _build_slash_registry(
    cli_leaves: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    entries_by_route = {
        tuple(entry["invocation"].split()): entry for entry in cli_leaves
    }
    entries: list[dict[str, Any]] = []
    for name in sorted(_SLASH_NAMES):
        base: dict[str, Any] = {
            "name": name,
            "command_id": f"slash.{name}",
            "canonical_name": f"/{name}",
            "invocation": f"/{name}",
            "source": "tui",
        }
        if name in _SLASH_LOCAL:
            parity, description = _SLASH_LOCAL[name]
            base.update(
                {
                    "classification": "local",
                    "scope": "desktop",
                    "execution": "frontend",
                    "description": description,
                    "risk": "local",
                    "parity": parity,
                    "available": True,
                }
            )
            if name == "exit":
                base["alias_of"] = "/quit"
            elif name == "runs":
                base["alias_of"] = "/history"
            if name == "blocks":
                base["supported_forms"] = [
                    "[list]",
                    "copy <N>",
                    "save <N> <workspace-relative-path>",
                ]
            elif name in {"history", "runs"}:
                base["supported_forms"] = ["[N]"]
        elif name in _SLASH_CLI_TARGETS:
            target, description = _SLASH_CLI_TARGETS[name]
            target_text = " ".join(target)
            target_entry = entries_by_route.get(target)
            if target_entry is None:
                nested = [
                    entry
                    for route, entry in entries_by_route.items()
                    if route[: len(target)] == target
                ]
                risks = {entry["risk"] for entry in nested}
                confirmations = {entry["confirmation"] for entry in nested}
                risk = next(iter(risks)) if len(risks) == 1 else "mixed"
                confirmation = (
                    next(iter(confirmations)) if len(confirmations) == 1 else "mixed"
                )
                available = bool(nested) and all(entry["available"] for entry in nested)
            else:
                risk = target_entry["risk"]
                confirmation = target_entry["confirmation"]
                available = bool(target_entry["available"])
            base.update(
                {
                    "classification": "cli",
                    "scope": "command_bridge",
                    "execution": "command_bridge",
                    "target": target_text,
                    "target_command_id": ".".join(target),
                    "description": description,
                    "risk": risk,
                    "confirmation": confirmation,
                    "parity": "acceptable",
                    "available": available,
                }
            )
        elif name in _SLASH_BACKEND:
            base.update(
                {
                    "classification": "backend",
                    "scope": "desktop_backend",
                    "execution": "command_bridge",
                    "description": _SLASH_BACKEND[name],
                    "risk": CommandRisk.READ_ONLY.value,
                    "confirmation": "never",
                    "parity": "adapted",
                    "available": True,
                }
            )
        elif name in _SLASH_DOMAIN:
            parity, description, risk, confirmation = _SLASH_DOMAIN[name]
            base.update(
                {
                    "classification": "domain",
                    "scope": "desktop_domain",
                    "execution": "command_bridge",
                    "description": description,
                    "risk": risk,
                    "confirmation": confirmation,
                    "parity": parity,
                    "available": True,
                }
            )
            if name == "memory":
                base["supported_forms"] = [
                    "show",
                    "forget <key>",
                    "edit",
                    "edit save <base-sha256> <base64url-utf8>",
                ]
            elif name == "sandbox":
                base["supported_forms"] = ["[list]", "revoke <exact-sha256>"]
            elif name == "voice":
                base["supported_forms"] = ["status", "transcribe <workspace-audio>"]
            elif name == "deployments":
                base["supported_forms"] = [
                    "list",
                    "undeploy <name>",
                    "reload <name>",
                    "docs <name>",
                ]
            elif name == "library":
                base["supported_forms"] = ["[query]"]
            elif name == "marketplace":
                base["supported_forms"] = [
                    "[query]",
                    "install <entity-id> [--name NAME]",
                ]
            elif name == "remember":
                base["supported_forms"] = ["<note>"]
            elif name == "run":
                base["supported_forms"] = [
                    "<chain-id> [--channel NAME]",
                    "<chain-id> --execute [--input K=V] [--task TEXT] [--save-result NAME]",
                ]
            elif name == "deploy":
                base["supported_forms"] = [
                    "<chain-id|unique-name> [--channel NAME] [--name NAME]"
                ]
            elif name == "promote":
                base["supported_forms"] = [
                    "<chain-id|unique-name> [--from NAME] [--to NAME] [--force]"
                ]
            elif name == "export":
                base["supported_forms"] = ["<md|mdx|json|html> [filename]"]
            elif name == "imgpreview":
                base["supported_forms"] = ["[status|<workspace-image>]"]
            elif name == "upload":
                base["supported_forms"] = ["<exact-chain-id>"]
            elif name == "evolution":
                base["supported_forms"] = [
                    "[list|dashboard|runs]",
                    "setup|new [chain-id]",
                    "launch <chain-id> [options]",
                    "<run-id>",
                    "watch <run-id> [--limit N]",
                    "accept <run-id> <individual-id>",
                ]
            elif name == "revise":
                base["supported_forms"] = [
                    "<exact-chain-id> <instruction> [--mode fast|deep]",
                    "save <draft-id>",
                    "discard <draft-id>",
                ]
        else:
            reason, related = _SLASH_UNAVAILABLE[name]
            base.update(
                {
                    "classification": "unavailable",
                    "scope": "tui_only",
                    "execution": "unavailable",
                    "description": reason,
                    "reason": reason,
                    "risk": "unavailable",
                    "parity": "not_implemented",
                    "available": False,
                }
            )
            if related is not None:
                base["related_command"] = related
        entries.append(base)
    return entries


def _route_from_namespace(
    parser: argparse.ArgumentParser,
    namespace: argparse.Namespace,
) -> tuple[str, ...]:
    route: list[str] = []
    current = parser
    while True:
        action = next(
            (
                candidate
                for candidate in current._actions
                if isinstance(candidate, argparse._SubParsersAction)
            ),
            None,
        )
        if action is None:
            break
        selected = getattr(namespace, action.dest, None)
        if selected is None or selected not in action.choices:
            break
        route.append(selected)
        current = action.choices[selected]
    return tuple(route)


def _classify_risk(
    route: tuple[str, ...],
    namespace: argparse.Namespace,
) -> CommandRisk:
    # Any future leaf without an explicit policy is gated as provider/tool
    # execution.  This is intentionally fail-closed.
    if route not in _POLICIES:
        return CommandRisk.PROVIDER_EXECUTION
    if route == ("import",):
        return (
            CommandRisk.FILESYSTEM_MUTATION
            if namespace.apply
            else CommandRisk.READ_ONLY
        )
    if route == ("run",):
        if namespace.execute:
            return CommandRisk.PROVIDER_EXECUTION
        if (
            namespace.export is not None
            or namespace.log is not None
            or namespace.save_result
        ):
            return CommandRisk.FILESYSTEM_MUTATION
        return CommandRisk.READ_ONLY
    if route == ("forget",):
        return (
            CommandRisk.FILESYSTEM_MUTATION
            if namespace.force
            else CommandRisk.READ_ONLY
        )
    if route == ("compose",):
        return (
            CommandRisk.FILESYSTEM_MUTATION if namespace.yes else CommandRisk.READ_ONLY
        )
    if route == ("migrate-secrets",):
        return (
            CommandRisk.READ_ONLY
            if namespace.dry_run
            else CommandRisk.FILESYSTEM_MUTATION
        )
    return _POLICIES[route].default_risk


def _special_desktop_command(
    route: tuple[str, ...],
    invocation: str,
    risk: CommandRisk,
    started: float,
) -> CommandDispatch | None:
    if route == ("app",):
        message = "Maestro is already running in this desktop window.\n"
    elif route == ("tui",):
        message = (
            "TUI is a compatibility launcher; run `maestro tui` from a terminal.\n"
        )
    elif route == ("web",):
        message = (
            "The browser server is an internal developer surface. "
            "Use this Maestro window instead.\n"
        )
    else:
        return None
    stdout = _BoundedTextIO()
    stdout.write(message)
    return CommandDispatch(
        200,
        _result_payload(
            ok=True,
            invocation=invocation,
            route=route,
            risk=risk,
            exit_code=0,
            stdout=stdout,
            stderr=_BoundedTextIO(),
            duration_ms=_duration_ms(started),
            executed=False,
            secrets_to_remove=(),
        ),
    )


def _command_meta(
    invocation: str,
    route: tuple[str, ...],
    risk: CommandRisk,
) -> dict[str, Any]:
    canonical_name = " ".join(route)
    return {
        "name": canonical_name,
        "command_id": ".".join(route),
        "canonical_name": canonical_name,
        "invocation": redact_sensitive_text(invocation),
        "source": "cli",
        "risk": risk.value,
    }


def _result_payload(
    *,
    ok: bool,
    invocation: str,
    route: tuple[str, ...],
    risk: CommandRisk,
    exit_code: int,
    stdout: _BoundedTextIO,
    stderr: _BoundedTextIO,
    duration_ms: int,
    executed: bool,
    secrets_to_remove: tuple[str, ...],
    error_code: str | None = None,
) -> dict[str, Any]:
    out = redact_sensitive_text(stdout.bounded_value(), secrets_to_remove)
    err = redact_sensitive_text(stderr.bounded_value(), secrets_to_remove)
    payload: dict[str, Any] = {
        "ok": ok,
        "command": _command_meta(invocation, route, risk),
        "exit_code": exit_code,
        "stdout": out,
        "stderr": err,
        "truncated": {
            "stdout": stdout.truncated,
            "stderr": stderr.truncated,
        },
        "duration_ms": duration_ms,
        "executed": executed,
    }
    if error_code is not None:
        message = err.strip() or f"command exited with status {exit_code}"
        payload["error"] = {"code": error_code, "message": message}
    return payload


def _domain_risk(value: DomainRisk) -> CommandRisk:
    try:
        return CommandRisk(value.value)
    except (AttributeError, ValueError) as exc:
        raise CommandRequestError(
            "domain_policy_invalid",
            "The desktop domain adapter returned an invalid risk policy.",
            status=500,
        ) from exc


def _desktop_command_meta(
    name: str,
    invocation: str,
    *,
    risk: CommandRisk = CommandRisk.READ_ONLY,
    source: str = "desktop_backend",
    effects: list[str] | None = None,
) -> dict[str, Any]:
    meta: dict[str, Any] = {
        "name": f"/{name}",
        "command_id": f"slash.{name}",
        "canonical_name": f"/{name}",
        "invocation": redact_sensitive_text(invocation),
        "source": source,
        "risk": risk.value,
    }
    if effects:
        meta["effects"] = list(effects)
    return meta


def _desktop_success_dispatch(
    name: str,
    invocation: str,
    stdout_text: str,
    data: dict[str, Any],
    started: float,
) -> CommandDispatch:
    stdout = _BoundedTextIO()
    stdout.write(_redact_sensitive_text(stdout_text))
    return CommandDispatch(
        200,
        {
            "ok": True,
            "command": _desktop_command_meta(name, invocation),
            "exit_code": 0,
            "stdout": stdout.bounded_value(),
            "stderr": "",
            "truncated": {"stdout": stdout.truncated, "stderr": False},
            "duration_ms": _duration_ms(started),
            "executed": True,
            "payload": data,
        },
    )


def _desktop_domain_dispatch(
    name: str,
    invocation: str,
    result: DomainCommandResult,
    *,
    risk: CommandRisk,
    supplied_secrets: tuple[str, ...],
    started: float,
) -> CommandDispatch:
    stdout = _BoundedTextIO()
    stderr = _BoundedTextIO()
    stdout.write(redact_sensitive_text(result.stdout, supplied_secrets))
    stderr.write(redact_sensitive_text(result.stderr, supplied_secrets))
    payload: dict[str, Any] = {
        "ok": bool(result.ok),
        "command": _desktop_command_meta(
            name,
            invocation,
            risk=risk,
            source="desktop_domain",
        ),
        "exit_code": int(result.exit_code),
        "stdout": stdout.bounded_value(),
        "stderr": stderr.bounded_value(),
        "truncated": {
            "stdout": stdout.truncated,
            "stderr": stderr.truncated,
        },
        "duration_ms": _duration_ms(started),
        "executed": True,
        "payload": _copy_json_value(result.payload),
    }
    if result.ephemeral_secret is not None:
        secret = result.ephemeral_secret
        if (
            not isinstance(secret, str)
            or not 20 <= len(secret) <= 256
            or any(ord(char) < 33 or ord(char) == 127 for char in secret)
        ):
            return _desktop_error_dispatch(
                500,
                "domain_secret_invalid",
                "The command returned an invalid ephemeral credential.",
                name=name,
                invocation=invocation,
                exit_code=2,
                executed=True,
                started=started,
                risk=risk,
                source="desktop_domain",
            )
        payload["ephemeral_secret"] = {
            "kind": "credential",
            "value": secret,
            "expires_in_seconds": 60,
        }
    if not result.ok:
        message = stderr.bounded_value().strip() or (
            f"command exited with status {result.exit_code}"
        )
        payload["error"] = {
            "code": "command_failed",
            "message": redact_sensitive_text(message, supplied_secrets),
        }
    return CommandDispatch(200, payload)


def _desktop_error_dispatch(
    status: int,
    code: str,
    message: str,
    *,
    name: str,
    invocation: str,
    exit_code: int | None = None,
    executed: bool = False,
    started: float | None = None,
    risk: CommandRisk = CommandRisk.READ_ONLY,
    source: str = "desktop_backend",
) -> CommandDispatch:
    safe_message = redact_sensitive_text(
        message,
        max_chars=MAX_COMMAND_OUTPUT_CHARS,
    )
    payload: dict[str, Any] = {
        "ok": False,
        "command": _desktop_command_meta(
            name,
            invocation,
            risk=risk,
            source=source,
        ),
        "error": {"code": code, "message": safe_message},
    }
    if exit_code is not None:
        payload.update(
            {
                "exit_code": exit_code,
                "stdout": "",
                "stderr": safe_message + "\n",
                "truncated": {"stdout": False, "stderr": False},
                "duration_ms": _duration_ms(started or time.perf_counter()),
                "executed": executed,
            }
        )
    return CommandDispatch(status, payload)


def _error_dispatch(
    status: int,
    code: str,
    message: str,
    *,
    invocation: str | None = None,
) -> CommandDispatch:
    payload: dict[str, Any] = {
        "ok": False,
        "error": {"code": code, "message": redact_sensitive_text(message)},
    }
    if invocation is not None:
        payload["command"] = {
            "invocation": redact_sensitive_text(invocation),
            "source": "cli",
        }
    return CommandDispatch(status, payload)


def _exception_dispatch(
    error: BaseException,
    secrets_to_remove: tuple[str, ...],
    *,
    invocation: str | None = None,
    route: tuple[str, ...] = (),
    risk: CommandRisk = CommandRisk.READ_ONLY,
    stdout: _BoundedTextIO | None = None,
    stderr: _BoundedTextIO | None = None,
    started: float | None = None,
) -> CommandDispatch:
    out = stdout or _BoundedTextIO()
    err = stderr or _BoundedTextIO()
    safe_message = redact_sensitive_text(str(error), secrets_to_remove)
    if safe_message:
        err.write(f"command failed: {safe_message}\n")
    payload = _result_payload(
        ok=False,
        invocation=invocation or "",
        route=route,
        risk=risk,
        exit_code=2,
        stdout=out,
        stderr=err,
        duration_ms=_duration_ms(started or time.perf_counter()),
        executed=True,
        secrets_to_remove=secrets_to_remove,
        error_code="command_exception",
    )
    return CommandDispatch(200, payload)


@contextlib.contextmanager
def _redirect_stdin(stream: TextIO):
    previous = sys.stdin
    sys.stdin = stream
    try:
        yield
    finally:
        sys.stdin = previous


def _duration_ms(started: float) -> int:
    return max(0, int((time.perf_counter() - started) * 1000))


def _copy_json_value(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _copy_json_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_copy_json_value(item) for item in value]
    return value


__all__ = [
    "CommandBridge",
    "CommandDispatch",
    "CommandRisk",
    "DesktopCommandSources",
    "DEFAULT_CONFIRMATION_TTL_SECONDS",
    "MAX_COMMAND_CHARS",
    "MAX_COMMAND_OUTPUT_CHARS",
]
