"""UI-neutral native adapters for the remaining Maestro slash workflows.

The TUI implementations of transcript export, image preview, chain upload,
evolution, and natural-language revision depend on mounted Textual widgets.
This module keeps their domain contracts while removing that UI dependency so
the desktop shell can call them in-process.  It deliberately has no HTTP-server
or web-asset imports and never invokes a shell or subprocess.

Every write or external side effect is represented by a
:class:`~care.desktop_domain_commands.DomainCommandPlan`.  Its confirmation
scope hashes the exact target, relevant configuration, and source state.  The
desktop bridge re-plans after the user confirms; stale confirmation tokens then
fail closed.  Revision is explicitly two phase: a confirmed provider preview
creates a bounded, expiring draft, and a second confirmed ``save`` action writes
that exact draft as a new version.
"""

from __future__ import annotations

import asyncio
import hashlib
import html
import ipaddress
import json
import os
import re
import secrets
import tempfile
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from itertools import islice
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit, urlunsplit

from care.desktop_domain_commands import (
    DomainCommandError,
    DomainCommandPlan,
    DomainCommandResult,
    DomainRisk,
)
from care.redaction import redact_sensitive_text
from care.runtime.workspace_context import current_workspace_root


MAX_TRANSCRIPT_ROWS = 5_000
MAX_TRANSCRIPT_CHARS = 4 * 1024 * 1024
MAX_EXPORT_BYTES = 8 * 1024 * 1024
MAX_EXISTING_EXPORT_BYTES = 16 * 1024 * 1024
MAX_IMAGE_BYTES = 16 * 1024 * 1024
MAX_DATASET_BYTES = 32 * 1024 * 1024
MAX_CHAIN_BYTES = 8 * 1024 * 1024
MAX_STATE_BYTES = 8 * 1024 * 1024
MAX_UPLOAD_RESPONSE_BYTES = 4 * 1024
MAX_EVOLUTION_ROWS = 100
MAX_EVOLUTION_EVENTS = 200
MAX_RESULT_CHARS = 96 * 1024
MAX_SAFE_VALUE_CHARS = 8_000
MAX_REVISION_DRAFTS = 32
REVISION_DRAFT_TTL_SECONDS = 10 * 60.0

_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$")
_DRAFT_ID_RE = re.compile(r"^[A-Za-z0-9_-]{8,128}$")
_HEADER_RE = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]{1,128}$")
_COLUMN_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]{0,127}$")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
_EXPORT_NAME_RE = re.compile(r"^[^/\\\x00-\x1f\x7f]{1,180}$")
_FORMATS = frozenset({"md", "mdx", "json", "html"})
_IMAGE_MIME = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".bmp": "image/bmp",
}
_TERMINAL_EVOLUTION_EVENTS = frozenset(
    {"completed", "evolution_completed", "failed", "cancelled"}
)


def _default_load_config() -> Any:
    from care.config import CareConfig

    return CareConfig.load()


def _default_build_memory(config: Any) -> Any:
    from care.memory import CareMemory

    return CareMemory.from_config(config)


def _default_build_platform(config: Any) -> Any:
    from gigaevo_client import GigaEvoConfig, PlatformClient

    from care.config import resolve_platform_api_base_url
    from care.platform import CarePlatform

    # ``CarePlatform.from_config`` also runs the TUI's local-stack bootstrap,
    # which may invoke container helper processes.  A desktop slash adapter
    # must never spawn those implicitly, so construct the same facade without
    # that bootstrap side effect.  A confirmed ``start_evolution`` still owns
    # its normal registry sync immediately before submission.
    sdk_config = GigaEvoConfig(
        platform_base_url=resolve_platform_api_base_url(config.platform),
        api_key=config.platform.api_key,
        timeout=config.platform.timeout,
    )
    mutation_id = (
        getattr(config.platform, "mutation_model_id", None) or "care-mutation"
    )
    validation_id = (
        getattr(config.platform, "validation_model_id", None) or "care-validation"
    )
    return CarePlatform(
        PlatformClient.from_config(sdk_config),
        mutation_model_id=mutation_id,
        validation_model_id=validation_id,
        config=config,
    )


def _default_preview_revision(
    config: Any,
    *,
    chain_id: str,
    chain: dict[str, Any],
    instruction: str,
    channel: str,
    mode: str,
) -> Any:
    from care.generation import build_mage_generator, run_edit

    generator = build_mage_generator(config, mode=mode)
    return _run_async(
        run_edit(
            generator,
            instruction,
            entity_id=chain_id,
            chain=chain,
            channel=channel,
            save=False,
        )
    )


def _default_upload_transport(
    *,
    url: str,
    api_key: str | None,
    auth_header: str,
    timeout: float,
    body: Mapping[str, Any],
) -> tuple[int, str]:
    import httpx

    headers = {"Content-Type": "application/json"}
    if api_key:
        headers[auth_header] = (
            f"Bearer {api_key}"
            if auth_header.lower() == "authorization"
            else api_key
        )
    with httpx.Client(timeout=timeout, follow_redirects=False) as client:
        with client.stream("POST", url, json=dict(body), headers=headers) as response:
            chunks: list[bytes] = []
            consumed = 0
            for chunk in response.iter_bytes():
                remaining = MAX_UPLOAD_RESPONSE_BYTES - consumed
                if remaining <= 0:
                    break
                piece = chunk[:remaining]
                chunks.append(piece)
                consumed += len(piece)
                if len(piece) < len(chunk):
                    break
            preview = b"".join(chunks).decode("utf-8", errors="replace")
            return int(response.status_code), preview


def _default_workspace_root() -> Path:
    return current_workspace_root()


def _default_export_root() -> Path:
    return current_workspace_root() / "exports"


def _default_image_limit() -> int:
    raw = (os.environ.get("CARE_CHAT__IMAGE_REF_MAX_BYTES") or "").strip()
    try:
        value = int(raw) if raw else 1_048_576
    except ValueError:
        value = 1_048_576
    return min(MAX_IMAGE_BYTES, max(1, value))


@dataclass(frozen=True)
class DesktopRemainingSources:
    """Dependency seams used by hermetic tests and the desktop bridge."""

    load_config: Callable[[], Any] = _default_load_config
    build_memory: Callable[[Any], Any] = _default_build_memory
    build_platform: Callable[[Any], Any] = _default_build_platform
    preview_revision: Callable[..., Any] = _default_preview_revision
    upload_transport: Callable[..., tuple[int, str]] = _default_upload_transport
    workspace_root: Callable[[], Path] = _default_workspace_root
    export_root: Callable[[], Path] = _default_export_root
    image_max_bytes: Callable[[], int] = _default_image_limit
    now: Callable[[], float] = time.monotonic
    new_id: Callable[[], str] = lambda: secrets.token_urlsafe(18)


@dataclass
class _RevisionDraft:
    draft_id: str
    entity_id: str
    channel: str
    instruction: str
    name: str
    base_digest: str
    revised_digest: str
    chain: dict[str, Any]
    created_at: float
    used: bool = False


