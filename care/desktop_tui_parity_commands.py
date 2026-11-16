"""Native, UI-neutral adapters for the last stateful TUI-only forms.

The desktop command bridge can consume these plans in the same way as the
other domain adapters.  This module deliberately does not import an HTTP
client, ``subprocess``, or a platform launcher.  All mutations and local model
invocations are confirmation-gated, bind the exact state observed while the
plan is built, and repeat that check immediately before the effect.

The voice seam is intentionally injectable.  A host may wrap a locally
installed Whisper implementation in :class:`LocalVoiceBackend`; this module
never chooses a provider or downloads model weights on its own.

Integration note: the editor ``content`` payload and the base64url save token
are private document transport, not chat transcript or diagnostic fields.  A
command bridge must suppress that opaque save token from invocation displays
and logs; generic credential redaction cannot recognise an encoded value.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import os
import re
import stat
import tempfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from itertools import islice
from pathlib import Path
from typing import Any

from care.desktop_domain_commands import (
    DomainCommandError,
    DomainCommandPlan,
    DomainCommandResult,
    DomainRisk,
)
from care.redaction import redact_sensitive_text
from care.runtime.workspace_context import current_workspace_root
from care.sandbox import SkillTrustStore, TrustRecord


# The save form is carried as one base64url token through the desktop bridge,
# whose command envelope is capped at 16 KiB.  Twelve thousand decoded bytes
# leave room for the command name, base digest, and confirmation re-plan while
# covering the default 8,000-character CARE.md context budget.
MAX_MEMORY_BYTES = 12_000
MAX_AUDIO_BYTES = 64 * 1024 * 1024
MAX_TRANSCRIPT_CHARS = 32 * 1024
MAX_RESULT_CHARS = 96 * 1024
MAX_SAFE_VALUE_CHARS = 64 * 1024
MAX_TRUST_ROWS = 500
MAX_TRUST_STATE_ROWS = 2_000
MAX_TRUST_TOOLS = 256

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_BASE64URL_RE = re.compile(r"^[A-Za-z0-9_-]*$")
_BACKEND_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._ -]{0,63}$")
_DISPLAY_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_METADATA_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")

_AUDIO_MIME_BY_SUFFIX = {
    ".aac": "audio/aac",
    ".aif": "audio/aiff",
    ".aiff": "audio/aiff",
    ".flac": "audio/flac",
    ".m4a": "audio/mp4",
    ".mp3": "audio/mpeg",
    ".ogg": "audio/ogg",
    ".opus": "audio/opus",
    ".wav": "audio/wav",
    ".webm": "audio/webm",
}


def _default_load_config() -> Any:
    from care.config import CareConfig

    return CareConfig.load()


def _default_memory_path(config: Any) -> Path:
    context = _field(config, "context", None)
    return Path(_field(context, "global_path", ""))


def _default_load_trust_store() -> SkillTrustStore:
    return SkillTrustStore.load()


def _default_workspace_root() -> Path:
    return current_workspace_root()


def _default_load_voice_backend() -> LocalVoiceBackend | None:
    return None


@dataclass(frozen=True)
class LocalAudioInput:
    """Bounded immutable audio snapshot handed to a local backend."""

    relative_path: str
    display_name: str
    mime_type: str
    sha256: str
    content: bytes = field(repr=False)


@dataclass(frozen=True)
class LocalVoiceBackend:
    """A host-supplied, local-only transcription adapter.

    ``state_sha256`` must return a digest of every setting that can change
    which local model/tool handles the file.  It must not return the settings
    themselves.  The callable receives immutable, already validated bytes --
    never an ambient filesystem path.  The host is responsible for injecting
    a vetted implementation that performs no network or shell operation.
    """

    name: str
    transcribe: Callable[[LocalAudioInput], str]
    state_sha256: Callable[[], str]


@dataclass(frozen=True)
class DesktopTuiParitySources:
    """Dependency seams for hermetic tests and native-shell integration."""

    load_config: Callable[[], Any] = _default_load_config
    memory_path: Callable[[Any], Path] = _default_memory_path
    load_trust_store: Callable[[], SkillTrustStore] = _default_load_trust_store
    workspace_root: Callable[[], Path] = _default_workspace_root
    load_voice_backend: Callable[
        [], LocalVoiceBackend | None
    ] = _default_load_voice_backend


@dataclass(frozen=True)
class _MemoryState:
    kind: str
    sha256: str
    size_bytes: int
    mode: int
    content: str
    sensitive: bool

    def binding(self) -> tuple[str, str, int, int, bool]:
        return (
            self.kind,
            self.sha256,
            self.size_bytes,
            self.mode,
            self.sensitive,
        )


@dataclass(frozen=True)
class _AudioState:
    path: Path
    relative_path: str
    mime_type: str
    suffix: str
    size_bytes: int
    sha256: str

    def binding(self) -> tuple[str, str, str, int, str]:
        return (
            self.relative_path,
            self.mime_type,
            self.suffix,
            self.size_bytes,
            self.sha256,
        )


class _PrivateText(str):
    """JSON-compatible text whose accidental object repr stays opaque."""

    def __repr__(self) -> str:
        return "'[private content]'"


class DesktopTuiParityCommands:
    """Plan ``memory edit``, ``sandbox revoke``, and voice transcription."""

    NAMES = frozenset({"memory", "sandbox", "voice"})

    def __init__(self, sources: DesktopTuiParitySources | None = None) -> None:
        self._sources = sources or DesktopTuiParitySources()

    def plan(self, name: str, args: Sequence[str]) -> DomainCommandPlan:
        """Strictly parse one form and return an auditable domain plan."""

        command = str(name or "").strip().lower()
        tokens = [str(item) for item in args]
        if command not in self.NAMES:
            raise DomainCommandError(
                "slash_command_unavailable",
                f"Desktop /{command or '?'} has no reviewed TUI-parity adapter.",
                status=501,
            )
        try:
            if command == "memory":
                return self._plan_memory(tokens)
            if command == "sandbox":
                return self._plan_sandbox(tokens)
            return self._plan_voice(tokens)
        except DomainCommandError:
            raise
        except Exception as exc:  # noqa: BLE001 - never expose source details
            raise DomainCommandError(
                "domain_unavailable",
                f"Desktop /{command} data is temporarily unavailable.",
                status=503,
            ) from exc

    # ------------------------------------------------------------------
    # /memory edit [save ...]
    # ------------------------------------------------------------------
    def _plan_memory(self, args: list[str]) -> DomainCommandPlan:
        if not args or args[0].lower() != "edit":
            raise DomainCommandError(
                "slash_action_unsupported",
                "This adapter supports `/memory edit` and "
                "`/memory edit save <base-sha256> <base64url-utf8>`.",
            )
        if len(args) == 1:
            return self._plan_memory_editor()
        if len(args) == 4 and args[1].lower() == "save":
            return self._plan_memory_save(args[2], args[3])
        raise DomainCommandError(
            "slash_arguments_invalid",
            "Usage: /memory edit save <base-sha256> <base64url-utf8>.",
        )

    def _plan_memory_editor(self) -> DomainCommandPlan:
        target = self._memory_target()
        state = _memory_state(target)
        preview = {
            "action": "edit_memory",
            "document": "CARE.md",
            "exists": state.kind == "file",
            "size_bytes": state.size_bytes,
            "base_sha256": state.sha256,
            "editable": not state.sensitive,
        }

        def execute() -> DomainCommandResult:
            current_target = self._memory_target()
            if current_target != target:
                raise _state_changed(
                    "The configured CARE.md path changed while it was opened."
                )
            current = _memory_state(current_target)
            if current.binding() != state.binding():
                raise _state_changed("CARE.md changed while it was opened.")
            payload = {
                **preview,
                "content": _PrivateText(current.content),
                "content_redacted": current.sensitive,
                "save_available": not current.sensitive,
                "encoding": "utf-8",
                "save_encoding": "base64url-utf8-unpadded",
                "max_size_bytes": MAX_MEMORY_BYTES,
            }
            if current.sensitive:
                payload["blocked_reason"] = "credential_like_content"
            return _result("CARE.md editor descriptor ready.\n", payload)

        return _read_plan("memory", preview, execute)

    def _plan_memory_save(
        self,
        raw_base_sha256: str,
        raw_content: str,
    ) -> DomainCommandPlan:
        base_sha256 = _exact_sha256(raw_base_sha256, label="Base SHA-256")
        encoded = _decode_memory_content(raw_content)
        content_sha256 = hashlib.sha256(encoded).hexdigest()
        target = self._memory_target()
        initial = _memory_state(target)
        if initial.sha256 != base_sha256:
            raise _state_changed(
                "CARE.md changed since this editor document was loaded."
            )
        if initial.sensitive:
            raise DomainCommandError(
                "memory_content_sensitive",
                "CARE.md contains credential-like text and cannot be safely "
                "round-tripped through the native editor.",
                status=409,
            )
        document_changed = (
            initial.kind == "missing" or content_sha256 != base_sha256
        )
        preview = {
            "action": "save_memory",
            "document": "CARE.md",
            "base_sha256": base_sha256,
            "new_sha256": content_sha256,
            "size_bytes": len(encoded),
            "creates_file": initial.kind == "missing",
            "changed": document_changed,
        }

        def execute() -> DomainCommandResult:
            current_target = self._memory_target()
            if current_target != target:
                raise _state_changed(
                    "The configured CARE.md path changed while confirmation was open."
                )
            current = _memory_state(current_target)
            if current.binding() != initial.binding():
                raise _state_changed(
                    "CARE.md changed while confirmation was open."
                )
            if current.sensitive:
                raise _state_changed(
                    "CARE.md is no longer safe to edit through the native app."
                )
            if current.kind == "missing" or content_sha256 != current.sha256:
                try:
                    _atomic_memory_write(
                        current_target,
                        encoded,
                        expected=current,
                    )
                except DomainCommandError:
                    raise
                except OSError as exc:
                    raise _operation_failed("Saving CARE.md failed.", exc) from exc
            saved = _memory_state(current_target)
            if (
                saved.kind != "file"
                or saved.sha256 != content_sha256
                or saved.size_bytes != len(encoded)
            ):
                raise _operation_failed(
                    "CARE.md could not be verified after saving.", RuntimeError()
                )
            return _result(
                "Saved CARE.md.\n",
                {**preview, "saved": True, "changed": document_changed},
            )

        return _mutation_plan(
            "memory",
            DomainRisk.FILESYSTEM_MUTATION,
            "This will atomically replace the configured global CARE.md document.",
            (
                f"{'Create' if initial.kind == 'missing' else 'Replace'} CARE.md",
            ),
            {
                "action": "memory_edit_save",
                "target_sha256": hashlib.sha256(
                    os.fsencode(target)
                ).hexdigest(),
                "initial_state": initial.binding(),
                "new_sha256": content_sha256,
                "new_size_bytes": len(encoded),
            },
            preview,
            execute,
        )

    def _memory_target(self) -> Path:
        try:
            config = self._sources.load_config()
            configured = self._sources.memory_path(config)
        except DomainCommandError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise _operation_failed(
                "Could not load the configured CARE.md path.", exc
            ) from exc
        return _configured_file_path(configured, label="CARE.md")

    # ------------------------------------------------------------------
    # /sandbox [list|revoke]
    # ------------------------------------------------------------------
    def _plan_sandbox(self, args: list[str]) -> DomainCommandPlan:
        if not args or [item.lower() for item in args] == ["list"]:
            return self._plan_sandbox_list()
        if len(args) == 2 and args[0].lower() == "revoke":
            return self._plan_sandbox_revoke(args[1])
        raise DomainCommandError(
            "slash_arguments_invalid",
            "Usage: /sandbox [list] or /sandbox revoke <exact-sha256>.",
        )

    def _plan_sandbox_list(self) -> DomainCommandPlan:
        store = self._load_trust_store()
        records = list(store.list_trusted())
        rows = [_trust_projection(record) for record in records[:MAX_TRUST_ROWS]]
        preview = {
            "action": "list_trusted_skills",
            "count": len(rows),
            "truncated": len(records) > MAX_TRUST_ROWS,
            "mutable": True,
        }

        def execute() -> DomainCommandResult:
            current = self._load_trust_store()
            current_records = list(current.list_trusted())
            current_rows = [
                _trust_projection(record)
                for record in current_records[:MAX_TRUST_ROWS]
            ]
            payload = {
                "action": "list_trusted_skills",
                "trusted_skills": current_rows,
                "count": len(current_rows),
                "truncated": len(current_records) > MAX_TRUST_ROWS,
                "mutable": True,
                "revoke_form": "/sandbox revoke <exact-sha256>",
            }
            lines = [f"Trusted skills: {len(current_rows)}"]
            lines.extend(
                f"- {row['name'] or 'unnamed'} ({row['sha256_prefix']})"
                for row in current_rows
            )
            if payload["truncated"]:
                lines.append(f"… showing first {MAX_TRUST_ROWS} approvals")
            return _result("\n".join(lines) + "\n", payload)

        return _read_plan("sandbox", preview, execute)

    def _plan_sandbox_revoke(self, raw_sha256: str) -> DomainCommandPlan:
        sha256 = _exact_sha256(raw_sha256, label="Skill SHA-256")
        store = self._load_trust_store()
        store_digest = _trust_store_digest(store)
        record = store.get(sha256)
        if record is None:
            raise DomainCommandError(
                "trust_record_not_found",
                "No trusted skill matches that exact SHA-256.",
                status=404,
            )
        record_state = _trust_record_state(record, expected_sha256=sha256)
        projected = _trust_projection(record)
        preview = {
            "action": "revoke_skill_trust",
            **projected,
        }

        def execute() -> DomainCommandResult:
            current = self._load_trust_store()
            if _trust_store_digest(current) != store_digest:
                raise _state_changed(
                    "The skill trust store changed while confirmation was open."
                )
            current_record = current.get(sha256)
            if current_record is None:
                raise _state_changed(
                    "This skill approval was already removed."
                )
            if (
                _trust_record_state(current_record, expected_sha256=sha256)
                != record_state
            ):
                raise _state_changed(
                    "This skill approval changed while confirmation was open."
                )
            try:
                removed = current.revoke(sha256)
            except Exception as exc:  # noqa: BLE001
                raise _operation_failed(
                    "Revoking the skill approval failed.", exc
                ) from exc
            if not removed or current.is_trusted(sha256):
                raise _operation_failed(
                    "The skill approval could not be verified as revoked.",
                    RuntimeError(),
                )
            expected_persisted_digest = _trust_store_digest(current)
            persisted = self._load_trust_store()
            if (
                persisted.is_trusted(sha256)
                or _trust_store_digest(persisted) != expected_persisted_digest
            ):
                raise _operation_failed(
                    "The persisted skill trust store could not be verified.",
                    RuntimeError(),
                )
            return _result(
                f"Revoked trust for {projected['name'] or 'skill'} "
                f"({projected['sha256_prefix']}).\n",
                {**preview, "revoked": True},
            )

        return _mutation_plan(
            "sandbox",
            DomainRisk.FILESYSTEM_MUTATION,
            "This will revoke one exact SHA-pinned skill approval.",
            (
                f"Revoke trust for {projected['name'] or 'skill'} "
                f"({projected['sha256_prefix']})",
            ),
            {
                "action": "sandbox_revoke",
                "sha256": sha256,
                "record_state_sha256": _fingerprint(record_state),
                "trust_store_sha256": store_digest,
            },
            preview,
            execute,
        )

    def _load_trust_store(self) -> SkillTrustStore:
        try:
            store = self._sources.load_trust_store()
        except Exception as exc:  # noqa: BLE001
            raise _operation_failed("Could not load the skill trust store.", exc) from exc
        if not isinstance(store, SkillTrustStore):
            raise DomainCommandError(
                "trust_store_invalid",
                "The configured skill trust store is unavailable.",
                status=503,
            )
        return store

    # ------------------------------------------------------------------
    # /voice [status|transcribe]
    # ------------------------------------------------------------------
    def _plan_voice(self, args: list[str]) -> DomainCommandPlan:
        if not args or [item.lower() for item in args] == ["status"]:
            return self._plan_voice_status()
        if len(args) == 2 and args[0].lower() == "transcribe":
            return self._plan_voice_transcribe(args[1])
        raise DomainCommandError(
            "slash_arguments_invalid",
            "Usage: /voice [status] or /voice transcribe <workspace-audio-path>.",
        )

    def _plan_voice_status(self) -> DomainCommandPlan:
        backend = self._load_voice_backend(required=False)
        name = _backend_state(backend)[0] if backend is not None else None
        preview = {
            "action": "voice_status",
            "available": backend is not None,
            "backend": name,
            "local_only": True,
            "max_audio_bytes": MAX_AUDIO_BYTES,
            "supported_suffixes": sorted(_AUDIO_MIME_BY_SUFFIX),
        }
        return _read_plan(
            "voice",
            preview,
            lambda: _result(
                (
                    f"Local voice backend: {name}.\n"
                    if name
                    else "Local voice transcription is not configured.\n"
                ),
                preview,
            ),
        )

    def _plan_voice_transcribe(self, raw_path: str) -> DomainCommandPlan:
        workspace = _workspace_root(self._sources.workspace_root())
        audio = _audio_state(workspace, raw_path)
        backend = self._load_voice_backend(required=True)
        assert backend is not None
        backend_name, backend_digest = _backend_state(backend)
        preview = {
            "action": "transcribe_workspace_audio",
            "relative_path": audio.relative_path,
            "display_name": _safe_metadata(audio.path.name, max_chars=180),
            "mime_type": audio.mime_type,
            "size_bytes": audio.size_bytes,
            "sha256": audio.sha256,
            "backend": backend_name,
            "local_only": True,
        }

        def execute() -> DomainCommandResult:
            current_workspace = _workspace_root(self._sources.workspace_root())
            if current_workspace != workspace:
                raise _state_changed(
                    "The workspace changed while confirmation was open."
                )
            current_audio = _audio_state(current_workspace, raw_path)
            if current_audio.binding() != audio.binding():
                raise _state_changed(
                    "The audio file changed while confirmation was open."
                )
            current_backend = self._load_voice_backend(required=True)
            assert current_backend is not None
            current_backend_name, current_backend_digest = _backend_state(
                current_backend
            )
            if (
                current_backend_name != backend_name
                or current_backend_digest != backend_digest
            ):
                raise _state_changed(
                    "The local voice backend changed while confirmation was open."
                )
            audio_input = _audio_snapshot(current_audio)
            try:
                transcript = current_backend.transcribe(audio_input)
            except Exception as exc:  # noqa: BLE001
                raise _operation_failed("Local voice transcription failed.", exc) from exc
            if not isinstance(transcript, str):
                raise DomainCommandError(
                    "voice_backend_invalid_result",
                    "The local voice backend returned an invalid transcript.",
                    status=502,
                )
            raw_preview = transcript[: MAX_TRANSCRIPT_CHARS + 1]
            safe = _safe_display(
                raw_preview[:MAX_TRANSCRIPT_CHARS],
                max_chars=MAX_TRANSCRIPT_CHARS,
            ).strip()
            if not safe:
                raise DomainCommandError(
                    "voice_transcription_empty",
                    "The local voice backend returned an empty transcript.",
                    status=502,
                )
            redacted = safe != _DISPLAY_CONTROL_RE.sub(
                " ", raw_preview[:MAX_TRANSCRIPT_CHARS]
            ).strip()
            payload = {
                **preview,
                "transcript": _PrivateText(safe),
                "word_count": len(safe.split()),
                "truncated": len(transcript) > MAX_TRANSCRIPT_CHARS,
                "redacted": redacted,
            }
            return _result(
                f"Transcribed {audio.relative_path} with {backend_name}.\n",
                payload,
            )

        return _mutation_plan(
            "voice",
            DomainRisk.EXTERNAL_SIDE_EFFECT,
            "This will read the selected workspace audio and invoke a vetted "
            "host-supplied local transcription model/tool with bounded bytes. "
            "The adapter itself has no network or shell implementation.",
            (
                f"Read {audio.relative_path}",
                f"Invoke local voice backend {backend_name}",
            ),
            {
                "action": "voice_transcribe",
                "workspace_sha256": hashlib.sha256(
                    os.fsencode(workspace)
                ).hexdigest(),
                "audio_state": audio.binding(),
                "backend_name": backend_name,
                "backend_state_sha256": backend_digest,
            },
            preview,
            execute,
        )

    def _load_voice_backend(
        self,
        *,
        required: bool,
    ) -> LocalVoiceBackend | None:
        try:
            backend = self._sources.load_voice_backend()
        except Exception as exc:  # noqa: BLE001
            raise _operation_failed(
                "Could not load the local voice backend.", exc
            ) from exc
        if backend is None:
            if required:
                raise DomainCommandError(
                    "voice_backend_unavailable",
                    "No local voice transcription backend is configured.",
                    status=409,
                )
            return None
        if not isinstance(backend, LocalVoiceBackend):
            raise DomainCommandError(
                "voice_backend_invalid",
                "The configured local voice backend is invalid.",
                status=503,
            )
        return backend


def plan(
    name: str,
    args: Sequence[str],
    *,
    sources: DesktopTuiParitySources | None = None,
) -> DomainCommandPlan:
    """Convenience entry point for callers that do not retain a service."""

    return DesktopTuiParityCommands(sources).plan(name, args)


# ---------------------------------------------------------------------------
# Memory helpers
# ---------------------------------------------------------------------------


def _configured_file_path(value: Any, *, label: str) -> Path:
    text = os.fspath(value) if isinstance(value, os.PathLike) else str(value or "")
    if not text or "\x00" in text:
        raise DomainCommandError(
            "memory_path_invalid", f"The configured {label} path is invalid.", status=409
        )
    expanded = Path(text).expanduser()
    if not expanded.is_absolute():
        expanded = current_workspace_root() / expanded
    if expanded.is_symlink():
        raise DomainCommandError(
            "unsafe_memory_target",
            f"The configured {label} target must not be a symlink.",
            status=409,
        )
    try:
        if expanded.exists():
            if not expanded.is_file():
                raise DomainCommandError(
                    "unsafe_memory_target",
                    f"The configured {label} target is not a regular file.",
                    status=409,
                )
            return expanded.resolve(strict=True)
        return expanded.resolve(strict=False)
    except DomainCommandError:
        raise
    except (OSError, RuntimeError) as exc:
        raise DomainCommandError(
            "memory_path_invalid",
            f"The configured {label} path cannot be resolved.",
            status=409,
        ) from exc


def _memory_state(path: Path) -> _MemoryState:
    if path.is_symlink():
        raise DomainCommandError(
            "unsafe_memory_target",
            "The configured CARE.md target must not be a symlink.",
            status=409,
        )
    try:
        before = path.stat(follow_symlinks=False)
    except FileNotFoundError:
        return _MemoryState(
            kind="missing",
            sha256=hashlib.sha256(b"").hexdigest(),
            size_bytes=0,
            mode=0o600,
            content="",
            sensitive=False,
        )
    except OSError as exc:
        raise _operation_failed("Could not read CARE.md metadata.", exc) from exc
    if not stat.S_ISREG(before.st_mode):
        raise DomainCommandError(
            "unsafe_memory_target",
            "The configured CARE.md target is not a regular file.",
            status=409,
        )
    if before.st_size > MAX_MEMORY_BYTES:
        raise DomainCommandError(
            "memory_document_too_large",
            f"CARE.md exceeds the {MAX_MEMORY_BYTES}-byte editor cap.",
            status=413,
        )
    try:
        with path.open("rb") as handle:
            data = handle.read(MAX_MEMORY_BYTES + 1)
        after = path.stat(follow_symlinks=False)
    except OSError as exc:
        raise _operation_failed("Could not read CARE.md.", exc) from exc
    if _stat_identity(before) != _stat_identity(after) or len(data) != after.st_size:
        raise _state_changed("CARE.md changed while it was being read.")
    if len(data) > MAX_MEMORY_BYTES:
        raise DomainCommandError(
            "memory_document_too_large",
            f"CARE.md exceeds the {MAX_MEMORY_BYTES}-byte editor cap.",
            status=413,
        )
    try:
        content = data.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise DomainCommandError(
            "memory_document_not_utf8",
            "CARE.md must be valid UTF-8 to use the native editor.",
            status=409,
        ) from exc
    if _DISPLAY_CONTROL_RE.search(content):
        raise DomainCommandError(
            "memory_document_invalid",
            "CARE.md contains unsupported control characters.",
            status=409,
        )
    safe_content = redact_sensitive_text(content, max_chars=MAX_MEMORY_BYTES)
    sensitive = safe_content != content
    return _MemoryState(
        kind="file",
        sha256=hashlib.sha256(data).hexdigest(),
        size_bytes=len(data),
        mode=stat.S_IMODE(before.st_mode),
        content=safe_content,
        sensitive=sensitive,
    )


def _decode_memory_content(value: Any) -> bytes:
    token = str(value or "")
    max_encoded = ((MAX_MEMORY_BYTES + 2) // 3) * 4
    if len(token) > max_encoded:
        raise DomainCommandError(
            "memory_content_invalid",
            f"Editor content must be at most {MAX_MEMORY_BYTES} decoded bytes.",
            status=413,
        )
    if not _BASE64URL_RE.fullmatch(token):
        raise DomainCommandError(
            "memory_content_invalid",
            "Editor content must be canonical unpadded base64url UTF-8.",
        )
    if len(token) % 4 == 1:
        raise DomainCommandError(
            "memory_content_invalid",
            "Editor content must be canonical unpadded base64url UTF-8.",
        )
    padded = token + "=" * ((-len(token)) % 4)
    try:
        data = base64.b64decode(
            padded.encode("ascii"),
            altchars=b"-_",
            validate=True,
        )
    except (UnicodeEncodeError, binascii.Error, ValueError) as exc:
        raise DomainCommandError(
            "memory_content_invalid",
            "Editor content must be canonical unpadded base64url UTF-8.",
        ) from exc
    canonical = base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")
    if canonical != token or len(data) > MAX_MEMORY_BYTES:
        raise DomainCommandError(
            "memory_content_invalid",
            f"Editor content must be at most {MAX_MEMORY_BYTES} decoded bytes.",
            status=413 if len(data) > MAX_MEMORY_BYTES else 400,
        )
    try:
        text = data.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise DomainCommandError(
            "memory_content_invalid",
            "Editor content must decode as UTF-8.",
        ) from exc
    if _DISPLAY_CONTROL_RE.search(text):
        raise DomainCommandError(
            "memory_content_invalid",
            "Editor content contains unsupported control characters.",
        )
    if redact_sensitive_text(text) != text:
        raise DomainCommandError(
            "memory_content_sensitive",
            "Credential-like values cannot be saved through the native memory editor.",
            status=409,
        )
    return data


def _atomic_memory_write(
    target: Path,
    data: bytes,
    *,
    expected: _MemoryState,
) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    parent = target.parent.resolve(strict=True)
    if not parent.is_dir() or target.parent != parent or target != parent / target.name:
        raise DomainCommandError(
            "unsafe_memory_target",
            "The configured CARE.md directory changed or traverses a symlink.",
            status=409,
        )
    if _memory_state(target).binding() != expected.binding():
        raise _state_changed("CARE.md changed while confirmation was open.")
    fd, temporary = tempfile.mkstemp(prefix=".care-md-", dir=parent)
    temp_path = Path(temporary)
    try:
        os.fchmod(fd, expected.mode if expected.kind == "file" else 0o600)
        with os.fdopen(fd, "wb", closefd=True) as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        if expected.kind == "missing":
            try:
                os.link(temp_path, target)
            except FileExistsError as exc:
                raise _state_changed(
                    "CARE.md appeared while confirmation was open."
                ) from exc
            temp_path.unlink(missing_ok=True)
        else:
            if _memory_state(target).binding() != expected.binding():
                raise _state_changed("CARE.md changed during the save.")
            os.replace(temp_path, target)
        _fsync_directory(parent)
    finally:
        try:
            os.close(fd)
        except OSError:
            pass
        temp_path.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Trust-store helpers
# ---------------------------------------------------------------------------


def _trust_projection(record: TrustRecord) -> dict[str, Any]:
    sha = str(record.sha256 or "").lower()
    prefix = sha[:12] if _SHA256_RE.fullmatch(sha) else "invalid"
    approved_at = record.approved_at
    approved_text = (
        approved_at.isoformat()
        if isinstance(approved_at, datetime)
        else ""
    )
    return {
        "name": _safe_metadata(record.name, max_chars=128),
        "sha256_prefix": prefix,
        "approved_at": _safe_metadata(approved_text, max_chars=64) or None,
        "trust_policy": _safe_metadata(record.trust_policy, max_chars=32),
        "allowed_tools": [
            _safe_metadata(tool, max_chars=128)
            for tool in list(record.allowed_tools)[:64]
        ],
    }


def _trust_record_state(
    record: TrustRecord,
    *,
    expected_sha256: str | None = None,
) -> tuple[str, str, str, str, str, tuple[str, ...]]:
    sha = str(record.sha256 or "")
    if not _SHA256_RE.fullmatch(sha) or (
        expected_sha256 is not None and sha != expected_sha256
    ):
        raise DomainCommandError(
            "trust_store_invalid",
            "The skill trust store contains an invalid SHA-pinned record.",
            status=409,
        )
    uri = str(record.uri or "")
    name = str(record.name or "")
    policy = str(record.trust_policy or "")
    approved = (
        record.approved_at.isoformat()
        if isinstance(record.approved_at, datetime)
        else str(record.approved_at or "")
    )
    tools = tuple(str(item) for item in record.allowed_tools)
    if (
        len(uri) > 4_096
        or len(name) > 1_024
        or len(policy) > 128
        or len(approved) > 128
        or len(tools) > MAX_TRUST_TOOLS
        or any(len(tool) > 4_096 for tool in tools)
    ):
        raise DomainCommandError(
            "trust_store_too_large",
            "The skill trust store contains an oversized record.",
            status=413,
        )
    return (sha, uri, name, approved, policy, tools)


def _trust_store_digest(store: SkillTrustStore) -> str:
    records = list(store.list_trusted())
    if len(records) > MAX_TRUST_STATE_ROWS:
        raise DomainCommandError(
            "trust_store_too_large",
            f"The skill trust store exceeds the {MAX_TRUST_STATE_ROWS}-record cap.",
            status=413,
        )
    states = sorted(_trust_record_state(record) for record in records)
    return _fingerprint(states)


# ---------------------------------------------------------------------------
# Voice helpers
# ---------------------------------------------------------------------------


def _workspace_root(value: Any) -> Path:
    try:
        root = Path(value).expanduser().resolve(strict=True)
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        raise DomainCommandError(
            "workspace_unavailable",
            "The current workspace is unavailable.",
            status=409,
        ) from exc
    if not root.is_dir():
        raise DomainCommandError(
            "workspace_unavailable",
            "The current workspace is unavailable.",
            status=409,
        )
    return root


def _audio_state(workspace: Path, raw_path: Any) -> _AudioState:
    text = str(raw_path or "").strip()
    if not text or "\x00" in text or _METADATA_CONTROL_RE.search(text):
        raise DomainCommandError(
            "slash_arguments_invalid", "Audio path is empty or invalid."
        )
    candidate = Path(text).expanduser()
    if not candidate.is_absolute():
        candidate = workspace / candidate
    if candidate.is_symlink():
        raise DomainCommandError(
            "unsafe_workspace_path",
            "Audio must be a regular, non-symlink file inside the workspace.",
        )
    try:
        path = candidate.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise DomainCommandError(
            "workspace_file_missing", "Audio file was not found.", status=404
        ) from exc
    if not _is_within(path, workspace) or not path.is_file():
        raise DomainCommandError(
            "unsafe_workspace_path",
            "Audio must be a regular file inside the workspace.",
        )
    suffix = path.suffix.lower()
    expected_mime = _AUDIO_MIME_BY_SUFFIX.get(suffix)
    if expected_mime is None:
        raise DomainCommandError(
            "unsupported_file_type", "Audio file has an unsupported suffix."
        )
    header, digest, size, _content = _inspect_audio_file(
        path,
        retain_content=False,
    )
    detected_mime = _detect_audio_mime(header, suffix)
    if detected_mime != expected_mime:
        raise DomainCommandError(
            "audio_mime_mismatch",
            "Audio contents do not match the supported file suffix.",
            status=415,
        )
    relative = path.relative_to(workspace).as_posix()
    return _AudioState(
        path=path,
        relative_path=relative,
        mime_type=detected_mime,
        suffix=suffix,
        size_bytes=size,
        sha256=digest,
    )


def _audio_snapshot(state: _AudioState) -> LocalAudioInput:
    header, digest, size, content = _inspect_audio_file(
        state.path,
        retain_content=True,
    )
    if (
        content is None
        or digest != state.sha256
        or size != state.size_bytes
        or _detect_audio_mime(header, state.suffix) != state.mime_type
    ):
        raise _state_changed(
            "The audio file changed while its confirmed snapshot was created."
        )
    return LocalAudioInput(
        relative_path=state.relative_path,
        display_name=_safe_metadata(state.path.name, max_chars=180),
        mime_type=state.mime_type,
        sha256=state.sha256,
        content=content,
    )


def _inspect_audio_file(
    path: Path,
    *,
    retain_content: bool,
) -> tuple[bytes, str, int, bytes | None]:
    """Open without following the final symlink and hash one stable inode."""

    try:
        path_before = path.stat(follow_symlinks=False)
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise _state_changed(
            "The audio file changed or became unavailable while it was read."
        ) from exc
    try:
        opened_before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened_before.st_mode)
            or _stat_identity(path_before) != _stat_identity(opened_before)
        ):
            raise _state_changed(
                "The audio path no longer identifies the confirmed regular file."
            )
        if opened_before.st_size <= 0:
            raise DomainCommandError(
                "audio_file_empty", "Audio file is empty.", status=409
            )
        if opened_before.st_size > MAX_AUDIO_BYTES:
            raise DomainCommandError(
                "workspace_file_too_large",
                f"Audio exceeds the {MAX_AUDIO_BYTES}-byte cap.",
                status=413,
            )
        digest = hashlib.sha256()
        header = bytearray()
        content = bytearray() if retain_content else None
        consumed = 0
        with os.fdopen(descriptor, "rb", closefd=True) as handle:
            descriptor = -1
            while chunk := handle.read(128 * 1024):
                consumed += len(chunk)
                if consumed > MAX_AUDIO_BYTES:
                    raise DomainCommandError(
                        "workspace_file_too_large",
                        f"Audio exceeds the {MAX_AUDIO_BYTES}-byte cap.",
                        status=413,
                    )
                if len(header) < 512:
                    header.extend(chunk[: 512 - len(header)])
                digest.update(chunk)
                if content is not None:
                    content.extend(chunk)
            opened_after = os.fstat(handle.fileno())
        try:
            path_after = path.stat(follow_symlinks=False)
        except OSError as exc:
            raise _state_changed(
                "The audio path changed while it was being read."
            ) from exc
        if (
            consumed != opened_after.st_size
            or _stat_identity(opened_before) != _stat_identity(opened_after)
            or _stat_identity(opened_after) != _stat_identity(path_after)
        ):
            raise _state_changed("The audio file changed while it was being read.")
        return (
            bytes(header),
            digest.hexdigest(),
            consumed,
            bytes(content) if content is not None else None,
        )
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _detect_audio_mime(header: bytes, suffix: str) -> str | None:
    if (
        suffix == ".wav"
        and len(header) >= 12
        and header[:4] == b"RIFF"
        and header[8:12] == b"WAVE"
    ):
        return "audio/wav"
    if suffix == ".flac" and header.startswith(b"fLaC"):
        return "audio/flac"
    if suffix in {".aif", ".aiff"} and (
        len(header) >= 12
        and header[:4] == b"FORM"
        and header[8:12] in {b"AIFF", b"AIFC"}
    ):
        return "audio/aiff"
    if suffix == ".m4a" and len(header) >= 12 and header[4:8] == b"ftyp":
        brands = header[8:64]
        if any(
            brand in brands
            for brand in (b"M4A ", b"M4B ", b"isom", b"mp41", b"mp42", b"qt  ")
        ):
            return "audio/mp4"
    if suffix == ".webm" and header.startswith(b"\x1aE\xdf\xa3"):
        return "audio/webm"
    if suffix in {".ogg", ".opus"} and header.startswith(b"OggS"):
        if suffix == ".opus" and b"OpusHead" not in header:
            return None
        return "audio/opus" if suffix == ".opus" else "audio/ogg"
    if suffix == ".mp3" and (
        header.startswith(b"ID3")
        or (
            len(header) >= 2
            and header[0] == 0xFF
            and header[1] & 0xE0 == 0xE0
            and header[1] & 0x06 != 0
        )
    ):
        return "audio/mpeg"
    if (
        suffix == ".aac"
        and len(header) >= 2
        and header[0] == 0xFF
        and header[1] & 0xF6 == 0xF0
    ):
        return "audio/aac"
    return None


def _backend_state(backend: LocalVoiceBackend) -> tuple[str, str]:
    name = str(backend.name or "").strip()
    if (
        not _BACKEND_NAME_RE.fullmatch(name)
        or redact_sensitive_text(name) != name
        or not callable(backend.transcribe)
    ):
        raise DomainCommandError(
            "voice_backend_invalid",
            "The configured local voice backend has invalid metadata.",
            status=503,
        )
    if not callable(backend.state_sha256):
        raise DomainCommandError(
            "voice_backend_invalid",
            "The local voice backend must expose a state digest.",
            status=503,
        )
    try:
        digest = str(backend.state_sha256() or "")
    except Exception as exc:  # noqa: BLE001
        raise _operation_failed(
            "Could not inspect the local voice backend state.", exc
        ) from exc
    if not _SHA256_RE.fullmatch(digest):
        raise DomainCommandError(
            "voice_backend_invalid",
            "The local voice backend state digest is invalid.",
            status=503,
        )
    return name, digest


# ---------------------------------------------------------------------------
# Shared plan/result and safety helpers
# ---------------------------------------------------------------------------


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
    state: Mapping[str, Any],
    preview: Mapping[str, Any],
    execute: Callable[[], DomainCommandResult],
) -> DomainCommandPlan:
    return DomainCommandPlan(
        name=name,
        risk=risk,
        reason=_safe_metadata(reason, max_chars=700),
        effects=tuple(_safe_metadata(item, max_chars=700) for item in effects),
        confirmation_scope=_fingerprint(state),
        preview=_safe_mapping(preview),
        execute=execute,
    )


def _result(
    stdout: str,
    payload: Mapping[str, Any],
    *,
    stderr: str = "",
) -> DomainCommandResult:
    safe_payload = _safe_mapping(payload)
    rendered = json.dumps(
        safe_payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    if len(rendered) > MAX_RESULT_CHARS:
        safe_payload = {
            "summary": _safe_display(rendered, max_chars=MAX_RESULT_CHARS),
            "truncated": True,
        }
    return DomainCommandResult(
        stdout=_safe_display(stdout, max_chars=MAX_RESULT_CHARS),
        payload=safe_payload,
        stderr=_safe_display(stderr, max_chars=MAX_RESULT_CHARS),
    )


def _safe_mapping(value: Mapping[str, Any]) -> dict[str, Any]:
    projected = _safe_value(dict(value), depth=0)
    return projected if isinstance(projected, dict) else {}


def _safe_value(value: Any, *, depth: int) -> Any:
    if depth >= 7:
        return "[depth limit]"
    if value is None or isinstance(value, bool | int):
        return value
    if isinstance(value, float):
        return value if value == value and abs(value) != float("inf") else None
    if isinstance(value, _PrivateText):
        return value
    if isinstance(value, str):
        return _safe_display(value, max_chars=MAX_SAFE_VALUE_CHARS)
    if isinstance(value, Mapping):
        output: dict[str, Any] = {}
        for index, (key, item) in enumerate(value.items()):
            if index >= 250:
                output["__truncated__"] = True
                break
            safe_key = _safe_metadata(key, max_chars=128)
            output[safe_key] = (
                "[redacted]"
                if _looks_secret_key(safe_key)
                else _safe_value(item, depth=depth + 1)
            )
        return output
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [
            _safe_value(item, depth=depth + 1)
            for item in islice(value, 500)
        ]
    return _safe_metadata(type(value).__name__, max_chars=128)


def _looks_secret_key(key: str) -> bool:
    normal = key.casefold().replace("-", "_")
    if normal.endswith("_sha256"):
        return False
    return any(
        marker in normal
        for marker in (
            "api_key",
            "api_token",
            "access_token",
            "refresh_token",
            "authorization",
            "client_secret",
            "password",
            "passwd",
            "secret",
        )
    ) or normal == "token"


def _safe_display(value: Any, *, max_chars: int) -> str:
    text = redact_sensitive_text(value, max_chars=max_chars)
    return _DISPLAY_CONTROL_RE.sub(" ", text)[:max_chars]


def _safe_metadata(value: Any, *, max_chars: int) -> str:
    text = redact_sensitive_text(value, max_chars=max_chars)
    return _METADATA_CONTROL_RE.sub(" ", text).strip()[:max_chars]


def _exact_sha256(value: Any, *, label: str) -> str:
    text = str(value or "")
    if not _SHA256_RE.fullmatch(text):
        raise DomainCommandError(
            "slash_arguments_invalid",
            f"{label} must be exactly 64 lowercase hexadecimal characters.",
        )
    return text


def _fingerprint(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=_json_default,
    ).encode("utf-8", errors="surrogatepass")
    return hashlib.sha256(encoded).hexdigest()


def _json_default(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, tuple):
        return list(value)
    raise TypeError(type(value).__name__)


def _stat_identity(value: os.stat_result) -> tuple[int, int, int, int, int]:
    return (
        int(value.st_dev),
        int(value.st_ino),
        int(value.st_size),
        int(value.st_mtime_ns),
        int(value.st_ctime_ns),
    )


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _fsync_directory(path: Path) -> None:
    try:
        directory_fd = os.open(path, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except OSError:
        pass


def _field(value: Any, name: str, default: Any = None) -> Any:
    if isinstance(value, Mapping):
        return value.get(name, default)
    return getattr(value, name, default)


def _state_changed(message: str) -> DomainCommandError:
    return DomainCommandError("confirmation_state_changed", message, status=409)


def _operation_failed(message: str, _exc: BaseException) -> DomainCommandError:
    return DomainCommandError("domain_operation_failed", message, status=502)


__all__ = [
    "DesktopTuiParityCommands",
    "DesktopTuiParitySources",
    "LocalAudioInput",
    "LocalVoiceBackend",
    "plan",
]
