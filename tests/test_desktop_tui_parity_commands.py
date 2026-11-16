"""Hermetic tests for the final native/TUI parity command forms."""

from __future__ import annotations

import base64
import hashlib
import os
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from care.desktop_domain_commands import DomainCommandError, DomainRisk
from care.desktop_tui_parity_commands import (
    MAX_AUDIO_BYTES,
    MAX_MEMORY_BYTES,
    MAX_TRANSCRIPT_CHARS,
    DesktopTuiParityCommands,
    DesktopTuiParitySources,
    LocalAudioInput,
    LocalVoiceBackend,
    plan,
)
from care.sandbox import SkillTrustStore


SHA_A = "a" * 64
SHA_B = "b" * 64
BACKEND_SHA = "c" * 64


def _b64url(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode()).rstrip(b"=").decode()


def _wav(payload: bytes = b"audio") -> bytes:
    size = len(payload) + 4
    return b"RIFF" + size.to_bytes(4, "little") + b"WAVE" + payload


class Harness:
    def __init__(self, workspace: Path) -> None:
        self.workspace = workspace
        self.memory_path = workspace / "config" / "CARE.md"
        self.trust_path = workspace / "state" / "trusted.json"
        self.backend_state = BACKEND_SHA
        self.transcribe_calls: list[LocalAudioInput] = []
        self.transcript = "hello from local whisper"
        self.backend_error: Exception | None = None
        self.backend: LocalVoiceBackend | None = LocalVoiceBackend(
            name="test-whisper",
            transcribe=self._transcribe,
            state_sha256=lambda: self.backend_state,
        )

    def _transcribe(self, audio: LocalAudioInput) -> str:
        self.transcribe_calls.append(audio)
        if self.backend_error is not None:
            raise self.backend_error
        return self.transcript

    def load_config(self) -> Any:
        return SimpleNamespace(
            context=SimpleNamespace(global_path=self.memory_path)
        )

    def sources(self) -> DesktopTuiParitySources:
        return DesktopTuiParitySources(
            load_config=self.load_config,
            memory_path=lambda config: config.context.global_path,
            load_trust_store=lambda: SkillTrustStore.load(
                path=self.trust_path
            ),
            workspace_root=lambda: self.workspace,
            load_voice_backend=lambda: self.backend,
        )

    def service(self) -> DesktopTuiParityCommands:
        return DesktopTuiParityCommands(self.sources())

    def trust_store(self) -> SkillTrustStore:
        return SkillTrustStore.load(path=self.trust_path)


def test_memory_edit_returns_bounded_read_only_editor_descriptor(
    tmp_path: Path,
) -> None:
    harness = Harness(tmp_path)
    harness.memory_path.parent.mkdir()
    content = "# CARE.md\n\nAnswer in Russian.\n"
    harness.memory_path.write_text(content)

    command = harness.service().plan("memory", ["edit"])

    assert command.risk is DomainRisk.READ_ONLY
    assert command.confirmation_scope == ""
    assert command.preview["base_sha256"] == hashlib.sha256(
        content.encode()
    ).hexdigest()
    result = command.execute()
    assert result.payload["content"] == content
    assert result.payload["editable"] is True
    assert result.payload["save_available"] is True
    assert result.payload["save_encoding"] == "base64url-utf8-unpadded"
    assert str(tmp_path) not in repr(command)
    assert str(tmp_path) not in repr(result)
    assert "Answer in Russian" not in repr(result)


def test_memory_edit_missing_document_has_empty_base_and_can_be_created(
    tmp_path: Path,
) -> None:
    harness = Harness(tmp_path)
    service = harness.service()
    descriptor = service.plan("memory", ["edit"]).execute()

    assert descriptor.payload["exists"] is False
    assert descriptor.payload["base_sha256"] == hashlib.sha256(b"").hexdigest()
    save = service.plan(
        "memory",
        [
            "edit",
            "save",
            descriptor.payload["base_sha256"],
            _b64url("# New CARE.md\n"),
        ],
    )
    assert save.preview["creates_file"] is True
    save.execute()
    assert harness.memory_path.read_text() == "# New CARE.md\n"
    assert not list(harness.memory_path.parent.glob(".care-md-*"))