class DesktopRemainingCommands:
    """Plan the remaining TUI-domain commands for a native client.

    ``transcript`` is required only for ``/export`` and is supplied by the
    caller because the backend module intentionally owns no UI/session state.
    Other commands ignore it.
    """

    NAMES = frozenset({"export", "imgpreview", "upload", "evolution", "revise"})

    def __init__(
        self,
        sources: DesktopRemainingSources | None = None,
        *,
        max_revision_drafts: int = MAX_REVISION_DRAFTS,
        revision_draft_ttl: float = REVISION_DRAFT_TTL_SECONDS,
    ) -> None:
        self._sources = sources or DesktopRemainingSources()
        self._drafts: dict[str, _RevisionDraft] = {}
        self._draft_lock = threading.RLock()
        self._max_revision_drafts = max(1, int(max_revision_drafts))
        self._draft_ttl = max(1.0, float(revision_draft_ttl))

    def plan(
        self,
        name: str,
        args: Sequence[str],
        *,
        transcript: Sequence[Mapping[str, Any]] | None = None,
    ) -> DomainCommandPlan:
        """Strictly parse one slash command and return an auditable plan."""

        command = str(name or "").strip().lower()
        tokens = [str(item) for item in args]
        if command not in self.NAMES:
            raise DomainCommandError(
                "slash_command_unavailable",
                f"Desktop /{command or '?'} has no reviewed remaining-command adapter.",
                status=501,
            )
        try:
            if command == "export":
                return self._plan_export(tokens, transcript)
            if command == "imgpreview":
                return self._plan_imgpreview(tokens)
            if command == "upload":
                return self._plan_upload(tokens)
            if command == "evolution":
                return self._plan_evolution(tokens)
            return self._plan_revise(tokens)
        except DomainCommandError:
            raise
        except Exception as exc:  # noqa: BLE001 - never reflect source details
            raise DomainCommandError(
                "domain_unavailable",
                f"Desktop /{command} data is temporarily unavailable.",
                status=503,
            ) from exc

    # ------------------------------------------------------------------
    # /export
    # ------------------------------------------------------------------
    def _plan_export(
        self,
        args: list[str],
        transcript: Sequence[Mapping[str, Any]] | None,
    ) -> DomainCommandPlan:
        if not args or len(args) > 2:
            raise DomainCommandError(
                "slash_arguments_invalid",
                "Usage: /export <md|mdx|json|html> [filename].",
            )
        fmt = args[0].lower().lstrip(".")
        if fmt not in _FORMATS:
            raise DomainCommandError(
                "slash_arguments_invalid",
                "Export format must be md, mdx, json, or html.",
            )
        rows = _normalise_transcript(transcript)
        filename = args[1] if len(args) == 2 else f"maestro-transcript.{fmt}"
        filename = _export_filename(filename, fmt)
        workspace, export_root = self._safe_roots()
        target = export_root / filename
        relative_target = target.relative_to(workspace).as_posix()
        body = _render_transcript(rows, fmt)
        encoded = body.encode("utf-8")
        if len(encoded) > MAX_EXPORT_BYTES:
            raise DomainCommandError(
                "export_too_large",
                f"Rendered transcript exceeds the {MAX_EXPORT_BYTES}-byte export cap.",
                status=413,
            )
        initial_state = _export_target_state(target)
        body_digest = hashlib.sha256(encoded).hexdigest()
        preview = {
            "action": "export_transcript",
            "format": fmt,
            "filename": filename,
            "line_count": len(rows),
            "size_bytes": len(encoded),
            "overwrites_existing": initial_state[0] == "file",
        }

        def execute() -> DomainCommandResult:
            current_workspace, current_root = self._safe_roots()
            current_target = current_root / filename
            if (
                current_target != target
                or current_target.relative_to(current_workspace).as_posix()
                != relative_target
            ):
                raise _state_changed("The configured export directory changed.")
            if _export_target_state(current_target) != initial_state:
                raise _state_changed("The export target changed while confirmation was open.")
            try:
                _atomic_export_write(
                    current_root,
                    current_target,
                    encoded,
                    expected_state=initial_state,
                )
            except DomainCommandError:
                raise
            except OSError as exc:
                raise _operation_failed("Transcript export failed.", exc) from exc
            payload = {
                **preview,
                "written": True,
                "sha256": body_digest,
                "relative_path": relative_target,
            }
            return _result(
                f"Exported {len(rows)} transcript lines to {relative_target}.\n",
                payload,
            )

        return _mutation_plan(
            "export",
            DomainRisk.FILESYSTEM_MUTATION,
            "This will write a redacted transcript file inside the workspace exports directory.",
            (
                f"{'Replace' if initial_state[0] == 'file' else 'Create'} {relative_target}",
            ),
            {
                "action": "export",
                "filename": filename,
                "format": fmt,
                "body_sha256": body_digest,
                "target_state": initial_state,
            },
            preview,
            execute,
        )

    def _safe_roots(self) -> tuple[Path, Path]:
        workspace = Path(self._sources.workspace_root()).expanduser().resolve()
        export_root = Path(self._sources.export_root()).expanduser().resolve(
            strict=False
        )
        if not _is_within(export_root, workspace):
            raise DomainCommandError(
                "unsafe_export_directory",
                "The configured export directory must be inside the workspace.",
            )
        return workspace, export_root

    # ------------------------------------------------------------------
    # /imgpreview
    # ------------------------------------------------------------------
    def _plan_imgpreview(self, args: list[str]) -> DomainCommandPlan:
        if not args or args == ["status"]:
            if len(args) > 1:
                raise DomainCommandError(
                    "slash_arguments_invalid",
                    "Usage: /imgpreview [status|<workspace-image>].",
                )

            def execute_status() -> DomainCommandResult:
                return _result(
                    "Native image preview is available for workspace images.\n",
                    {
                        "native_preview": True,
                        "terminal_protocol_required": False,
                        "supported_mime_types": sorted(set(_IMAGE_MIME.values())),
                        "max_bytes": min(
                            MAX_IMAGE_BYTES,
                            max(1, int(self._sources.image_max_bytes())),
                        ),
                    },
                )

            return _read_plan(
                "imgpreview",
                {"action": "status", "native_preview": True},
                execute_status,
            )
        if len(args) != 1:
            raise DomainCommandError(
                "slash_arguments_invalid",
                "Quote image paths containing spaces as one argument.",
            )
        workspace = Path(self._sources.workspace_root()).expanduser().resolve()
        descriptor = _image_descriptor(
            workspace,
            args[0],
            max_bytes=min(
                MAX_IMAGE_BYTES,
                max(1, int(self._sources.image_max_bytes())),
            ),
        )

        def execute_image() -> DomainCommandResult:
            current_root = Path(self._sources.workspace_root()).expanduser().resolve()
            current = _image_descriptor(
                current_root,
                args[0],
                max_bytes=min(
                    MAX_IMAGE_BYTES,
                    max(1, int(self._sources.image_max_bytes())),
                ),
            )
            if current["sha256"] != descriptor["sha256"]:
                raise _state_changed("The image changed before it could be opened.")
            return _result(
                f"Image ready: {current['relative_path']} ({current['size_bytes']} bytes).\n",
                current,
            )

        return _read_plan(
            "imgpreview",
            {
                "action": "preview",
                "relative_path": descriptor["relative_path"],
                "mime_type": descriptor["mime_type"],
                "size_bytes": descriptor["size_bytes"],
            },
            execute_image,
        )

    # ------------------------------------------------------------------
    # /upload
    # ------------------------------------------------------------------
    def _plan_upload(self, args: list[str]) -> DomainCommandPlan:
        if len(args) != 1:
            raise DomainCommandError(
                "slash_arguments_invalid",
                "Usage: /upload <exact-chain-id>.",
            )
        chain_id = _identifier(args[0], label="Chain id")
        config = self._sources.load_config()
        _require_service_endpoint(config, "memory", label="Memory URL")
        upload = _upload_settings(config)
        memory = self._sources.build_memory(config)
        chain = _get_chain(memory, chain_id)
        chain_digest = _validated_chain_digest(chain)
        config_digest = _fingerprint(
            {
                "upload": upload["fingerprint"],
                "memory": _field(config, "memory", None),
            }
        )
        endpoint = upload["url"]
        preview = {
            "action": "upload_chain",
            "chain_id": chain_id,
            "endpoint": endpoint,
            "auth_configured": bool(upload["api_key"]),
            "payload_sha256": chain_digest,
        }

        def execute() -> DomainCommandResult:
            current_config = self._sources.load_config()
            _require_service_endpoint(current_config, "memory", label="Memory URL")
            current_upload = _upload_settings(current_config)
            current_config_digest = _fingerprint(
                {
                    "upload": current_upload["fingerprint"],
                    "memory": _field(current_config, "memory", None),
                }
            )
            if current_config_digest != config_digest:
                raise _state_changed("Upload configuration changed while confirmation was open.")
            current_memory = self._sources.build_memory(current_config)
            current_chain = _get_chain(current_memory, chain_id)
            if _validated_chain_digest(current_chain) != chain_digest:
                raise _state_changed("The saved chain changed while confirmation was open.")
            body = {"chain_id": chain_id, "chain": current_chain}
            try:
                status, response_text = self._sources.upload_transport(
                    url=current_upload["url"],
                    api_key=current_upload["api_key"],
                    auth_header=current_upload["auth_header"],
                    timeout=current_upload["timeout"],
                    body=body,
                )
            except Exception as exc:  # noqa: BLE001
                raise _operation_failed("Chain upload failed.", exc) from exc
            response_preview = redact_sensitive_text(
                response_text,
                (current_upload["api_key"] or "",),
                max_chars=500,
            )
            ok = 200 <= int(status) < 300
            payload = {
                **preview,
                "uploaded": ok,
                "status": int(status),
                "response_preview": response_preview,
            }
            if not ok:
                return _result(
                    f"Upload rejected with HTTP {int(status)}.\n",
                    payload,
                    ok=False,
                    exit_code=1,
                    stderr=f"Upload rejected with HTTP {int(status)}.\n",
                )
            return _result(f"Uploaded chain {chain_id} (HTTP {int(status)}).\n", payload)

        return _mutation_plan(
            "upload",
            DomainRisk.EXTERNAL_SIDE_EFFECT,
            "This will send one exact saved chain to the configured upload endpoint.",
            (f"POST chain {chain_id} to {endpoint}",),
            {
                "action": "upload",
                "chain_id": chain_id,
                "chain_sha256": chain_digest,
                "upload_config_sha256": config_digest,
            },
            preview,
            execute,
        )

    # ------------------------------------------------------------------
    # /evolution
    # ------------------------------------------------------------------
    def _plan_evolution(self, args: list[str]) -> DomainCommandPlan:
        if not args:
            return self._evolution_intro()
        verb = args[0].lower()
        if verb in {"list", "dashboard", "runs"}:
            if len(args) != 1:
                raise DomainCommandError(
                    "slash_arguments_invalid",
                    "Desktop /evolution list accepts no additional arguments.",
                )
            return self._evolution_list()
        if verb in {"setup", "new"}:
            return self._evolution_setup(args[1:])
        if verb == "launch":
            return self._evolution_launch(args[1:])
        if verb == "watch":
            return self._evolution_watch(args[1:])
        if verb == "accept":
            return self._evolution_accept(args[1:])
        if len(args) != 1:
            raise DomainCommandError(
                "slash_arguments_invalid",
                "Usage: /evolution [list|setup|launch|watch|accept|<run-id>].",
            )
        return self._evolution_snapshot(_identifier(args[0], label="Evolution id"))

    def _evolution_intro(self) -> DomainCommandPlan:
        preview = {
            "action": "intro",
            "forms": ["list", "setup", "launch", "watch", "accept", "snapshot"],
        }
        return _read_plan(
            "evolution",
            preview,
            lambda: _result(
                "Evolution can compare chain variants against evaluation data. "
                "Use /evolution setup, /evolution list, or /evolution <run-id>.\n",
                preview,
            ),
        )

    def _evolution_list(self) -> DomainCommandPlan:
        def execute() -> DomainCommandResult:
            config = self._sources.load_config()
            _require_service_endpoint(config, "platform", label="Platform URL")
            platform = self._sources.build_platform(config)
            try:
                envelope = platform.list_evolutions(limit=MAX_EVOLUTION_ROWS)
            except TypeError:
                envelope = platform.list_evolutions()
            except Exception as exc:  # noqa: BLE001
                raise _operation_failed("Could not list evolution runs.", exc) from exc
            rows = _evolution_rows(envelope)
            lines = ["Evolution runs:"]
            lines.extend(
                f"- {row.get('id') or '?'} | {row.get('status') or 'unknown'}"
                for row in rows
            )
            if not rows:
                lines = ["No evolution runs found."]
            return _result(
                "\n".join(lines) + "\n",
                {
                    "runs": rows,
                    "count": len(rows),
                    "truncated": _raw_evolution_count(envelope) > MAX_EVOLUTION_ROWS,
                },
            )

        return _read_plan("evolution", {"action": "list"}, execute)

    def _evolution_setup(self, args: list[str]) -> DomainCommandPlan:
        if len(args) > 1:
            raise DomainCommandError(
                "slash_arguments_invalid",
                "Usage: /evolution setup [exact-chain-id].",
            )
        chain_id = _identifier(args[0], label="Chain id") if args else None
        chain_summary: dict[str, Any] | None = None
        if chain_id:
            config = self._sources.load_config()
            _require_service_endpoint(config, "memory", label="Memory URL")
            memory = self._sources.build_memory(config)
            chain = _get_chain(memory, chain_id)
            chain_summary = {
                "chain_id": chain_id,
                "name": _safe_text(chain.get("name") or chain_id, max_chars=256),
                "steps": len(chain.get("steps") or []),
                "sha256": _validated_chain_digest(chain),
            }
        form = {
            "action": "setup",
            "selected_chain": chain_summary,
            "defaults": {
                "mode": "full_chain",
                "iterations": 5,
                "population": 8,
                "validation_type": "Continuous (0..1)",
                "continuous_metric": "ROUGE-L",
                "binary_method": "equality",
                "target_column": "expected",
            },
            "launch_usage": (
                "/evolution launch <chain-id> [--mode full_chain|per_step] "
                "[--iterations N] [--population N] [--criteria TEXT] "
                "[--data workspace.csv|workspace.jsonl]"
            ),
        }
        return _read_plan(
            "evolution",
            form,
            lambda: _result("Evolution setup is ready for the native form.\n", form),
        )

    def _evolution_snapshot(self, run_id: str) -> DomainCommandPlan:
        def execute() -> DomainCommandResult:
            config = self._sources.load_config()
            _require_service_endpoint(config, "platform", label="Platform URL")
            platform = self._sources.build_platform(config)
            try:
                state = platform.get_evolution(run_id)
            except Exception as exc:  # noqa: BLE001
                raise _operation_failed("Could not fetch the evolution run.", exc) from exc
            safe_state = _safe_value(state)
            status = (
                safe_state.get("status", "unknown")
                if isinstance(safe_state, Mapping)
                else "unknown"
            )
            return _result(
                f"Evolution {run_id} — status: {status}.\n",
                {"run_id": run_id, "state": safe_state},
            )

        return _read_plan(
            "evolution",
            {"action": "snapshot", "run_id": run_id},
            execute,
        )

    def _evolution_watch(self, args: list[str]) -> DomainCommandPlan:
        if not args:
            raise DomainCommandError(
                "slash_arguments_invalid",
                "Usage: /evolution watch <run-id> [--limit N].",
            )
        run_id = _identifier(args[0], label="Evolution id")
        limit = 50
        if len(args) > 1:
            if len(args) != 3 or args[1] != "--limit":
                raise DomainCommandError(
                    "slash_arguments_invalid",
                    "Usage: /evolution watch <run-id> [--limit N].",
                )
            limit = _bounded_int(
                args[2],
                label="Event limit",
                minimum=1,
                maximum=MAX_EVOLUTION_EVENTS,
            )

        def execute() -> DomainCommandResult:
            config = self._sources.load_config()
            _require_service_endpoint(config, "platform", label="Platform URL")
            platform = self._sources.build_platform(config)
            iterator: Any = None
            try:
                iterator = iter(platform.stream_events(run_id))
                events: list[Any] = []
                terminal = False
                for event in iterator:
                    events.append(_safe_value(event))
                    kind = _event_kind(event)
                    if kind in _TERMINAL_EVOLUTION_EVENTS:
                        terminal = True
                        break
                    if len(events) >= limit:
                        break
            except Exception as exc:  # noqa: BLE001
                raise _operation_failed("Evolution event stream failed.", exc) from exc
            finally:
                close = getattr(iterator, "close", None)
                if callable(close):
                    try:
                        close()
                    except Exception:  # noqa: BLE001 - transport cleanup only
                        pass
            return _result(
                f"Read {len(events)} events from evolution {run_id}.\n",
                {
                    "run_id": run_id,
                    "events": events,
                    "count": len(events),
                    "terminal": terminal,
                    "batch_limit": limit,
                    "streaming": False,
                },
            )

        return _read_plan(
            "evolution",
            {"action": "watch_batch", "run_id": run_id, "limit": limit},
            execute,
        )

    def _evolution_launch(self, args: list[str]) -> DomainCommandPlan:
        spec = self._parse_evolution_launch(args)
        config = self._sources.load_config()
        _require_service_endpoint(config, "memory", label="Memory URL")
        _require_service_endpoint(config, "platform", label="Platform URL")
        memory = self._sources.build_memory(config)
        chain = _get_chain(memory, spec["chain_id"])
        chain_digest = _validated_chain_digest(chain)
        service_digest = _service_config_digest(config, include_memory=True)
        data_state = _dataset_state(spec.get("data_path"))
        request_preview = {
            key: value
            for key, value in spec.items()
            if key not in {"data_path"}
        }
        if spec.get("data_path") is not None:
            request_preview["data_file"] = spec["data_path"].name
        preview = {
            "action": "launch",
            **request_preview,
            "chain_sha256": chain_digest,
            "dataset_sha256": data_state[1] if data_state else None,
        }

        def execute() -> DomainCommandResult:
            current_config = self._sources.load_config()
            _require_service_endpoint(current_config, "memory", label="Memory URL")
            _require_service_endpoint(current_config, "platform", label="Platform URL")
            if (
                _service_config_digest(current_config, include_memory=True)
                != service_digest
            ):
                raise _state_changed("Evolution or Memory configuration changed.")
            current_memory = self._sources.build_memory(current_config)
            current_chain = _get_chain(current_memory, spec["chain_id"])
            if _validated_chain_digest(current_chain) != chain_digest:
                raise _state_changed("The seed chain changed while confirmation was open.")
            if _dataset_state(spec.get("data_path")) != data_state:
                raise _state_changed("The evaluation dataset changed while confirmation was open.")
            platform = self._sources.build_platform(current_config)
            kwargs = {
                "base_chain_id": spec["chain_id"],
                "evolution_mode": spec["mode"],
                "max_iterations": spec["iterations"],
                "population_size": spec["population"],
                "validation_criteria": spec["criteria"] or None,
                "test_data_path": (
                    str(spec["data_path"]) if spec.get("data_path") else None
                ),
                "target_column": spec["target_column"],
                "validation_type": spec["validation_type"],
                "continuous_metric": spec["continuous_metric"],
                "binary_method": spec["binary_method"],
                "objectives": list(spec["objectives"]),
                "base_chain_content": current_chain,
                "mutation_max_tokens": spec["mutation_max_tokens"],
            }
            try:
                ref = platform.start_evolution(**kwargs)
            except Exception as exc:  # noqa: BLE001
                raise _operation_failed("Evolution launch failed.", exc) from exc
            run_id = _identifier(
                _field(ref, "evolution_id", None) or _field(ref, "id", ""),
                label="Returned evolution id",
            )
            status = _safe_text(_field(ref, "status", "queued"), max_chars=64)
            return _result(
                f"Evolution {run_id} started ({status or 'queued'}).\n",
                {
                    **preview,
                    "started": True,
                    "run_id": run_id,
                    "status": status or "queued",
                },
            )

        return _mutation_plan(
            "evolution",
            DomainRisk.EXTERNAL_SIDE_EFFECT,
            "This will submit a new evolution run to the configured Platform.",
            (
                f"Launch evolution for chain {spec['chain_id']} "
                f"({spec['iterations']} iterations x {spec['population']} individuals)",
            ),
            {
                "action": "evolution_launch",
                "request": request_preview,
                "chain_sha256": chain_digest,
                "dataset_state": data_state,
                "service_config_sha256": service_digest,
            },
            preview,
            execute,
        )

    def _parse_evolution_launch(self, args: list[str]) -> dict[str, Any]:
        if not args:
            raise DomainCommandError(
                "slash_arguments_invalid",
                "Usage: /evolution launch <exact-chain-id> [options].",
            )
        chain_id = _identifier(args[0], label="Chain id")
        values: dict[str, Any] = {
            "chain_id": chain_id,
            "mode": "full_chain",
            "iterations": 5,
            "population": 8,
            "criteria": "",
            "data_path": None,
            "target_column": "expected",
            "validation_type": "Continuous (0..1)",
            "continuous_metric": "ROUGE-L",
            "binary_method": "equality",
            "objectives": (),
            "mutation_max_tokens": None,
        }
        options: dict[str, str] = {}
        objectives: list[str] = []
        index = 1
        allowed = {
            "--mode",
            "--iterations",
            "--population",
            "--criteria",
            "--data",
            "--target-column",
            "--validation-type",
            "--metric",
            "--binary-method",
            "--objective",
            "--mutation-max-tokens",
        }
        while index < len(args):
            option = args[index]
            if option not in allowed or index + 1 >= len(args):
                raise DomainCommandError(
                    "slash_arguments_invalid",
                    "Unknown or incomplete /evolution launch option.",
                )
            value = args[index + 1]
            if option == "--objective":
                objectives.append(_public_phrase(value, label="Objective", max_chars=200))
            elif option in options:
                raise DomainCommandError(
                    "slash_arguments_invalid",
                    f"{option} may be supplied only once.",
                )
            else:
                options[option] = value
            index += 2
        if "--mode" in options:
            mode = options["--mode"].lower()
            if mode not in {"full_chain", "per_step"}:
                raise DomainCommandError(
                    "slash_arguments_invalid",
                    "Evolution mode must be full_chain or per_step.",
                )
            values["mode"] = mode
        if "--iterations" in options:
            values["iterations"] = _bounded_int(
                options["--iterations"], label="Iterations", minimum=1, maximum=1_000
            )
        if "--population" in options:
            values["population"] = _bounded_int(
                options["--population"], label="Population", minimum=2, maximum=1_000
            )
        if "--criteria" in options:
            values["criteria"] = _public_phrase(
                options["--criteria"], label="Validation criteria", max_chars=4_000
            )
        if "--target-column" in options:
            column = options["--target-column"]
            if not _COLUMN_RE.fullmatch(column):
                raise DomainCommandError(
                    "slash_arguments_invalid",
                    "Target column has an invalid name.",
                )
            values["target_column"] = column
        if "--validation-type" in options:
            kind = options["--validation-type"].lower()
            aliases = {
                "continuous": "Continuous (0..1)",
                "continuous (0..1)": "Continuous (0..1)",
                "binary": "Binary (0/1)",
                "binary (0/1)": "Binary (0/1)",
            }
            if kind not in aliases:
                raise DomainCommandError(
                    "slash_arguments_invalid",
                    "Validation type must be continuous or binary.",
                )
            values["validation_type"] = aliases[kind]
        if "--metric" in options:
            values["continuous_metric"] = _public_phrase(
                options["--metric"], label="Metric", max_chars=64
            )
        if "--binary-method" in options:
            method = options["--binary-method"].lower()
            if method not in {"equality", "substring", "regexp"}:
                raise DomainCommandError(
                    "slash_arguments_invalid",
                    "Binary method must be equality, substring, or regexp.",
                )
            values["binary_method"] = method
        if "--mutation-max-tokens" in options:
            values["mutation_max_tokens"] = _bounded_int(
                options["--mutation-max-tokens"],
                label="Mutation max tokens",
                minimum=1,
                maximum=1_000_000,
            )
        if objectives:
            values["objectives"] = tuple(objectives[:16])
        if "--data" in options:
            workspace = Path(self._sources.workspace_root()).expanduser().resolve()
            values["data_path"] = _workspace_file(
                workspace,
                options["--data"],
                suffixes={".csv", ".jsonl"},
                max_bytes=MAX_DATASET_BYTES,
                label="Evaluation dataset",
            )
        return values

    def _evolution_accept(self, args: list[str]) -> DomainCommandPlan:
        if len(args) != 2:
            raise DomainCommandError(
                "slash_arguments_invalid",
                "Usage: /evolution accept <run-id> <individual-id>.",
            )
        run_id = _identifier(args[0], label="Evolution id")
        individual_id = _identifier(args[1], label="Individual id")
        config = self._sources.load_config()
        _require_service_endpoint(config, "memory", label="Memory URL")
        _require_service_endpoint(config, "platform", label="Platform URL")
        platform = self._sources.build_platform(config)
        try:
            state = platform.get_evolution(run_id)
        except Exception as exc:  # noqa: BLE001
            raise _operation_failed("Could not inspect the evolution run.", exc) from exc
        state_digest = _exact_json_digest(
            state, max_bytes=MAX_STATE_BYTES, label="Evolution state"
        )
        service_digest = _service_config_digest(config, include_memory=True)
        preview = {
            "action": "accept",
            "run_id": run_id,
            "individual_id": individual_id,
            "run_status": _safe_text(_field(state, "status", "unknown"), max_chars=64),
            "run_state_sha256": state_digest,
        }

        def execute() -> DomainCommandResult:
            current_config = self._sources.load_config()
            _require_service_endpoint(current_config, "memory", label="Memory URL")
            _require_service_endpoint(current_config, "platform", label="Platform URL")
            if (
                _service_config_digest(current_config, include_memory=True)
                != service_digest
            ):
                raise _state_changed("Evolution or Memory configuration changed.")
            current_platform = self._sources.build_platform(current_config)
            try:
                current_state = current_platform.get_evolution(run_id)
            except Exception as exc:  # noqa: BLE001
                raise _operation_failed("Could not re-check the evolution run.", exc) from exc
            if (
                _exact_json_digest(
                    current_state,
                    max_bytes=MAX_STATE_BYTES,
                    label="Evolution state",
                )
                != state_digest
            ):
                raise _state_changed("The evolution run changed while confirmation was open.")
            memory = self._sources.build_memory(current_config)
            try:
                try:
                    response = current_platform.accept_individual(
                        run_id, individual_id, memory=memory
                    )
                except TypeError:
                    response = current_platform.accept_individual(run_id, individual_id)
            except Exception as exc:  # noqa: BLE001
                raise _operation_failed("Evolution acceptance failed.", exc) from exc
            safe_response = _safe_value(response)
            return _result(
                f"Accepted individual {individual_id} from evolution {run_id}.\n",
                {**preview, "accepted": True, "result": safe_response},
            )

        return _mutation_plan(
            "evolution",
            DomainRisk.EXTERNAL_SIDE_EFFECT,
            "This will promote the selected evolution result to the stable chain channel.",
            (f"Accept individual {individual_id} from evolution {run_id}",),
            {
                "action": "evolution_accept",
                "run_id": run_id,
                "individual_id": individual_id,
                "run_state_sha256": state_digest,
                "service_config_sha256": service_digest,
            },
            preview,
            execute,
        )

    # ------------------------------------------------------------------
    # /revise
    # ------------------------------------------------------------------
    def _plan_revise(self, args: list[str]) -> DomainCommandPlan:
        if args and args[0].lower() == "save":
            return self._plan_revise_save(args[1:])
        if args and args[0].lower() == "discard":
            return self._plan_revise_discard(args[1:])
        return self._plan_revise_preview(args)

    def _plan_revise_preview(self, args: list[str]) -> DomainCommandPlan:
        if len(args) < 2:
            raise DomainCommandError(
                "slash_arguments_invalid",
                "Usage: /revise <exact-chain-id> <instruction> [--mode fast|deep].",
            )
        mode = "fast"
        if len(args) >= 2 and args[-2:] in (["--mode", "fast"], ["--mode", "deep"]):
            mode = args[-1]
            args = args[:-2]
        elif "--mode" in args:
            raise DomainCommandError(
                "slash_arguments_invalid",
                "Revision mode must be supplied once as --mode fast or --mode deep.",
            )
        if len(args) < 2:
            raise DomainCommandError(
                "slash_arguments_invalid",
                "A revision instruction is required.",
            )
        chain_id = _revision_entity_id(args[0])
        instruction = _public_phrase(
            " ".join(args[1:]), label="Revision instruction", max_chars=8_000
        )
        channel = "latest"
        config = self._sources.load_config()
        _require_service_endpoint(config, "memory", label="Memory URL")
        _require_service_endpoint(config, "mage", label="Model provider URL")
        memory = self._sources.build_memory(config)
        chain = _get_chain(memory, chain_id, channel=channel)
        base_digest = _validated_chain_digest(chain)
        provider_digest = _provider_config_digest(config, mode=mode)
        preview = {
            "action": "preview_revision",
            "chain_id": chain_id,
            "channel": channel,
            "mode": mode,
            "instruction": instruction,
            "base_sha256": base_digest,
        }

        def execute() -> DomainCommandResult:
            current_config = self._sources.load_config()
            _require_service_endpoint(current_config, "memory", label="Memory URL")
            _require_service_endpoint(
                current_config, "mage", label="Model provider URL"
            )
            if _provider_config_digest(current_config, mode=mode) != provider_digest:
                raise _state_changed("The model configuration changed while confirmation was open.")
            current_memory = self._sources.build_memory(current_config)
            current_chain = _get_chain(current_memory, chain_id, channel=channel)
            if _validated_chain_digest(current_chain) != base_digest:
                raise _state_changed("The chain changed while confirmation was open.")
            try:
                result = self._sources.preview_revision(
                    current_config,
                    chain_id=chain_id,
                    chain=current_chain,
                    instruction=instruction,
                    channel=channel,
                    mode=mode,
                )
            except Exception as exc:  # noqa: BLE001
                raise _operation_failed("Revision preview failed.", exc) from exc
            if bool(_field(result, "needs_disambiguation", False)):
                raise DomainCommandError(
                    "revision_target_ambiguous",
                    "The revision target was ambiguous; use one exact chain id.",
                    status=409,
                )
            revised = _field(result, "chain_dict", None)
            before = _field(result, "before_chain_dict", None) or current_chain
            if not isinstance(revised, dict) or not revised:
                return _result(
                    "The model proposed no chain changes.\n",
                    {**preview, "changed": False, "draft_id": None},
                )
            revised_digest = _validated_chain_digest(revised)
            if revised_digest == _validated_chain_digest(before):
                return _result(
                    "The model proposed no chain changes.\n",
                    {**preview, "changed": False, "draft_id": None},
                )
            draft_id = self._store_revision_draft(
                entity_id=chain_id,
                channel=channel,
                instruction=instruction,
                name=_safe_text(revised.get("name") or chain_id, max_chars=256),
                base_digest=base_digest,
                revised_digest=revised_digest,
                chain=dict(revised),
            )
            plan_lines = _revision_plan_lines(result, before, revised)
            safe_before = _bounded_projection(before, max_chars=24_000)
            safe_after = _bounded_projection(revised, max_chars=24_000)
            payload = {
                **preview,
                "changed": True,
                "draft_id": draft_id,
                "draft_expires_in_seconds": int(self._draft_ttl),
                "summary": plan_lines,
                "before": safe_before,
                "after": safe_after,
                "after_sha256": revised_digest,
                "save_command": f"/revise save {draft_id}",
            }
            return _result(
                "Revision preview ready. Review it, then confirm the separate "
                f"save action with /revise save {draft_id}.\n",
                payload,
            )

        return _mutation_plan(
            "revise",
            DomainRisk.EXTERNAL_SIDE_EFFECT,
            "This will send the selected chain and instruction to the configured model provider to build a preview. It will not save changes.",
            (f"Generate a revision preview for chain {chain_id}",),
            {
                "action": "revise_preview",
                "chain_id": chain_id,
                "channel": channel,
                "mode": mode,
                "instruction_sha256": hashlib.sha256(
                    instruction.encode("utf-8")
                ).hexdigest(),
                "base_sha256": base_digest,
                "provider_config_sha256": provider_digest,
            },
            preview,
            execute,
        )

    def _plan_revise_save(self, args: list[str]) -> DomainCommandPlan:
        if len(args) != 1 or not _DRAFT_ID_RE.fullmatch(args[0]):
            raise DomainCommandError(
                "slash_arguments_invalid",
                "Usage: /revise save <draft-id>.",
            )
        draft = self._get_revision_draft(args[0])
        config = self._sources.load_config()
        _require_service_endpoint(config, "memory", label="Memory URL")
        memory_digest = _fingerprint(_field(config, "memory", None))
        memory = self._sources.build_memory(config)
        current_chain = _get_chain(memory, draft.entity_id, channel=draft.channel)
        if _validated_chain_digest(current_chain) != draft.base_digest:
            raise _state_changed("The base chain changed after the revision preview.")
        preview = {
            "action": "save_revision",
            "draft_id": draft.draft_id,
            "chain_id": draft.entity_id,
            "channel": draft.channel,
            "before_sha256": draft.base_digest,
            "after_sha256": draft.revised_digest,
            "change_summary": f"revise: {draft.instruction}"[:200],
        }

        def execute() -> DomainCommandResult:
            claimed = self._claim_revision_draft(draft.draft_id)
            try:
                current_config = self._sources.load_config()
                _require_service_endpoint(
                    current_config, "memory", label="Memory URL"
                )
                if _fingerprint(_field(current_config, "memory", None)) != memory_digest:
                    raise _state_changed("Memory configuration changed while confirmation was open.")
                current_memory = self._sources.build_memory(current_config)
                latest = _get_chain(
                    current_memory, claimed.entity_id, channel=claimed.channel
                )
                if _validated_chain_digest(latest) != claimed.base_digest:
                    raise _state_changed("The base chain changed while confirmation was open.")
                try:
                    saved_id = current_memory.save_chain(
                        claimed.chain,
                        name=claimed.name or claimed.entity_id,
                        entity_id=claimed.entity_id,
                        channel=claimed.channel,
                        change_summary=f"revise: {claimed.instruction}"[:200],
                    )
                except Exception as exc:  # noqa: BLE001
                    raise _operation_failed("Saving the revision failed.", exc) from exc
            except Exception:
                self._release_revision_draft(claimed.draft_id)
                raise
            self._finish_revision_draft(claimed.draft_id)
            safe_saved_id = _safe_text(saved_id or claimed.entity_id, max_chars=256)
            return _result(
                f"Saved revised chain {safe_saved_id}.\n",
                {**preview, "saved": True, "saved_entity_id": safe_saved_id},
            )

        return _mutation_plan(
            "revise",
            DomainRisk.FILESYSTEM_MUTATION,
            "This will save the reviewed revision as a new version of the selected chain.",
            (f"Save one new {draft.channel} version of chain {draft.entity_id}",),
            {
                "action": "revise_save",
                "draft_id": draft.draft_id,
                "entity_id": draft.entity_id,
                "channel": draft.channel,
                "base_sha256": draft.base_digest,
                "revised_sha256": draft.revised_digest,
                "memory_config_sha256": memory_digest,
            },
            preview,
            execute,
        )

    def _plan_revise_discard(self, args: list[str]) -> DomainCommandPlan:
        if len(args) != 1 or not _DRAFT_ID_RE.fullmatch(args[0]):
            raise DomainCommandError(
                "slash_arguments_invalid",
                "Usage: /revise discard <draft-id>.",
            )
        draft = self._get_revision_draft(args[0])

        def execute() -> DomainCommandResult:
            self._finish_revision_draft(draft.draft_id)
            return _result(
                f"Discarded revision draft {draft.draft_id}.\n",
                {"draft_id": draft.draft_id, "discarded": True},
            )

        return _mutation_plan(
            "revise",
            DomainRisk.FILESYSTEM_MUTATION,
            "This will discard the in-memory revision draft without changing the saved chain.",
            (f"Discard revision draft {draft.draft_id}",),
            {
                "action": "revise_discard",
                "draft_id": draft.draft_id,
                "revised_sha256": draft.revised_digest,
            },
            {"action": "discard_revision", "draft_id": draft.draft_id},
            execute,
        )

    def _store_revision_draft(
        self,
        *,
        entity_id: str,
        channel: str,
        instruction: str,
        name: str,
        base_digest: str,
        revised_digest: str,
        chain: dict[str, Any],
    ) -> str:
        now = float(self._sources.now())
        with self._draft_lock:
            self._prune_drafts(now)
            while len(self._drafts) >= self._max_revision_drafts:
                oldest = min(self._drafts.values(), key=lambda item: item.created_at)
                self._drafts.pop(oldest.draft_id, None)
            for _ in range(8):
                draft_id = str(self._sources.new_id())
                if _DRAFT_ID_RE.fullmatch(draft_id) and draft_id not in self._drafts:
                    break
            else:
                raise DomainCommandError(
                    "draft_id_unavailable",
                    "Could not allocate a revision draft id.",
                    status=500,
                )
            self._drafts[draft_id] = _RevisionDraft(
                draft_id=draft_id,
                entity_id=entity_id,
                channel=channel,
                instruction=instruction,
                name=name,
                base_digest=base_digest,
                revised_digest=revised_digest,
                chain=chain,
                created_at=now,
            )
            return draft_id

    def _get_revision_draft(self, draft_id: str) -> _RevisionDraft:
        now = float(self._sources.now())
        with self._draft_lock:
            self._prune_drafts(now)
            draft = self._drafts.get(draft_id)
            if draft is None or draft.used:
                raise DomainCommandError(
                    "revision_draft_missing",
                    "Revision draft is missing, expired, or already used.",
                    status=404,
                )
            return draft

    def _claim_revision_draft(self, draft_id: str) -> _RevisionDraft:
        with self._draft_lock:
            draft = self._get_revision_draft(draft_id)
            draft.used = True
            return draft

    def _release_revision_draft(self, draft_id: str) -> None:
        with self._draft_lock:
            draft = self._drafts.get(draft_id)
            if draft is not None:
                draft.used = False

    def _finish_revision_draft(self, draft_id: str) -> None:
        with self._draft_lock:
            self._drafts.pop(draft_id, None)

    def _prune_drafts(self, now: float) -> None:
        expired = [
            draft_id
            for draft_id, draft in self._drafts.items()
            if now - draft.created_at >= self._draft_ttl
        ]
        for draft_id in expired:
            self._drafts.pop(draft_id, None)


def _read_plan(
    name: str,
    preview: Mapping[str, Any],
    execute: Callable[[], DomainCommandResult],
) -> DomainCommandPlan:
    return DomainCommandPlan(
        name=name,
        risk=DomainRisk.READ_ONLY,
        reason="",
        effects=(),
        confirmation_scope="",
        preview=_safe_mapping(preview),
        execute=execute,
    )


def _mutation_plan(
    name: str,
    risk: DomainRisk,
    reason: str,
    effects: tuple[str, ...],
    scope: Mapping[str, Any],
    preview: Mapping[str, Any],
    execute: Callable[[], DomainCommandResult],
) -> DomainCommandPlan:
    return DomainCommandPlan(
        name=name,
        risk=risk,
        reason=_safe_text(reason, max_chars=700),
        effects=tuple(_safe_text(item, max_chars=700) for item in effects),
        confirmation_scope=_fingerprint(scope),
        preview=_safe_mapping(preview),
        execute=execute,
    )


def _result(
    stdout: str,
    payload: Mapping[str, Any],
    *,
    ok: bool = True,
    exit_code: int = 0,
    stderr: str = "",
) -> DomainCommandResult:
    safe_payload = _safe_mapping(payload)
    rendered_payload = json.dumps(
        safe_payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    if len(rendered_payload) > MAX_RESULT_CHARS:
        safe_payload = {
            "summary": redact_sensitive_text(
                rendered_payload, max_chars=MAX_RESULT_CHARS
            ),
            "truncated": True,
        }
    return DomainCommandResult(
        stdout=redact_sensitive_text(stdout, max_chars=MAX_RESULT_CHARS),
        payload=safe_payload,
        ok=bool(ok),
        exit_code=int(exit_code),
        stderr=redact_sensitive_text(stderr, max_chars=MAX_RESULT_CHARS),
    )


def _normalise_transcript(
    transcript: Sequence[Mapping[str, Any]] | None,
) -> list[dict[str, Any]]:
    if transcript is None:
        raise DomainCommandError(
            "transcript_required",
            "The desktop caller must supply the current transcript for /export.",
        )
    if isinstance(transcript, (str, bytes)):
        raise DomainCommandError("transcript_invalid", "Transcript must be a list.")
    if len(transcript) > MAX_TRANSCRIPT_ROWS:
        raise DomainCommandError(
            "transcript_too_large",
            f"Transcript exceeds the {MAX_TRANSCRIPT_ROWS}-line cap.",
            status=413,
        )
    total = 0
    rows: list[dict[str, Any]] = []
    for raw in transcript:
        if not isinstance(raw, Mapping):
            raise DomainCommandError(
                "transcript_invalid", "Every transcript row must be an object."
            )
        role = str(raw.get("role") or "").lower()
        if role not in {"user", "assistant", "system", "tool"}:
            raise DomainCommandError(
                "transcript_invalid", "Transcript rows have an invalid role."
            )
        text = redact_sensitive_text(raw.get("text") or "")
        total += len(text)
        if total > MAX_TRANSCRIPT_CHARS:
            raise DomainCommandError(
                "transcript_too_large",
                f"Transcript exceeds the {MAX_TRANSCRIPT_CHARS}-character cap.",
                status=413,
            )
        rows.append(
            {
                "role": role,
                "text": text,
                "timestamp": _safe_text(raw.get("timestamp") or "", max_chars=64),
                "mode": _safe_text(raw.get("mode") or "", max_chars=64) or None,
                "reaction": _safe_text(raw.get("reaction") or "", max_chars=64)
                or None,
                "provenance": _safe_value(raw.get("provenance")),
            }
        )
    return rows


def _export_filename(value: str, fmt: str) -> str:
    name = str(value or "").strip()
    if not _EXPORT_NAME_RE.fullmatch(name) or name in {".", ".."}:
        raise DomainCommandError(
            "unsafe_export_filename",
            "Export filename must be a plain filename without directories.",
        )
    if Path(name).suffix.lower() != f".{fmt}":
        raise DomainCommandError(
            "export_extension_mismatch",
            f"Export filename must end in .{fmt}.",
        )
    return name


def _render_transcript(rows: Sequence[Mapping[str, Any]], fmt: str) -> str:
    if fmt == "json":
        return json.dumps(list(rows), ensure_ascii=False, indent=2) + "\n"
    if fmt == "html":
        body: list[str] = []
        for row in rows:
            role = str(row["role"])
            timestamp = html.escape(str(row.get("timestamp") or ""))
            label = html.escape(role)
            content = html.escape(str(row.get("text") or ""))
            body.append(
                f'<article class="line line-{role}"><header>{label}'
                f'{" · " + timestamp if timestamp else ""}</header>'
                f"<pre>{content}</pre></article>"
            )
        return (
            "<!doctype html>\n<html lang=\"en\"><head><meta charset=\"utf-8\">"
            "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
            "<title>Maestro transcript</title><style>"
            "body{max-width:900px;margin:2rem auto;padding:0 1rem;font:15px system-ui;"
            "background:#f7f4ed;color:#292720}.line{background:#fff;border:1px solid #ddd5c5;"
            "border-radius:14px;padding:1rem;margin:1rem 0}.line-assistant{border-color:#a7b89b}"
            "header{font-weight:700;color:#52634b}pre{white-space:pre-wrap;word-break:break-word;"
            "font:inherit;margin:.6rem 0 0}</style></head><body><h1>Maestro transcript</h1>"
            + "".join(body)
            + "</body></html>\n"
        )
    output = ["# Maestro transcript", ""]
    for row in rows:
        meta = str(row["role"])
        if row.get("timestamp"):
            meta += f" · {row['timestamp']}"
        text = str(row.get("text") or "")
        fence = "`" * max(4, _longest_backtick_run(text) + 1)
        output.extend([f"## {meta}", "", f"{fence}text", text, fence, ""])
    return "\n".join(output)


def _longest_backtick_run(value: str) -> int:
    longest = current = 0
    for char in value:
        if char == "`":
            current += 1
            longest = max(longest, current)
        else:
            current = 0
    return longest


def _export_target_state(path: Path) -> tuple[str, str | None, int]:
    if not path.exists() and not path.is_symlink():
        return ("missing", None, 0)
    if path.is_symlink() or not path.is_file():
        raise DomainCommandError(
            "unsafe_export_target",
            "Export target must be a regular file, not a symlink or directory.",
        )
    size = path.stat().st_size
    if size > MAX_EXISTING_EXPORT_BYTES:
        raise DomainCommandError(
            "export_target_too_large",
            "Existing export is too large to overwrite safely.",
            status=413,
        )
    return ("file", _sha256_file(path), size)


def _atomic_export_write(
    root: Path,
    target: Path,
    data: bytes,
    *,
    expected_state: tuple[str, str | None, int],
) -> None:
    root.mkdir(parents=True, exist_ok=True)
    if root.is_symlink() or not root.is_dir():
        raise DomainCommandError(
            "unsafe_export_directory", "Export directory is not a regular directory."
        )
    resolved_root = root.resolve()
    if resolved_root != root:
        raise DomainCommandError(
            "unsafe_export_directory",
            "Export directory changed or traverses a symlink.",
        )
    if target.parent.resolve() != resolved_root or target.name != target.relative_to(root).name:
        raise DomainCommandError(
            "unsafe_export_target", "Export target escaped the export directory."
        )
    if _export_target_state(target) != expected_state:
        raise _state_changed("The export target changed while confirmation was open.")
    fd, temporary = tempfile.mkstemp(prefix=".maestro-export-", dir=resolved_root)
    temp_path = Path(temporary)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb", closefd=True) as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        if expected_state[0] == "missing":
            try:
                os.link(temp_path, target)
            except FileExistsError as exc:
                raise _state_changed("The export target appeared during the write.") from exc
            temp_path.unlink(missing_ok=True)
        else:
            if _export_target_state(target) != expected_state:
                raise _state_changed("The export target changed during the write.")
            os.replace(temp_path, target)
        try:
            dir_fd = os.open(resolved_root, os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        except OSError:
            pass
    finally:
        try:
            os.close(fd)
        except OSError:
            pass
        temp_path.unlink(missing_ok=True)


def _image_descriptor(workspace: Path, raw: str, *, max_bytes: int) -> dict[str, Any]:
    path = _workspace_file(
        workspace,
        raw,
        suffixes=set(_IMAGE_MIME),
        max_bytes=max_bytes,
        label="Image",
    )
    mime = _IMAGE_MIME[path.suffix.lower()]
    with path.open("rb") as handle:
        header = handle.read(32)
    detected = _detect_image_mime(header)
    if detected != mime and not (mime == "image/jpeg" and detected == "image/jpeg"):
        raise DomainCommandError(
            "image_content_invalid",
            "Image content does not match its supported file extension.",
        )
    relative = path.relative_to(workspace).as_posix()
    digest = _sha256_file(path)
    resource_id = hashlib.sha256(f"{relative}\0{digest}".encode()).hexdigest()
    return {
        "action": "preview",
        "resource_id": resource_id,
        "relative_path": relative,
        "display_name": _safe_text(path.name, max_chars=180),
        "mime_type": mime,
        "size_bytes": path.stat().st_size,
        "sha256": digest,
        "route": (
            "/api/workspace-images/"
            + quote(relative, safe="/")
            + f"?sha256={digest}"
        ),
    }


def _detect_image_mime(header: bytes) -> str | None:
    if header.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if header.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if header.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if header.startswith(b"BM"):
        return "image/bmp"
    if len(header) >= 12 and header[:4] == b"RIFF" and header[8:12] == b"WEBP":
        return "image/webp"
    return None


def _workspace_file(
    workspace: Path,
    raw: str,
    *,
    suffixes: set[str],
    max_bytes: int,
    label: str,
) -> Path:
    text = str(raw or "").strip()
    if not text or _CONTROL_RE.search(text):
        raise DomainCommandError(
            "slash_arguments_invalid", f"{label} path is empty or invalid."
        )
    candidate = Path(text).expanduser()
    if not candidate.is_absolute():
        candidate = workspace / candidate
    try:
        path = candidate.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise DomainCommandError(
            "workspace_file_missing", f"{label} was not found.", status=404
        ) from exc
    if not _is_within(path, workspace) or not path.is_file():
        raise DomainCommandError(
            "unsafe_workspace_path", f"{label} must be a regular file inside the workspace."
        )
    if path.suffix.lower() not in suffixes:
        raise DomainCommandError(
            "unsupported_file_type", f"{label} has an unsupported file type."
        )
    size = path.stat().st_size
    if size > max_bytes:
        raise DomainCommandError(
            "workspace_file_too_large",
            f"{label} exceeds the {max_bytes}-byte cap.",
            status=413,
        )
    return path


def _upload_settings(config: Any) -> dict[str, Any]:
    upload = _field(config, "upload", None)
    if upload is None:
        raise DomainCommandError(
            "upload_not_configured",
            "Upload target is not configured. Set CARE_UPLOAD__URL first.",
            status=409,
        )
    url = _validated_endpoint(_field(upload, "url", ""), label="Upload URL")
    auth_header = str(_field(upload, "auth_header", "Authorization") or "Authorization")
    if not _HEADER_RE.fullmatch(auth_header):
        raise DomainCommandError(
            "upload_config_invalid", "Upload authentication header is invalid."
        )
    api_key_raw = _field(upload, "api_key", None)
    api_key = str(api_key_raw) if api_key_raw else None
    if api_key and (len(api_key) > 16_384 or "\n" in api_key or "\r" in api_key):
        raise DomainCommandError(
            "upload_config_invalid", "Upload credential has an invalid shape."
        )
    try:
        timeout = float(_field(upload, "timeout", 30.0) or 30.0)
    except (TypeError, ValueError, OverflowError) as exc:
        raise DomainCommandError(
            "upload_config_invalid", "Upload timeout is invalid."
        ) from exc
    if not (0.1 <= timeout <= 120.0):
        raise DomainCommandError(
            "upload_config_invalid", "Upload timeout must be between 0.1 and 120 seconds."
        )
    return {
        "url": url,
        "auth_header": auth_header,
        "api_key": api_key,
        "timeout": timeout,
        "fingerprint": {
            "url": url,
            "auth_header": auth_header,
            "api_key_sha256": _secret_digest(api_key),
            "timeout": timeout,
        },
    }


def _validated_endpoint(value: Any, *, label: str) -> str:
    raw = str(value or "").strip()
    try:
        parsed = urlsplit(raw)
        hostname = parsed.hostname
        port = parsed.port
    except (TypeError, ValueError) as exc:
        raise DomainCommandError("external_url_invalid", f"{label} is invalid.") from exc
    if (
        not hostname
        or parsed.scheme not in {"http", "https"}
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise DomainCommandError(
            "external_url_invalid",
            f"{label} must be an http(s) URL without credentials, query, or fragment.",
        )
    if parsed.scheme == "http" and not _is_loopback_host(hostname):
        raise DomainCommandError(
            "external_url_insecure",
            f"{label} must use HTTPS unless it targets loopback.",
        )
    host = hostname.lower()
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    netloc = host + (f":{port}" if port is not None else "")
    return urlunsplit((parsed.scheme, netloc, parsed.path or "/", "", ""))


def _require_service_endpoint(config: Any, section: str, *, label: str) -> str:
    service = _field(config, section, None)
    if service is None:
        raise DomainCommandError(
            "service_not_configured", f"{label} is not configured.", status=409
        )
    return _validated_endpoint(_field(service, "base_url", ""), label=label)


def _is_loopback_host(hostname: str) -> bool:
    if hostname.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        return False


def _get_chain(memory: Any, chain_id: str, *, channel: str = "latest") -> dict[str, Any]:
    getter = getattr(memory, "get_chain", None)
    if not callable(getter):
        raise DomainCommandError(
            "domain_capability_missing",
            "This Memory build cannot load saved chains.",
            status=501,
        )
    try:
        try:
            chain = getter(chain_id, channel=channel)
        except TypeError:
            chain = getter(chain_id)
    except (KeyError, LookupError) as exc:
        raise DomainCommandError(
            "chain_not_found", f"Saved chain {chain_id} was not found.", status=404
        ) from exc
    except Exception as exc:  # noqa: BLE001
        raise _operation_failed("Could not load the saved chain.", exc) from exc
    if not isinstance(chain, dict) or not chain:
        raise DomainCommandError(
            "chain_not_found", f"Saved chain {chain_id} was not found.", status=404
        )
    return chain


def _evolution_rows(envelope: Any) -> list[dict[str, Any]]:
    if isinstance(envelope, Mapping):
        raw = (
            envelope.get("items")
            or envelope.get("evolutions")
            or envelope.get("results")
            or envelope.get("data")
            or []
        )
    elif isinstance(envelope, Sequence) and not isinstance(envelope, (str, bytes)):
        raw = envelope
    else:
        raw = []
    rows: list[dict[str, Any]] = []
    for item in islice(raw, MAX_EVOLUTION_ROWS):
        safe = _safe_value(item)
        if not isinstance(safe, dict):
            continue
        if "id" not in safe and "evolution_id" in safe:
            safe["id"] = safe.get("evolution_id")
        rows.append(safe)
    return rows


def _raw_evolution_count(envelope: Any) -> int:
    if isinstance(envelope, Mapping):
        for key in ("items", "evolutions", "results", "data"):
            value = envelope.get(key)
            if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
                return len(value)
    if isinstance(envelope, Sequence) and not isinstance(envelope, (str, bytes)):
        return len(envelope)
    return 0


def _event_kind(event: Any) -> str:
    if not isinstance(event, Mapping):
        return ""
    return str(event.get("event") or event.get("type") or event.get("kind") or "").lower()


def _dataset_state(path: Path | None) -> tuple[str, str, int] | None:
    if path is None:
        return None
    if not path.exists() or not path.is_file():
        raise DomainCommandError(
            "workspace_file_missing", "Evaluation dataset is no longer available.", status=404
        )
    size = path.stat().st_size
    if size > MAX_DATASET_BYTES:
        raise DomainCommandError(
            "workspace_file_too_large",
            "Evaluation dataset exceeds the size cap.",
            status=413,
        )
    return (path.name, _sha256_file(path), size)


def _provider_config_digest(config: Any, *, mode: str) -> str:
    mage = _field(config, "mage", None)
    return _fingerprint(
        {
            "mode": mode,
            "mage": mage,
            # Revision can use Memory lookup/research depending on the MAGE
            # flags, so bind that endpoint and credential too.
            "memory": _field(config, "memory", None),
        }
    )


def _service_config_digest(config: Any, *, include_memory: bool) -> str:
    platform = _field(config, "platform", None)
    state: dict[str, Any] = {"platform": platform}
    if include_memory:
        state["memory"] = _field(config, "memory", None)
    return _fingerprint(state)


def _secret_digest(value: Any) -> str:
    raw = str(value or "")
    return hashlib.sha256(raw.encode("utf-8", errors="surrogatepass")).hexdigest()


def _revision_plan_lines(result: Any, before: Any, after: Any) -> list[str]:
    try:
        from care.runtime.chain_edit_view import render_edit_plan_lines

        lines = render_edit_plan_lines(result)
    except Exception:  # noqa: BLE001
        lines = []
    if not lines:
        lines = [
            f"steps: {len(_field(before, 'steps', []) or [])} -> "
            f"{len(_field(after, 'steps', []) or [])}"
        ]
    return [_safe_text(line, max_chars=2_000) for line in lines[:200]]


def _identifier(value: Any, *, label: str) -> str:
    text = str(value or "").strip()
    if not _ID_RE.fullmatch(text):
        raise DomainCommandError(
            "slash_arguments_invalid",
            f"{label} may use only letters, digits, '.', '_', ':', and '-'.",
        )
    return text


def _revision_entity_id(value: Any) -> str:
    """Accept the exact bounded/control-free id returned by Memory.

    Production revision pointers intentionally preserve the backend entity id
    verbatim. Composer arguments are shell-quoted before they reach this
    parser, so spaces and punctuation do not require weakening the stricter
    identifier grammar used by unrelated slash commands.
    """

    text = str(value or "")
    if not text.strip() or len(text) > 512 or _CONTROL_RE.search(text):
        raise DomainCommandError(
            "slash_arguments_invalid",
            "Chain id must be control-free text of at most 512 characters.",
        )
    return text


def _public_phrase(value: Any, *, label: str, max_chars: int) -> str:
    text = str(value or "").strip()
    if (
        not text
        or len(text) > max_chars
        or _CONTROL_RE.search(text.replace("\n", ""))
        or redact_sensitive_text(text) != text
    ):
        raise DomainCommandError(
            "slash_arguments_invalid",
            f"{label} must be non-secret text of at most {max_chars} characters.",
        )
    return text


def _bounded_int(value: Any, *, label: str, minimum: int, maximum: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise DomainCommandError(
            "slash_arguments_invalid", f"{label} must be an integer."
        ) from exc
    if not minimum <= number <= maximum:
        raise DomainCommandError(
            "slash_arguments_invalid",
            f"{label} must be between {minimum} and {maximum}.",
        )
    return number


def _safe_value(value: Any, *, depth: int = 0) -> Any:
    if depth >= 7:
        return "[depth limit]"
    if value is None or isinstance(value, bool | int):
        return value
    if isinstance(value, float):
        return value if value == value and abs(value) != float("inf") else None
    if isinstance(value, str):
        return redact_sensitive_text(value, max_chars=MAX_SAFE_VALUE_CHARS)
    if isinstance(value, Path):
        return value.name
    if isinstance(value, Mapping):
        output: dict[str, Any] = {}
        for index, (key, item) in enumerate(value.items()):
            if index >= 250:
                output["__truncated__"] = True
                break
            safe_key = _safe_text(key, max_chars=128)
            output[safe_key] = (
                "[redacted]"
                if _looks_secret_key(safe_key)
                else _safe_value(item, depth=depth + 1)
            )
        return output
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [_safe_value(item, depth=depth + 1) for item in islice(value, 500)]
    dump = getattr(value, "model_dump", None)
    if callable(dump):
        try:
            return _safe_value(dump(mode="json"), depth=depth + 1)
        except Exception:  # noqa: BLE001
            pass
    if hasattr(value, "__dict__"):
        try:
            return _safe_value(vars(value), depth=depth + 1)
        except Exception:  # noqa: BLE001
            pass
    return _safe_text(type(value).__name__, max_chars=128)


def _safe_mapping(value: Mapping[str, Any]) -> dict[str, Any]:
    projected = _safe_value(dict(value))
    return projected if isinstance(projected, dict) else {}


def _bounded_projection(value: Any, *, max_chars: int) -> Any:
    projected = _safe_value(value)
    encoded = json.dumps(projected, ensure_ascii=False, sort_keys=True)
    if len(encoded) <= max_chars:
        return projected
    return {
        "summary": redact_sensitive_text(encoded, max_chars=max_chars),
        "truncated": True,
    }


def _safe_text(value: Any, *, max_chars: int) -> str:
    text = redact_sensitive_text(value, max_chars=max_chars)
    return _CONTROL_RE.sub(" ", text).strip()[:max_chars]


def _looks_secret_key(value: str) -> bool:
    normalised = value.lower().replace("-", "_")
    if normalised.endswith("_sha256"):
        return False
    if normalised in {
        "api_key",
        "apikey",
        "api_token",
        "access_token",
        "refresh_token",
        "token",
        "password",
        "passwd",
        "secret",
        "client_secret",
        "authorization",
    }:
        return True
    return normalised.endswith(
        (
            "_api_key",
            "_api_token",
            "_access_token",
            "_refresh_token",
            "_password",
            "_passwd",
            "_secret",
            "_authorization",
        )
    )


def _reject_sensitive_chain(chain: Mapping[str, Any]) -> None:
    if _contains_sensitive_content(chain):
        raise DomainCommandError(
            "chain_contains_sensitive_data",
            "The chain appears to contain embedded credentials. Remove them before sending it to an external service.",
            status=409,
        )


def _validated_chain_digest(chain: Mapping[str, Any]) -> str:
    digest = _exact_json_digest(
        chain,
        max_bytes=MAX_CHAIN_BYTES,
        label="Chain payload",
    )
    _reject_sensitive_chain(chain)
    return digest


def _contains_sensitive_content(value: Any, *, depth: int = 0) -> bool:
    if depth >= 10:
        # Deep, attacker-controlled shapes are not part of the chain format;
        # fail closed instead of letting a hidden credential bypass scanning.
        return True
    if isinstance(value, str):
        return redact_sensitive_text(value) != value
    if isinstance(value, Mapping):
        for key, item in value.items():
            if _looks_secret_key(str(key)) or _contains_sensitive_content(
                item, depth=depth + 1
            ):
                return True
        return False
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return any(
            _contains_sensitive_content(item, depth=depth + 1)
            for item in value
        )
    return False


def _fingerprint(value: Any) -> str:
    normalised = _fingerprint_value(value)
    encoded = json.dumps(
        normalised,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8", errors="surrogatepass")
    return hashlib.sha256(encoded).hexdigest()


def _exact_json_digest(value: Any, *, max_bytes: int, label: str) -> str:
    encoder = json.JSONEncoder(
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=_exact_json_default,
    )
    digest = hashlib.sha256()
    consumed = 0
    try:
        for text in encoder.iterencode(value):
            chunk = text.encode("utf-8", errors="surrogatepass")
            consumed += len(chunk)
            if consumed > max_bytes:
                raise DomainCommandError(
                    "domain_payload_too_large",
                    f"{label} exceeds the {max_bytes}-byte cap.",
                    status=413,
                )
            digest.update(chunk)
    except DomainCommandError:
        raise
    except (TypeError, ValueError, RecursionError) as exc:
        raise DomainCommandError(
            "domain_payload_invalid",
            f"{label} is not valid bounded JSON data.",
        ) from exc
    return digest.hexdigest()


def _exact_json_default(value: Any) -> Any:
    dump = getattr(value, "model_dump", None)
    if callable(dump):
        return dump(mode="json")
    if hasattr(value, "__dict__"):
        return vars(value)
    if isinstance(value, Path):
        return str(value)
    raise TypeError(f"unsupported JSON value: {type(value).__name__}")


def _fingerprint_value(value: Any, *, depth: int = 0) -> Any:
    if depth >= 8:
        return "[depth-limit]"
    if value is None or isinstance(value, bool | int | str):
        return value[:16_384] if isinstance(value, str) else value
    if isinstance(value, float):
        return value if value == value and abs(value) != float("inf") else None
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Mapping):
        output: dict[str, Any] = {}
        for index, key in enumerate(sorted(value, key=lambda item: str(item))):
            if index >= 1_000:
                output["__truncated__"] = True
                break
            output[str(key)[:256]] = _fingerprint_value(
                value[key], depth=depth + 1
            )
        return output
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [
            _fingerprint_value(item, depth=depth + 1)
            for item in islice(value, 2_000)
        ]
    dump = getattr(value, "model_dump", None)
    if callable(dump):
        try:
            return _fingerprint_value(dump(mode="json"), depth=depth + 1)
        except Exception:  # noqa: BLE001
            pass
    if hasattr(value, "__dict__"):
        try:
            return _fingerprint_value(vars(value), depth=depth + 1)
        except Exception:  # noqa: BLE001
            pass
    return type(value).__name__


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(128 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _field(value: Any, name: str, default: Any = None) -> Any:
    if isinstance(value, Mapping):
        return value.get(name, default)
    return getattr(value, name, default)


def _run_async(awaitable: Any) -> Any:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(awaitable)
    close = getattr(awaitable, "close", None)
    if callable(close):
        close()
    raise DomainCommandError(
        "async_context_unsupported",
        "This desktop command must execute outside an active event loop.",
        status=500,
    )


def _state_changed(message: str) -> DomainCommandError:
    return DomainCommandError("confirmation_state_changed", message, status=409)


def _operation_failed(message: str, _exc: BaseException) -> DomainCommandError:
    return DomainCommandError("domain_operation_failed", message, status=502)


__all__ = [
    "DesktopRemainingCommands",
    "DesktopRemainingSources",
]