def test_memory_save_is_confirmed_state_bound_atomic_and_preserves_mode(
    tmp_path: Path,
) -> None:
    harness = Harness(tmp_path)
    harness.memory_path.parent.mkdir()
    harness.memory_path.write_text("before\n")
    harness.memory_path.chmod(0o640)
    base = hashlib.sha256(b"before\n").hexdigest()
    encoded = _b64url("after\n")

    command = harness.service().plan(
        "memory", ["edit", "save", base, encoded]
    )

    assert command.risk is DomainRisk.FILESYSTEM_MUTATION
    assert command.confirmation_scope
    assert command.preview["new_sha256"] == hashlib.sha256(b"after\n").hexdigest()
    assert encoded not in repr(command.preview)
    assert harness.memory_path.read_text() == "before\n"
    result = command.execute()
    assert harness.memory_path.read_text() == "after\n"
    assert harness.memory_path.stat().st_mode & 0o777 == 0o640
    assert result.payload["saved"] is True
    assert encoded not in repr(result)
    assert not list(harness.memory_path.parent.glob(".care-md-*"))


def test_memory_save_rechecks_file_and_configured_target(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    harness.memory_path.parent.mkdir()
    harness.memory_path.write_text("base")
    base = hashlib.sha256(b"base").hexdigest()
    service = harness.service()

    changed_file = service.plan(
        "memory", ["edit", "save", base, _b64url("new")]
    )
    harness.memory_path.write_text("concurrent")
    with pytest.raises(DomainCommandError) as caught:
        changed_file.execute()
    assert caught.value.code == "confirmation_state_changed"
    assert harness.memory_path.read_text() == "concurrent"

    new_base = hashlib.sha256(b"concurrent").hexdigest()
    changed_target = service.plan(
        "memory", ["edit", "save", new_base, _b64url("new")]
    )
    old_target = harness.memory_path
    harness.memory_path = tmp_path / "other" / "CARE.md"
    with pytest.raises(DomainCommandError) as caught:
        changed_target.execute()
    assert caught.value.code == "confirmation_state_changed"
    assert old_target.read_text() == "concurrent"


def test_memory_save_rejects_stale_base_sensitive_and_noncanonical_content(
    tmp_path: Path,
) -> None:
    harness = Harness(tmp_path)
    harness.memory_path.parent.mkdir()
    harness.memory_path.write_text("safe")
    service = harness.service()

    with pytest.raises(DomainCommandError) as stale:
        service.plan("memory", ["edit", "save", "0" * 64, _b64url("new")])
    assert stale.value.code == "confirmation_state_changed"

    with pytest.raises(DomainCommandError) as secret:
        service.plan(
            "memory",
            [
                "edit",
                "save",
                hashlib.sha256(b"safe").hexdigest(),
                _b64url("api_key=sk-secret-value"),
            ],
        )
    assert secret.value.code == "memory_content_sensitive"

    for invalid in ("=", "YQ==", "a", "***"):
        with pytest.raises(DomainCommandError) as malformed:
            service.plan(
                "memory",
                [
                    "edit",
                    "save",
                    hashlib.sha256(b"safe").hexdigest(),
                    invalid,
                ],
            )
        assert malformed.value.code == "memory_content_invalid"


def test_memory_editor_redacts_existing_credentials_and_disables_save(
    tmp_path: Path,
) -> None:
    harness = Harness(tmp_path)
    harness.memory_path.parent.mkdir()
    harness.memory_path.write_text("api_key=sk-super-secret\n")
    service = harness.service()

    result = service.plan("memory", ["edit"]).execute()

    assert "sk-super-secret" not in repr(result)
    assert "[redacted]" in result.payload["content"]
    assert result.payload["content_redacted"] is True
    assert result.payload["save_available"] is False
    with pytest.raises(DomainCommandError) as caught:
        service.plan(
            "memory",
            [
                "edit",
                "save",
                result.payload["base_sha256"],
                _b64url("replacement"),
            ],
        )
    assert caught.value.code == "memory_content_sensitive"


def test_memory_editor_rejects_symlink_non_utf8_and_oversized_files(
    tmp_path: Path,
) -> None:
    harness = Harness(tmp_path)
    harness.memory_path.parent.mkdir()
    outside = tmp_path / "outside.md"
    outside.write_text("outside")
    harness.memory_path.symlink_to(outside)
    with pytest.raises(DomainCommandError) as symlinked:
        harness.service().plan("memory", ["edit"])
    assert symlinked.value.code == "unsafe_memory_target"
    harness.memory_path.unlink()

    harness.memory_path.write_bytes(b"\xff")
    with pytest.raises(DomainCommandError) as non_utf8:
        harness.service().plan("memory", ["edit"])
    assert non_utf8.value.code == "memory_document_not_utf8"

    with harness.memory_path.open("wb") as handle:
        handle.truncate(MAX_MEMORY_BYTES + 1)
    with pytest.raises(DomainCommandError) as oversized:
        harness.service().plan("memory", ["edit"])
    assert oversized.value.code == "memory_document_too_large"


def test_sandbox_list_is_bounded_redacted_and_does_not_expose_exact_sha_or_uri(
    tmp_path: Path,
) -> None:
    harness = Harness(tmp_path)
    store = harness.trust_store()
    store.trust(
        sha256=SHA_A,
        uri="file:///private/skills/pdf?token=uri-secret",
        name="pdf-reader",
        allowed_tools=("Read", "Authorization: Bearer tool-secret-value"),
        approved_at=datetime(2026, 8, 31, tzinfo=timezone.utc),
    )

    result = harness.service().plan("sandbox", ["list"]).execute()

    assert result.payload["mutable"] is True
    assert result.payload["revoke_form"].endswith("<exact-sha256>")
    row = result.payload["trusted_skills"][0]
    assert row["sha256_prefix"] == SHA_A[:12]
    assert SHA_A not in repr(result)
    assert "file:///private" not in repr(result)
    assert "uri-secret" not in repr(result)
    assert "tool-secret-value" not in repr(result)
    assert "[redacted]" in row["allowed_tools"][1]


def test_sandbox_revoke_is_confirmed_exact_rechecked_and_persisted(
    tmp_path: Path,
) -> None:
    harness = Harness(tmp_path)
    store = harness.trust_store()
    store.trust(
        sha256=SHA_A,
        uri="local:///skills/pdf",
        name="pdf-reader",
        approved_at=datetime(2026, 8, 31, tzinfo=timezone.utc),
    )
    command = harness.service().plan("sandbox", ["revoke", SHA_A])

    assert command.risk is DomainRisk.FILESYSTEM_MUTATION
    assert command.confirmation_scope
    assert command.preview["sha256_prefix"] == SHA_A[:12]
    assert SHA_A not in repr(command.preview)
    assert harness.trust_store().is_trusted(SHA_A)
    result = command.execute()
    assert result.payload["revoked"] is True
    assert not harness.trust_store().is_trusted(SHA_A)


def test_sandbox_revoke_fails_closed_when_any_store_state_changes(
    tmp_path: Path,
) -> None:
    harness = Harness(tmp_path)
    store = harness.trust_store()
    store.trust(sha256=SHA_A, uri="local:///a", name="a")
    command = harness.service().plan("sandbox", ["revoke", SHA_A])
    changed = harness.trust_store()
    changed.trust(sha256=SHA_B, uri="local:///b", name="b")

    with pytest.raises(DomainCommandError) as caught:
        command.execute()

    assert caught.value.code == "confirmation_state_changed"
    assert harness.trust_store().is_trusted(SHA_A)
    assert harness.trust_store().is_trusted(SHA_B)


@pytest.mark.parametrize("sha", ["a" * 12, "A" * 64, "g" * 64, ""])
def test_sandbox_revoke_requires_exact_lowercase_sha(
    tmp_path: Path,
    sha: str,
) -> None:
    with pytest.raises(DomainCommandError) as caught:
        Harness(tmp_path).service().plan("sandbox", ["revoke", sha])
    assert caught.value.code == "slash_arguments_invalid"


def test_sandbox_revoke_unknown_sha_is_not_presented_for_confirmation(
    tmp_path: Path,
) -> None:
    with pytest.raises(DomainCommandError) as caught:
        Harness(tmp_path).service().plan("sandbox", ["revoke", SHA_A])
    assert caught.value.code == "trust_record_not_found"
    assert caught.value.status == 404


def test_voice_status_is_read_only_and_never_invokes_backend(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    result = harness.service().plan("voice", ["status"]).execute()
    assert result.payload["available"] is True
    assert result.payload["backend"] == "test-whisper"
    assert result.payload["local_only"] is True
    assert harness.transcribe_calls == []

    harness.backend = None
    unavailable = harness.service().plan("voice", []).execute()
    assert unavailable.payload["available"] is False


def test_voice_transcribe_is_confirmed_workspace_bound_and_local(
    tmp_path: Path,
) -> None:
    harness = Harness(tmp_path)
    audio = tmp_path / "audio" / "sample.wav"
    audio.parent.mkdir()
    audio.write_bytes(_wav())

    command = harness.service().plan(
        "voice", ["transcribe", "audio/sample.wav"]
    )

    assert command.risk is DomainRisk.EXTERNAL_SIDE_EFFECT
    assert command.confirmation_scope
    assert command.preview["relative_path"] == "audio/sample.wav"
    assert command.preview["mime_type"] == "audio/wav"
    assert command.preview["local_only"] is True
    assert "no network or shell implementation" in command.reason
    assert str(tmp_path) not in repr(command)
    assert harness.transcribe_calls == []
    result = command.execute()
    assert len(harness.transcribe_calls) == 1
    supplied = harness.transcribe_calls[0]
    assert supplied.relative_path == "audio/sample.wav"
    assert supplied.content == audio.read_bytes()
    assert str(tmp_path) not in repr(supplied)
    assert result.payload["transcript"] == "hello from local whisper"
    assert result.payload["truncated"] is False
    assert "hello from local whisper" not in repr(result)


def test_voice_rechecks_audio_and_backend_state_before_invocation(
    tmp_path: Path,
) -> None:
    harness = Harness(tmp_path)
    audio = tmp_path / "sample.wav"
    audio.write_bytes(_wav(b"one"))
    service = harness.service()

    changed_audio = service.plan("voice", ["transcribe", "sample.wav"])
    audio.write_bytes(_wav(b"two"))
    with pytest.raises(DomainCommandError) as file_changed:
        changed_audio.execute()
    assert file_changed.value.code == "confirmation_state_changed"
    assert harness.transcribe_calls == []

    backend_changed = service.plan("voice", ["transcribe", "sample.wav"])
    harness.backend_state = "d" * 64
    with pytest.raises(DomainCommandError) as model_changed:
        backend_changed.execute()
    assert model_changed.value.code == "confirmation_state_changed"
    assert harness.transcribe_calls == []


def test_voice_rejects_outside_symlink_disguised_empty_and_oversized_audio(
    tmp_path: Path,
) -> None:
    harness = Harness(tmp_path)
    outside = tmp_path.parent / f"outside-{tmp_path.name}.wav"
    outside.write_bytes(_wav())
    try:
        with pytest.raises(DomainCommandError) as escaped:
            harness.service().plan("voice", ["transcribe", str(outside)])
        assert escaped.value.code == "unsafe_workspace_path"

        link = tmp_path / "linked.wav"
        link.symlink_to(outside)
        with pytest.raises(DomainCommandError) as symlinked:
            harness.service().plan("voice", ["transcribe", "linked.wav"])
        assert symlinked.value.code == "unsafe_workspace_path"
    finally:
        outside.unlink(missing_ok=True)

    disguised = tmp_path / "fake.wav"
    disguised.write_bytes(b"not audio")
    with pytest.raises(DomainCommandError) as mime:
        harness.service().plan("voice", ["transcribe", "fake.wav"])
    assert mime.value.code == "audio_mime_mismatch"

    wrong_suffix = tmp_path / "sample.txt"
    wrong_suffix.write_bytes(_wav())
    with pytest.raises(DomainCommandError) as suffix:
        harness.service().plan("voice", ["transcribe", "sample.txt"])
    assert suffix.value.code == "unsupported_file_type"

    empty = tmp_path / "empty.wav"
    empty.touch()
    with pytest.raises(DomainCommandError) as empty_error:
        harness.service().plan("voice", ["transcribe", "empty.wav"])
    assert empty_error.value.code == "audio_file_empty"

    large = tmp_path / "large.wav"
    with large.open("wb") as handle:
        handle.write(_wav())
        handle.truncate(MAX_AUDIO_BYTES + 1)
    with pytest.raises(DomainCommandError) as oversized:
        harness.service().plan("voice", ["transcribe", "large.wav"])
    assert oversized.value.code == "workspace_file_too_large"


def test_voice_output_is_bounded_and_redacted(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    audio = tmp_path / "sample.wav"
    audio.write_bytes(_wav())
    secret = "sk-never-return-this"
    harness.transcript = "api_key=" + secret + " " + ("word " * MAX_TRANSCRIPT_CHARS)

    result = harness.service().plan(
        "voice", ["transcribe", "sample.wav"]
    ).execute()

    assert result.payload["truncated"] is True
    assert result.payload["redacted"] is True
    assert len(result.payload["transcript"]) <= MAX_TRANSCRIPT_CHARS
    assert secret not in repr(result)
    assert "[redacted]" in result.payload["transcript"]


def test_voice_backend_failure_is_stable_and_does_not_leak_details(
    tmp_path: Path,
) -> None:
    harness = Harness(tmp_path)
    audio = tmp_path / "sample.wav"
    audio.write_bytes(_wav())
    harness.backend_error = RuntimeError(
        "/private/model.bin Authorization: Bearer backend-secret"
    )
    command = harness.service().plan("voice", ["transcribe", "sample.wav"])

    with pytest.raises(DomainCommandError) as caught:
        command.execute()

    assert caught.value.code == "domain_operation_failed"
    assert "backend-secret" not in str(caught.value)
    assert "/private/model.bin" not in str(caught.value)


def test_voice_requires_configured_backend_and_valid_state_digest(
    tmp_path: Path,
) -> None:
    harness = Harness(tmp_path)
    (tmp_path / "sample.wav").write_bytes(_wav())
    harness.backend = None
    with pytest.raises(DomainCommandError) as missing:
        harness.service().plan("voice", ["transcribe", "sample.wav"])
    assert missing.value.code == "voice_backend_unavailable"

    harness.backend = LocalVoiceBackend(
        name="local",
        transcribe=lambda _path: "ok",
        state_sha256=lambda: "not-a-sha",
    )
    with pytest.raises(DomainCommandError) as invalid:
        harness.service().plan("voice", ["transcribe", "sample.wav"])
    assert invalid.value.code == "voice_backend_invalid"


def test_module_plan_entrypoint_and_strict_forms(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    status = plan("voice", ["status"], sources=harness.sources()).execute()
    assert status.payload["available"] is True

    service = harness.service()
    for name, args in (
        ("unknown", []),
        ("memory", ["show"]),
        ("sandbox", ["revoke"]),
        ("voice", ["transcribe"]),
    ):
        with pytest.raises(DomainCommandError):
            service.plan(name, args)


def test_memory_empty_save_creates_real_file(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    empty_sha = hashlib.sha256(b"").hexdigest()
    command = harness.service().plan(
        "memory", ["edit", "save", empty_sha, ""]
    )
    assert command.preview["changed"] is True
    result = command.execute()
    assert result.payload["changed"] is True
    assert harness.memory_path.is_file()
    assert harness.memory_path.read_bytes() == b""
    assert harness.memory_path.stat().st_mode & 0o777 == 0o600


def test_memory_decoded_size_is_bounded_before_write(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    encoded = base64.urlsafe_b64encode(b"x" * (MAX_MEMORY_BYTES + 1)).rstrip(
        b"="
    ).decode()
    with pytest.raises(DomainCommandError) as caught:
        harness.service().plan(
            "memory",
            ["edit", "save", hashlib.sha256(b"").hexdigest(), encoded],
        )
    assert caught.value.code == "memory_content_invalid"
    assert caught.value.status == 413
    assert not harness.memory_path.exists()


def test_voice_static_state_digest_is_deterministic(
    tmp_path: Path,
) -> None:
    harness = Harness(tmp_path)
    (tmp_path / "sample.wav").write_bytes(_wav())
    harness.backend = LocalVoiceBackend(
        name="static-local",
        transcribe=lambda _audio: "static transcript",
        state_sha256=lambda: "f" * 64,
    )
    first = harness.service().plan("voice", ["transcribe", "sample.wav"])
    second = harness.service().plan("voice", ["transcribe", "sample.wav"])
    assert first.confirmation_scope == second.confirmation_scope
    assert first.execute().payload["transcript"] == "static transcript"


def test_voice_supported_mime_signatures(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    fixtures = {
        "a.mp3": b"ID3\x04\x00\x00payload",
        "a.flac": b"fLaCpayload",
        "a.ogg": b"OggS" + b"payload",
        "a.opus": b"OggS" + b"\x00" * 20 + b"OpusHead",
        "a.aac": b"\xff\xf1payload",
        "a.aiff": b"FORM\x00\x00\x00\x08AIFFpayload",
        "a.m4a": b"\x00\x00\x00\x18ftypM4A payload",
        "a.webm": b"\x1aE\xdf\xa3payload",
    }
    for filename, body in fixtures.items():
        (tmp_path / filename).write_bytes(body)
        command = harness.service().plan(
            "voice", ["transcribe", filename]
        )
        assert command.preview["mime_type"].startswith("audio/")


def test_memory_save_wrong_arg_shape_does_not_decode_or_write(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    with pytest.raises(DomainCommandError) as caught:
        harness.service().plan(
            "memory",
            ["edit", "save", hashlib.sha256(b"").hexdigest()],
        )
    assert caught.value.code == "slash_arguments_invalid"
    assert not harness.memory_path.exists()


def test_backend_state_digest_never_appears_in_public_output(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    (tmp_path / "sample.wav").write_bytes(_wav())
    command = harness.service().plan("voice", ["transcribe", "sample.wav"])
    assert BACKEND_SHA not in repr(command.preview)
    result = command.execute()
    assert BACKEND_SHA not in repr(result)


def test_memory_file_permission_change_invalidates_confirmation(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    harness.memory_path.parent.mkdir()
    harness.memory_path.write_text("base")
    harness.memory_path.chmod(0o600)
    command = harness.service().plan(
        "memory",
        [
            "edit",
            "save",
            hashlib.sha256(b"base").hexdigest(),
            _b64url("new"),
        ],
    )
    os.chmod(harness.memory_path, 0o640)
    with pytest.raises(DomainCommandError) as caught:
        command.execute()
    assert caught.value.code == "confirmation_state_changed"
    assert harness.memory_path.read_text() == "base"


def test_mutation_confirmation_scopes_bind_exact_content_store_audio_and_backend(
    tmp_path: Path,
) -> None:
    harness = Harness(tmp_path)
    harness.memory_path.parent.mkdir()
    harness.memory_path.write_text("base")
    base = hashlib.sha256(b"base").hexdigest()
    memory_a = harness.service().plan(
        "memory", ["edit", "save", base, _b64url("first")]
    )
    memory_b = harness.service().plan(
        "memory", ["edit", "save", base, _b64url("second")]
    )
    assert memory_a.confirmation_scope != memory_b.confirmation_scope

    store = harness.trust_store()
    store.trust(
        sha256=SHA_A,
        uri="local:///a",
        name="a",
        approved_at=datetime(2026, 8, 30, tzinfo=timezone.utc),
    )
    sandbox_a = harness.service().plan("sandbox", ["revoke", SHA_A])
    store = harness.trust_store()
    store.trust(
        sha256=SHA_A,
        uri="local:///a",
        name="a",
        allowed_tools=("Read",),
        approved_at=datetime(2026, 8, 31, tzinfo=timezone.utc),
    )
    sandbox_b = harness.service().plan("sandbox", ["revoke", SHA_A])
    assert sandbox_a.confirmation_scope != sandbox_b.confirmation_scope

    audio = tmp_path / "sample.wav"
    audio.write_bytes(_wav(b"one"))
    voice_a = harness.service().plan("voice", ["transcribe", "sample.wav"])
    audio.write_bytes(_wav(b"two"))
    voice_b = harness.service().plan("voice", ["transcribe", "sample.wav"])
    assert voice_a.confirmation_scope != voice_b.confirmation_scope
    harness.backend_state = "e" * 64
    voice_c = harness.service().plan("voice", ["transcribe", "sample.wav"])
    assert voice_b.confirmation_scope != voice_c.confirmation_scope
