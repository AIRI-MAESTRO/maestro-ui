"""Hermetic tests for the native settings bridge."""

from __future__ import annotations

import json
import os
import stat
import tomllib
from pathlib import Path
from typing import Any

import pytest

import care.desktop_settings as desktop_settings
from care.config import CareConfig, MageConfig
from care.desktop_settings import DesktopSettingsBridge
from care.dotenv import load_env_file
from care.runtime.keystore import FileKeystore, Keystore


SENSITIVE_SENTINEL = "SENSITIVE_TEST_SENTINEL"
REPLACEMENT_SENTINEL = "REPLACEMENT_TEST_SENTINEL"
SECRET_VERSION = "0123456789abcdef"


class FakeSecureKeystore(Keystore):
    name = "test-keychain"
    persistent = True

    def __init__(self) -> None:
        self.values: dict[tuple[str, str], str] = {}

    @classmethod
    def available(cls) -> bool:
        return True

    def store(self, service: str, key: str, value: str) -> None:
        self.values[(service, key)] = value

    def fetch(self, service: str, key: str) -> str | None:
        return self.values.get((service, key))

    def delete(self, service: str, key: str) -> None:
        self.values.pop((service, key), None)


def _bridge(
    tmp_path: Path,
    config: CareConfig,
    keystore: Keystore,
) -> DesktopSettingsBridge:
    return DesktopSettingsBridge(
        config_loader=lambda: config,
        config_path=tmp_path / "config.toml",
        env_path=tmp_path / ".env",
        keystore_factory=lambda: keystore,
        token_factory=lambda: "settings-confirmation",
        secret_key_factory=lambda: SECRET_VERSION,
        environ={},
    )


def _confirm(
    bridge: DesktopSettingsBridge,
    payload: dict[str, Any],
) -> Any:
    challenge = bridge.update(payload)
    assert challenge.status == 409
    token = challenge.payload["confirmation"]["token"]
    return bridge.update(payload, confirmation_token=token)


def test_snapshot_never_returns_existing_secrets(tmp_path: Path) -> None:
    config = CareConfig(
        mage=MageConfig(
            api_key=SENSITIVE_SENTINEL,
            web_search_api_key=SENSITIVE_SENTINEL,
            base_url="https://openrouter.ai/api/v1",
            model="example/model",
        )
    )
    config.memory.api_key = SENSITIVE_SENTINEL
    config.platform.api_key = SENSITIVE_SENTINEL
    config.upload.api_key = SENSITIVE_SENTINEL
    snapshot = _bridge(tmp_path, config, FakeSecureKeystore()).snapshot()
    serialised = json.dumps(snapshot)

    assert SENSITIVE_SENTINEL not in serialised
    assert "api_key" not in snapshot["settings"]
    assert "search_api_key" not in snapshot["settings"]
    assert "memory_api_key" not in snapshot["settings"]
    assert "platform_api_key" not in snapshot["settings"]
    assert "upload_api_key" not in snapshot["settings"]
    assert snapshot["settings"]["api_key_present"] is True
    assert snapshot["settings"]["search_api_key_present"] is True
    assert snapshot["settings"]["memory_api_key_present"] is True
    assert snapshot["settings"]["platform_api_key_present"] is True
    assert snapshot["settings"]["upload_api_key_present"] is True
    assert snapshot["storage"]["existing_secrets_returned"] is False
    assert snapshot["storage"]["config_target"] == "config.toml"
    assert snapshot["storage"]["env_target"] == ".env"
    assert str(tmp_path) not in serialised


@pytest.mark.parametrize("explicit_config_path", [False, True])
def test_default_loader_never_resolves_keychain_secrets(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    explicit_config_path: bool,
) -> None:
    calls: list[dict[str, Any]] = []

    def raw_loader(**kwargs: Any) -> CareConfig:
        calls.append(kwargs)
        return CareConfig()

    monkeypatch.setattr(CareConfig, "load", raw_loader)
    kwargs: dict[str, Any] = {
        "cwd": tmp_path,
        "env_path": tmp_path / ".env",
        "environ": {},
    }
    if explicit_config_path:
        kwargs["config_path"] = tmp_path / "config.toml"
    bridge = DesktopSettingsBridge(**kwargs)

    bridge.snapshot()

    assert len(calls) == 1
    assert calls[0]["resolve_secrets"] is False


def test_service_keys_are_write_only_versioned_keychain_references(
    tmp_path: Path,
) -> None:
    memory_secret = "MEMORY_REPLACEMENT_TEST_SENTINEL"
    platform_secret = "PLATFORM_REPLACEMENT_TEST_SENTINEL"
    upload_secret = "UPLOAD_REPLACEMENT_TEST_SENTINEL"
    config_path = tmp_path / "config.toml"
    env_path = tmp_path / ".env"
    keystore = FakeSecureKeystore()
    bridge = DesktopSettingsBridge(
        config_loader=CareConfig,
        config_path=config_path,
        env_path=env_path,
        cwd=tmp_path,
        keystore_factory=lambda: keystore,
        token_factory=lambda: "service-key-token",
        secret_key_factory=lambda: SECRET_VERSION,
        environ={},
    )

    result = _confirm(
        bridge,
        {
            "memory_api_key_action": "replace",
            "memory_api_key": memory_secret,
            "platform_api_key_action": "replace",
            "platform_api_key": platform_secret,
            "upload_api_key_action": "replace",
            "upload_api_key": upload_secret,
        },
    )

    assert result.status == 200
    serialised = json.dumps(result.payload)
    config_body = config_path.read_text(encoding="utf-8")
    env_body = env_path.read_text(encoding="utf-8")
    for secret in (memory_secret, platform_secret, upload_secret):
        assert secret not in serialised
        assert secret not in config_body
        assert secret not in env_body
    for parent, secret in (
        ("memory", memory_secret),
        ("platform", platform_secret),
        ("upload", upload_secret),
    ):
        key = f"{parent}.api_key.{SECRET_VERSION}"
        reference = f"keystore://care/{key}"
        assert keystore.fetch("care", key) == secret
        assert reference in config_body
        assert f"CARE_{parent.upper()}__API_KEY={reference}" in env_body
        assert result.payload["settings"][f"{parent}_api_key_present"] is True


@pytest.mark.parametrize("action", ["keep", "clear"])
def test_service_key_keep_and_clear_actions(
    action: str,
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "config.toml"
    env_path = tmp_path / ".env"
    persisted = CareConfig()
    current = CareConfig()
    keystore = FakeSecureKeystore()
    references: dict[str, str] = {}
    for index, parent in enumerate(("memory", "platform", "upload"), start=1):
        key = f"{parent}.api_key.{str(index) * 16}"
        reference = f"keystore://care/{key}"
        secret = f"{parent.upper()}_EXISTING_TEST_SENTINEL"
        getattr(persisted, parent).api_key = reference
        getattr(current, parent).api_key = secret
        keystore.store("care", key, secret)
        references[parent] = reference
    config_path.write_text(
        desktop_settings._format_toml(persisted.model_dump(mode="json")),
        encoding="utf-8",
    )
    bridge = DesktopSettingsBridge(
        config_loader=lambda: current,
        config_path=config_path,
        env_path=env_path,
        cwd=tmp_path,
        keystore_factory=lambda: keystore,
        token_factory=lambda: f"service-{action}-token",
        secret_key_factory=lambda: SECRET_VERSION,
        environ={},
    )
    payload: dict[str, Any] = {"reduced_motion": True}
    if action == "clear":
        payload.update(
            {
                "memory_api_key_action": "clear",
                "platform_api_key_action": "clear",
                "upload_api_key_action": "clear",
            }
        )

    result = _confirm(bridge, payload)

    assert result.status == 200
    config_body = config_path.read_text(encoding="utf-8")
    env_body = env_path.read_text(encoding="utf-8")
    for parent, reference in references.items():
        if action == "keep":
            assert reference in config_body
            assert reference in env_body
            assert result.payload["settings"][f"{parent}_api_key_present"] is True
        else:
            assert reference not in config_body
            assert reference not in env_body
            assert result.payload["settings"][f"{parent}_api_key_present"] is False
            _service, key = reference.removeprefix("keystore://").split("/", 1)
            assert keystore.fetch("care", key) is None


def test_deepseek_provider_is_first_class_and_model_stays_free_text(
    tmp_path: Path,
) -> None:
    config = CareConfig(
        mage=MageConfig(
            base_url="https://api.deepseek.com",
            model="organisation/future-model-name",
        )
    )
    bridge = _bridge(tmp_path, config, FakeSecureKeystore())

    snapshot = bridge.snapshot()
    assert snapshot["settings"]["provider"] == "deepseek"
    assert "deepseek" in snapshot["choices"]["provider"]
    saved = _confirm(
        bridge,
        {
            "provider": "deepseek",
            "base_url": "https://api.deepseek.com",
            "model": "organisation/another-future-model",
        },
    )
    assert saved.status == 200


def test_accidental_secret_shape_in_nonsecret_field_is_redacted(
    tmp_path: Path,
) -> None:
    accidental = "sk-this-is-a-synthetic-test-value"
    config = CareConfig(
        mage=MageConfig(
            base_url="https://api.deepseek.com",
            model=accidental,
        )
    )
    bridge = _bridge(tmp_path, config, FakeSecureKeystore())

    snapshot = bridge.snapshot()
    assert accidental not in json.dumps(snapshot)
    challenge = bridge.update({"model": "sk-another-synthetic-test-value"})
    assert challenge.status == 400
    assert "sk-another" not in json.dumps(challenge.payload)
    assert not (tmp_path / "config.toml").exists()
    assert not (tmp_path / ".env").exists()

    url_secret = "sk-synthetic-custom-path-secret"
    url_result = bridge.update(
        {
            "provider": "custom",
            "base_url": f"https://models.example.test/{url_secret}",
        }
    )
    assert url_result.status == 400
    assert url_secret not in json.dumps(url_result.payload)
    assert not (tmp_path / "config.toml").exists()
    assert not (tmp_path / ".env").exists()


@pytest.mark.parametrize(
    ("provider", "base_url"),
    [
        ("openrouter", "http://openrouter.ai/api/v1"),
        ("deepseek", "http://api.deepseek.com"),
        ("openai", "http://api.openai.com/v1"),
        ("custom", "http://models.example.test/v1"),
        ("openrouter", "https://openrouter.ai/wrong-path"),
        ("custom", "https://api.deepseek.com"),
        ("deepseek", "https://sub.api.deepseek.com"),
        ("deepseek", "https://api.deepseek.com:8443"),
    ],
)
def test_provider_transport_policy_rejects_insecure_or_wrong_endpoints(
    tmp_path: Path,
    provider: str,
    base_url: str,
) -> None:
    config = CareConfig(mage=MageConfig(model="example/model"))
    result = _bridge(tmp_path, config, FakeSecureKeystore()).update(
        {"provider": provider, "base_url": base_url}
    )
    assert result.status == 400
    assert result.payload["error"]["code"] == "invalid_settings"


@pytest.mark.parametrize(
    ("provider", "base_url"),
    [
        ("custom", "http://127.0.0.1:11434/v1"),
        ("custom", "http://localhost:8000/v1"),
        ("deepseek", "https://api.deepseek.com/v1"),
    ],
)
def test_provider_transport_policy_accepts_loopback_or_https(
    tmp_path: Path,
    provider: str,
    base_url: str,
) -> None:
    config = CareConfig(mage=MageConfig(model="example/model"))
    result = _bridge(tmp_path, config, FakeSecureKeystore()).update(
        {"provider": provider, "base_url": base_url}
    )
    assert result.status == 409


@pytest.mark.parametrize(
    "payload",
    [
        {"provider": "unknown"},
        {"base_url": "file:///tmp/provider"},
        {"model": "bad\nmodel"},
        {"default_mode": "surprise"},
        {"api_key_action": "replace"},
        {"api_key_action": "keep", "api_key": SENSITIVE_SENTINEL},
        {"unexpected": True},
    ],
)
def test_validation_rejects_before_confirmation_or_write(
    tmp_path: Path,
    payload: dict[str, Any],
) -> None:
    config_path = tmp_path / "config.toml"
    env_path = tmp_path / ".env"
    keystore = FakeSecureKeystore()
    bridge = DesktopSettingsBridge(
        config_loader=CareConfig,
        config_path=config_path,
        env_path=env_path,
        keystore_factory=lambda: keystore,
        secret_key_factory=lambda: SECRET_VERSION,
        environ={},
    )

    result = bridge.update(payload)

    assert result.status == 400
    assert result.payload["error"]["code"] == "invalid_settings"
    assert not config_path.exists()
    assert not env_path.exists()
    assert keystore.values == {}


def test_confirmation_is_bound_one_use_and_secret_safe(tmp_path: Path) -> None:
    bridge = _bridge(tmp_path, CareConfig(), FakeSecureKeystore())
    payload = {
        "model": "example/new-model",
        "api_key_action": "replace",
        "api_key": REPLACEMENT_SENTINEL,
    }

    challenge = bridge.update(payload)
    rendered = json.dumps(challenge.payload)
    assert challenge.status == 409
    assert REPLACEMENT_SENTINEL not in rendered

    token = challenge.payload["confirmation"]["token"]
    mismatch = bridge.update(
        {"model": "example/other-model"},
        confirmation_token=token,
    )
    assert mismatch.status == 409
    assert mismatch.payload["error"]["code"] == "confirmation_invalid"

    second_challenge = bridge.update(payload)
    token = second_challenge.payload["confirmation"]["token"]
    saved = bridge.update(payload, confirmation_token=token)
    assert saved.status == 200
    replay = bridge.update(payload, confirmation_token=token)
    assert replay.status == 409
    assert replay.payload["error"]["code"] == "confirmation_invalid"


def test_upload_key_confirmation_is_bound_to_hidden_service_boundary(
    tmp_path: Path,
) -> None:
    current = CareConfig()
    current.upload.url = "https://upload-a.example.test/chains"
    bridge = _bridge(tmp_path, current, FakeSecureKeystore())
    payload = {
        "upload_api_key_action": "replace",
        "upload_api_key": REPLACEMENT_SENTINEL,
    }

    challenge = bridge.update(payload)
    assert challenge.status == 409
    current.upload.url = "https://upload-b.example.test/chains"
    result = bridge.update(
        payload,
        confirmation_token=challenge.payload["confirmation"]["token"],
    )

    assert result.status == 409
    assert result.payload["error"]["code"] == "confirmation_invalid"
    assert REPLACEMENT_SENTINEL not in json.dumps(result.payload)


def test_platform_key_confirmation_is_bound_to_resolved_master_endpoint(
    tmp_path: Path,
) -> None:
    current = CareConfig()
    current.platform.base_url = "http://localhost:8001"
    current.platform.master_base_url = "https://platform-a.example.test"
    bridge = _bridge(tmp_path, current, FakeSecureKeystore())
    payload = {
        "platform_api_key_action": "replace",
        "platform_api_key": REPLACEMENT_SENTINEL,
    }

    challenge = bridge.update(payload)
    assert challenge.status == 409
    current.platform.master_base_url = "https://platform-b.example.test"
    result = bridge.update(
        payload,
        confirmation_token=challenge.payload["confirmation"]["token"],
    )

    assert result.status == 409
    assert result.payload["error"]["code"] == "confirmation_invalid"
    assert REPLACEMENT_SENTINEL not in json.dumps(result.payload)


def test_upload_key_replace_rejects_insecure_remote_target(tmp_path: Path) -> None:
    current = CareConfig()
    current.upload.url = "http://upload.example.test/chains"
    result = _bridge(tmp_path, current, FakeSecureKeystore()).update(
        {
            "upload_api_key_action": "replace",
            "upload_api_key": REPLACEMENT_SENTINEL,
        }
    )

    assert result.status == 400
    assert result.payload["error"]["code"] == "invalid_settings"
    assert REPLACEMENT_SENTINEL not in json.dumps(result.payload)
    assert not (tmp_path / "config.toml").exists()
    assert not (tmp_path / ".env").exists()


def test_save_uses_keychain_references_and_removes_plaintext_env_override(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = CareConfig(
        mage=MageConfig(
            api_key=SENSITIVE_SENTINEL,
            base_url="https://openrouter.ai/api/v1",
            model="example/old-model",
        )
    )
    config_path = tmp_path / "config.toml"
    env_path = tmp_path / ".env"
    env_path.write_text(
        (
            f"CARE_MAGE__API_KEY={SENSITIVE_SENTINEL}\n"
            f"export CARE_MAGE__API_KEY={SENSITIVE_SENTINEL}\n"
            f"CARE_MAGE__API_KEY={SENSITIVE_SENTINEL}\n"
            "# CARE_MAGE__API_KEY remains managed by Maestro\n"
            "KEEP_ME=yes\n"
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("CARE_MAGE__API_KEY", SENSITIVE_SENTINEL)
    keystore = FakeSecureKeystore()
    bridge = DesktopSettingsBridge(
        config_loader=lambda: config,
        config_path=config_path,
        env_path=env_path,
        keystore_factory=lambda: keystore,
        token_factory=lambda: "save-token",
        secret_key_factory=lambda: SECRET_VERSION,
        environ={"CARE_MAGE__API_KEY": SENSITIVE_SENTINEL},
    )

    result = _confirm(bridge, {"model": "example/new-model"})

    assert result.status == 200
    assert SENSITIVE_SENTINEL not in json.dumps(result.payload)
    config_body = config_path.read_text(encoding="utf-8")
    env_body = env_path.read_text(encoding="utf-8")
    assert SENSITIVE_SENTINEL not in config_body
    assert SENSITIVE_SENTINEL not in env_body
    reference = f"keystore://care/mage.api_key.{SECRET_VERSION}"
    assert f'api_key = "{reference}"' in config_body
    assert f"CARE_MAGE__API_KEY={reference}" in env_body
    assert env_body.count("CARE_MAGE__API_KEY=") == 1
    assert env_body.splitlines().count(
        f"CARE_MAGE__API_KEY={reference}"
    ) == 1
    assert "export CARE_MAGE__API_KEY" not in env_body
    assert "# CARE_MAGE__API_KEY remains managed by Maestro" in env_body
    assert "KEEP_ME=yes" in env_body
    assert stat.S_IMODE(config_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(env_path.stat().st_mode) == 0o600
    assert (
        keystore.fetch("care", f"mage.api_key.{SECRET_VERSION}")
        == SENSITIVE_SENTINEL
    )
    assert result.payload["settings"]["api_key_present"] is True

    with config_path.open("rb") as stream:
        parsed = tomllib.load(stream)
    assert parsed["mage"]["model"] == "example/new-model"


def test_saved_keychain_references_resolve_after_hermetic_restart(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "config.toml"
    env_path = tmp_path / ".env"
    keystore = FakeSecureKeystore()
    config = CareConfig(
        mage=MageConfig(
            api_key=SENSITIVE_SENTINEL,
            base_url="https://api.deepseek.com",
            model="example/restart-model",
        )
    )
    bridge = DesktopSettingsBridge(
        config_loader=lambda: config,
        config_path=config_path,
        env_path=env_path,
        keystore_factory=lambda: keystore,
        token_factory=lambda: "restart-token",
        secret_key_factory=lambda: SECRET_VERSION,
        environ={},
    )
    assert _confirm(bridge, {}).status == 200
    assert SENSITIVE_SENTINEL not in config_path.read_text(encoding="utf-8")
    assert SENSITIVE_SENTINEL not in env_path.read_text(encoding="utf-8")

    monkeypatch.setattr(
        "care.runtime.keystore.detect_keystore",
        lambda: keystore,
    )
    for key in tuple(os.environ):
        if key.startswith("CARE_"):
            monkeypatch.delenv(key, raising=False)
    applied = load_env_file(env_path)
    restarted = CareConfig.load(path=config_path, env=applied)

    assert restarted.mage.api_key == SENSITIVE_SENTINEL
    assert applied["CARE_MAGE__API_KEY"] == (
        f"keystore://care/mage.api_key.{SECRET_VERSION}"
    )


def test_plaintext_file_keystore_fallback_is_refused(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    env_path = tmp_path / ".env"
    bridge = DesktopSettingsBridge(
        config_loader=CareConfig,
        config_path=config_path,
        env_path=env_path,
        keystore_factory=lambda: FileKeystore(tmp_path / "secrets.json"),
        token_factory=lambda: "file-token",
        secret_key_factory=lambda: SECRET_VERSION,
        environ={},
    )

    result = _confirm(
        bridge,
        {
            "model": "example/model",
            "api_key_action": "replace",
            "api_key": REPLACEMENT_SENTINEL,
        },
    )

    assert result.status == 500
    assert result.payload["error"]["code"] == "settings_save_failed"
    assert "plaintext fallback was refused" in result.payload["error"]["message"]
    assert not config_path.exists()
    assert not env_path.exists()
    assert not (tmp_path / "secrets.json").exists()


def test_file_and_keychain_state_roll_back_when_second_write_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "config.toml"
    env_path = tmp_path / ".env"
    original_config = '[mage]\nmodel = "example/old-model"\n'
    config_path.write_text(original_config, encoding="utf-8")
    env_path.write_text("ORIGINAL_ENV=yes\n", encoding="utf-8")
    keystore = FakeSecureKeystore()
    keystore.store("care", "mage.api_key", SENSITIVE_SENTINEL)
    config = CareConfig(
        mage=MageConfig(
            api_key=SENSITIVE_SENTINEL,
            base_url="https://openrouter.ai/api/v1",
            model="example/old-model",
        )
    )
    bridge = DesktopSettingsBridge(
        config_loader=lambda: config,
        config_path=config_path,
        env_path=env_path,
        keystore_factory=lambda: keystore,
        token_factory=lambda: "rollback-token",
        secret_key_factory=lambda: SECRET_VERSION,
        environ={},
    )
    original_atomic_write = desktop_settings._atomic_write
    failed = False

    def flaky_write(path: Path, body: bytes, *, mode: int) -> None:
        nonlocal failed
        if path == env_path and not failed:
            failed = True
            raise OSError("synthetic env write failure")
        original_atomic_write(path, body, mode=mode)

    monkeypatch.setattr(desktop_settings, "_atomic_write", flaky_write)
    payload = {
        "api_key_action": "replace",
        "api_key": REPLACEMENT_SENTINEL,
    }

    result = _confirm(bridge, payload)

    assert result.status == 500
    assert result.payload["error"]["code"] == "settings_save_failed"
    assert config_path.read_text(encoding="utf-8") == original_config
    assert env_path.read_text(encoding="utf-8") == "ORIGINAL_ENV=yes\n"
    assert keystore.fetch("care", "mage.api_key") == SENSITIVE_SENTINEL
    assert keystore.fetch("care", f"mage.api_key.{SECRET_VERSION}") is None
    assert REPLACEMENT_SENTINEL not in config_path.read_text(encoding="utf-8")
    assert REPLACEMENT_SENTINEL not in env_path.read_text(encoding="utf-8")


def test_keychain_mutate_then_raise_is_included_in_rollback(tmp_path: Path) -> None:
    class MutateThenRaiseKeystore(FakeSecureKeystore):
        def store(self, service: str, key: str, value: str) -> None:
            super().store(service, key, value)
            raise RuntimeError("synthetic post-commit store failure")

    keystore = MutateThenRaiseKeystore()
    bridge = DesktopSettingsBridge(
        config_loader=CareConfig,
        config_path=tmp_path / "config.toml",
        env_path=tmp_path / ".env",
        cwd=tmp_path,
        keystore_factory=lambda: keystore,
        token_factory=lambda: "mutate-then-raise-token",
        secret_key_factory=lambda: SECRET_VERSION,
        environ={},
    )
    payload = {
        "api_key_action": "replace",
        "api_key": REPLACEMENT_SENTINEL,
    }

    result = _confirm(bridge, payload)

    assert result.status == 500
    assert result.payload["error"]["code"] == "settings_save_failed"
    assert keystore.values == {}
    assert not (tmp_path / "config.toml").exists()
    assert not (tmp_path / ".env").exists()


def test_keep_preserves_raw_reference_when_keychain_fetch_is_transiently_unavailable(
    tmp_path: Path,
) -> None:
    old_key = "mage.api_key.oldoldoldoldold1"
    old_reference = f"keystore://care/{old_key}"
    config_path = tmp_path / "config.toml"
    env_path = tmp_path / ".env"
    config_path.write_text(
        (
            "[mage]\n"
            f'api_key = "{old_reference}"\n'
            'base_url = "https://openrouter.ai/api/v1"\n'
            'model = "example/old-model"\n'
        ),
        encoding="utf-8",
    )
    env_path.write_text(f"CARE_MAGE__API_KEY={old_reference}\n", encoding="utf-8")

    class FetchFailsKeystore(FakeSecureKeystore):
        def __init__(self) -> None:
            super().__init__()
            self.calls: list[str] = []

        def fetch(self, service: str, key: str) -> str | None:
            self.calls.append("fetch")
            raise RuntimeError("transient keychain outage")

        def store(self, service: str, key: str, value: str) -> None:
            self.calls.append("store")
            super().store(service, key, value)

        def delete(self, service: str, key: str) -> None:
            self.calls.append("delete")
            super().delete(service, key)

    keystore = FetchFailsKeystore()
    keystore.values[("care", old_key)] = SENSITIVE_SENTINEL
    current = CareConfig(
        mage=MageConfig(
            api_key=None,
            base_url="https://openrouter.ai/api/v1",
            model="example/old-model",
        )
    )
    bridge = DesktopSettingsBridge(
        config_loader=lambda: current,
        config_path=config_path,
        env_path=env_path,
        cwd=tmp_path,
        keystore_factory=lambda: keystore,
        token_factory=lambda: "keep-token",
        secret_key_factory=lambda: SECRET_VERSION,
        environ={},
    )

    result = _confirm(bridge, {"model": "example/new-model"})

    assert result.status == 200
    assert old_reference in config_path.read_text(encoding="utf-8")
    assert old_reference in env_path.read_text(encoding="utf-8")
    assert keystore.values[("care", old_key)] == SENSITIVE_SENTINEL
    assert keystore.calls == []
    assert result.payload["settings"]["api_key_present"] is True


def test_replace_stages_immutable_secret_before_switch_and_purges_old_after_commit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    old_key = "mage.api_key.oldoldoldoldold2"
    old_reference = f"keystore://care/{old_key}"
    new_key = f"mage.api_key.{SECRET_VERSION}"
    config_path = tmp_path / "config.toml"
    env_path = tmp_path / ".env"
    config_path.write_text(
        (
            "[mage]\n"
            f'api_key = "{old_reference}"\n'
            'base_url = "https://openrouter.ai/api/v1"\n'
            'model = "example/model"\n'
        ),
        encoding="utf-8",
    )
    env_path.write_text(f"CARE_MAGE__API_KEY={old_reference}\n", encoding="utf-8")
    keystore = FakeSecureKeystore()
    keystore.values[("care", old_key)] = SENSITIVE_SENTINEL
    current = CareConfig(
        mage=MageConfig(
            api_key=SENSITIVE_SENTINEL,
            base_url="https://openrouter.ai/api/v1",
            model="example/model",
        )
    )
    bridge = DesktopSettingsBridge(
        config_loader=lambda: current,
        config_path=config_path,
        env_path=env_path,
        cwd=tmp_path,
        keystore_factory=lambda: keystore,
        token_factory=lambda: "replace-token",
        secret_key_factory=lambda: SECRET_VERSION,
        environ={},
    )
    original_atomic_write = desktop_settings._atomic_write
    observed_staging = False

    def inspect_before_switch(path: Path, body: bytes, *, mode: int) -> None:
        nonlocal observed_staging
        if not observed_staging:
            observed_staging = True
            assert keystore.values[("care", old_key)] == SENSITIVE_SENTINEL
            assert keystore.values[("care", new_key)] == REPLACEMENT_SENTINEL
            assert old_reference in config_path.read_text(encoding="utf-8")
        original_atomic_write(path, body, mode=mode)

    monkeypatch.setattr(desktop_settings, "_atomic_write", inspect_before_switch)
    result = _confirm(
        bridge,
        {"api_key_action": "replace", "api_key": REPLACEMENT_SENTINEL},
    )

    assert result.status == 200
    assert observed_staging is True
    assert ("care", old_key) not in keystore.values
    assert keystore.values[("care", new_key)] == REPLACEMENT_SENTINEL
    assert f"keystore://care/{new_key}" in config_path.read_text(encoding="utf-8")


def test_gc_purges_all_desktop_refs_removed_from_rewritten_surfaces(
    tmp_path: Path,
) -> None:
    shadowed_key = "mage.api_key.aaaaaaaaaaaaaaaa"
    effective_key = "mage.api_key.bbbbbbbbbbbbbbbb"
    shadowed_reference = f"keystore://care/{shadowed_key}"
    effective_reference = f"keystore://care/{effective_key}"
    config_path = tmp_path / "config.toml"
    env_path = tmp_path / ".env"
    config_path.write_text(
        "[mage]\n"
        f'api_key = "{shadowed_reference}"\n'
        'base_url = "https://openrouter.ai/api/v1"\n',
        encoding="utf-8",
    )
    env_path.write_text(
        f"CARE_MAGE__API_KEY={effective_reference}\n",
        encoding="utf-8",
    )
    keystore = FakeSecureKeystore()
    keystore.values[("care", shadowed_key)] = SENSITIVE_SENTINEL
    keystore.values[("care", effective_key)] = SENSITIVE_SENTINEL
    current = CareConfig(
        mage=MageConfig(
            api_key=SENSITIVE_SENTINEL,
            base_url="https://openrouter.ai/api/v1",
        )
    )
    bridge = DesktopSettingsBridge(
        config_loader=lambda: current,
        config_path=config_path,
        env_path=env_path,
        cwd=tmp_path,
        keystore_factory=lambda: keystore,
        token_factory=lambda: "all-refs-token",
        secret_key_factory=lambda: SECRET_VERSION,
        environ={},
    )

    result = _confirm(
        bridge,
        {"api_key_action": "replace", "api_key": REPLACEMENT_SENTINEL},
    )

    assert result.status == 200
    assert ("care", shadowed_key) not in keystore.values
    assert ("care", effective_key) not in keystore.values
    assert keystore.values[("care", f"mage.api_key.{SECRET_VERSION}")] == (
        REPLACEMENT_SENTINEL
    )


def test_clear_disconnects_then_purges_the_previous_keychain_reference(
    tmp_path: Path,
) -> None:
    old_key = "mage.api_key.oldoldoldoldold3"
    old_reference = f"keystore://care/{old_key}"
    config_path = tmp_path / "config.toml"
    env_path = tmp_path / ".env"
    config_path.write_text(
        "[mage]\n"
        f'api_key = "{old_reference}"\n'
        'base_url = "https://openrouter.ai/api/v1"\n',
        encoding="utf-8",
    )
    env_path.write_text(f"CARE_MAGE__API_KEY={old_reference}\n", encoding="utf-8")
    keystore = FakeSecureKeystore()
    keystore.values[("care", old_key)] = SENSITIVE_SENTINEL
    current = CareConfig(
        mage=MageConfig(
            api_key=SENSITIVE_SENTINEL,
            base_url="https://openrouter.ai/api/v1",
        )
    )
    bridge = DesktopSettingsBridge(
        config_loader=lambda: current,
        config_path=config_path,
        env_path=env_path,
        cwd=tmp_path,
        keystore_factory=lambda: keystore,
        token_factory=lambda: "clear-token",
        secret_key_factory=lambda: SECRET_VERSION,
        environ={},
    )

    result = _confirm(bridge, {"api_key_action": "clear"})

    assert result.status == 200
    assert result.payload["settings"]["api_key_present"] is False
    assert old_reference not in config_path.read_text(encoding="utf-8")
    assert old_reference not in env_path.read_text(encoding="utf-8")
    assert ("care", old_key) not in keystore.values


def test_clear_retains_unowned_legacy_keychain_reference(tmp_path: Path) -> None:
    legacy_key = "mage.api_key"
    legacy_reference = f"keystore://care/{legacy_key}"
    config_path = tmp_path / "config.toml"
    env_path = tmp_path / ".env"
    config_path.write_text(
        "[mage]\n"
        f'api_key = "{legacy_reference}"\n'
        'base_url = "https://openrouter.ai/api/v1"\n',
        encoding="utf-8",
    )
    env_path.write_text(
        f"CARE_MAGE__API_KEY={legacy_reference}\n",
        encoding="utf-8",
    )
    keystore = FakeSecureKeystore()
    keystore.values[("care", legacy_key)] = SENSITIVE_SENTINEL
    current = CareConfig(
        mage=MageConfig(
            api_key=SENSITIVE_SENTINEL,
            base_url="https://openrouter.ai/api/v1",
        )
    )
    bridge = DesktopSettingsBridge(
        config_loader=lambda: current,
        config_path=config_path,
        env_path=env_path,
        cwd=tmp_path,
        keystore_factory=lambda: keystore,
        token_factory=lambda: "legacy-clear-token",
        secret_key_factory=lambda: SECRET_VERSION,
        environ={},
    )

    result = _confirm(bridge, {"api_key_action": "clear"})

    assert result.status == 200
    assert keystore.values[("care", legacy_key)] == SENSITIVE_SENTINEL
    assert result.payload["warnings"] == [
        "Settings were saved; a legacy or shared Keychain credential was retained "
        "to avoid deleting another project's key."
    ]
    assert legacy_reference not in config_path.read_text(encoding="utf-8")
    assert legacy_reference not in env_path.read_text(encoding="utf-8")


def test_keychain_cleanup_failure_is_sanitized_and_surfaced_after_commit(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    old_key = "mage.api_key.oldoldoldoldold5"
    old_reference = f"keystore://care/{old_key}"
    config_path = tmp_path / "config.toml"
    env_path = tmp_path / ".env"
    config_path.write_text(
        "[mage]\n"
        f'api_key = "{old_reference}"\n'
        'base_url = "https://openrouter.ai/api/v1"\n',
        encoding="utf-8",
    )
    env_path.write_text(f"CARE_MAGE__API_KEY={old_reference}\n", encoding="utf-8")

    class DeleteFailsKeystore(FakeSecureKeystore):
        def delete(self, service: str, key: str) -> None:
            if key == old_key:
                raise RuntimeError(f"api_key={SENSITIVE_SENTINEL}")
            super().delete(service, key)

    keystore = DeleteFailsKeystore()
    keystore.values[("care", old_key)] = SENSITIVE_SENTINEL
    current = CareConfig(
        mage=MageConfig(
            api_key=SENSITIVE_SENTINEL,
            base_url="https://openrouter.ai/api/v1",
        )
    )
    bridge = DesktopSettingsBridge(
        config_loader=lambda: current,
        config_path=config_path,
        env_path=env_path,
        cwd=tmp_path,
        keystore_factory=lambda: keystore,
        token_factory=lambda: "warning-token",
        secret_key_factory=lambda: SECRET_VERSION,
        environ={},
    )

    result = _confirm(
        bridge,
        {"api_key_action": "replace", "api_key": REPLACEMENT_SENTINEL},
    )

    assert result.status == 200
    assert result.payload["warnings"] == [
        "Settings were saved, but an old Keychain credential could not be removed."
    ]
    assert SENSITIVE_SENTINEL not in json.dumps(result.payload)
    assert SENSITIVE_SENTINEL not in caplog.text
    assert f"keystore://care/mage.api_key.{SECRET_VERSION}" in (
        config_path.read_text(encoding="utf-8")
    )


def test_keychain_cleanup_prefers_noninteractive_delete_after_commit(
    tmp_path: Path,
) -> None:
    old_key = "mage.api_key.oldoldoldoldold6"
    old_reference = f"keystore://care/{old_key}"
    config_path = tmp_path / "config.toml"
    env_path = tmp_path / ".env"
    config_path.write_text(
        "[mage]\n"
        f'api_key = "{old_reference}"\n'
        'base_url = "https://openrouter.ai/api/v1"\n',
        encoding="utf-8",
    )
    env_path.write_text(f"CARE_MAGE__API_KEY={old_reference}\n", encoding="utf-8")

    class NoninteractiveCleanupKeystore(FakeSecureKeystore):
        def __init__(self) -> None:
            super().__init__()
            self.noninteractive_deletes: list[tuple[str, str]] = []

        def delete(self, service: str, key: str) -> None:
            raise AssertionError("cleanup must not use an interactive delete")

        def delete_noninteractive(self, service: str, key: str) -> None:
            self.noninteractive_deletes.append((service, key))
            self.values.pop((service, key), None)

    keystore = NoninteractiveCleanupKeystore()
    keystore.values[("care", old_key)] = SENSITIVE_SENTINEL
    current = CareConfig(
        mage=MageConfig(
            api_key=SENSITIVE_SENTINEL,
            base_url="https://openrouter.ai/api/v1",
        )
    )
    bridge = DesktopSettingsBridge(
        config_loader=lambda: current,
        config_path=config_path,
        env_path=env_path,
        cwd=tmp_path,
        keystore_factory=lambda: keystore,
        token_factory=lambda: "noninteractive-cleanup-token",
        secret_key_factory=lambda: SECRET_VERSION,
        environ={},
    )

    result = _confirm(
        bridge,
        {"api_key_action": "replace", "api_key": REPLACEMENT_SENTINEL},
    )

    assert result.status == 200
    assert result.payload["warnings"] == []
    assert keystore.noninteractive_deletes == [("care", old_key)]


def test_project_literal_secret_fails_closed_with_migration_instruction(
    tmp_path: Path,
) -> None:
    project_path = tmp_path / "care.toml"
    project_path.write_text(
        "[mage]\n"
        f'api_key = "{SENSITIVE_SENTINEL}"\n'
        'base_url = "https://openrouter.ai/api/v1"\n',
        encoding="utf-8",
    )
    config_path = tmp_path / "global.toml"
    env_path = tmp_path / ".env"
    keystore = FakeSecureKeystore()
    current = CareConfig(
        mage=MageConfig(
            api_key=SENSITIVE_SENTINEL,
            base_url="https://openrouter.ai/api/v1",
        )
    )
    bridge = DesktopSettingsBridge(
        config_loader=lambda: current,
        config_path=config_path,
        env_path=env_path,
        cwd=tmp_path,
        keystore_factory=lambda: keystore,
        token_factory=lambda: "project-token",
        secret_key_factory=lambda: SECRET_VERSION,
        environ={},
    )

    result = bridge.update({"model": "example/model"})

    assert result.status == 409
    assert result.payload["error"]["code"] == "settings_source_conflict"
    assert "migrate-secrets" in result.payload["error"]["message"]
    assert not config_path.exists()
    assert not env_path.exists()
    assert keystore.values == {}
    assert project_path.read_text(encoding="utf-8").count(SENSITIVE_SENTINEL) == 1


def test_project_owned_native_setting_fails_closed_without_flattening(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "global.toml"
    original_global = '[mage]\nmodel = "global/model"\n'
    config_path.write_text(original_global, encoding="utf-8")
    project_path = tmp_path / "care.toml"
    project_path.write_text('[mage]\nmodel = "project/model"\n', encoding="utf-8")
    bridge = DesktopSettingsBridge(
        config_loader=lambda: CareConfig(
            mage=MageConfig(model="project/model"),
        ),
        config_path=config_path,
        env_path=tmp_path / ".env",
        cwd=tmp_path,
        keystore_factory=FakeSecureKeystore,
        environ={},
    )

    result = bridge.update({"model": "new/model"})

    assert result.status == 409
    assert result.payload["error"]["code"] == "settings_source_conflict"
    assert "Project care.toml owns" in result.payload["error"]["message"]
    assert config_path.read_text(encoding="utf-8") == original_global
    assert project_path.read_text(encoding="utf-8") == (
        '[mage]\nmodel = "project/model"\n'
    )
    assert not (tmp_path / ".env").exists()


def test_external_managed_environment_override_fails_closed(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    original_global = '[mage]\nmodel = "global/model"\n'
    config_path.write_text(original_global, encoding="utf-8")
    env_path = tmp_path / ".env"
    original_env = "CARE_MAGE__MODEL=dotenv/model\n"
    env_path.write_text(original_env, encoding="utf-8")
    bridge = DesktopSettingsBridge(
        config_loader=lambda: CareConfig(
            mage=MageConfig(model="external/model"),
        ),
        config_path=config_path,
        env_path=env_path,
        cwd=tmp_path,
        keystore_factory=FakeSecureKeystore,
        environ={"CARE_MAGE__MODEL": "external/model"},
    )

    result = bridge.update({"model": "new/model"})

    assert result.status == 409
    assert result.payload["error"]["code"] == "settings_source_conflict"
    assert "CARE_MAGE__MODEL" in result.payload["error"]["message"]
    assert "external/model" not in json.dumps(result.payload)
    assert config_path.read_text(encoding="utf-8") == original_global
    assert env_path.read_text(encoding="utf-8") == original_env


def test_unmanaged_project_values_are_not_flattened_into_global_config(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "global.toml"
    config_path.write_text('[mage]\nmodel = "global/model"\n', encoding="utf-8")
    project_path = tmp_path / "care.toml"
    project_path.write_text("[sandbox]\ncpu_limit = 9.0\n", encoding="utf-8")
    current = CareConfig(mage=MageConfig(model="global/model"))
    current.sandbox.cpu_limit = 9.0
    bridge = DesktopSettingsBridge(
        config_loader=lambda: current,
        config_path=config_path,
        env_path=tmp_path / ".env",
        cwd=tmp_path,
        keystore_factory=FakeSecureKeystore,
        token_factory=lambda: "unmanaged-project-token",
        secret_key_factory=lambda: SECRET_VERSION,
        environ={},
    )

    result = _confirm(bridge, {"model": "new/model"})

    assert result.status == 200
    with config_path.open("rb") as stream:
        global_config = tomllib.load(stream)
    assert global_config["mage"]["model"] == "new/model"
    assert global_config["sandbox"]["cpu_limit"] == 2.0
    assert project_path.read_text(encoding="utf-8") == (
        "[sandbox]\ncpu_limit = 9.0\n"
    )


def test_provider_change_with_existing_key_requires_replace_or_clear(
    tmp_path: Path,
) -> None:
    current = CareConfig(
        mage=MageConfig(
            api_key=SENSITIVE_SENTINEL,
            base_url="https://openrouter.ai/api/v1",
            model="example/model",
        )
    )
    bridge = _bridge(tmp_path, current, FakeSecureKeystore())

    result = bridge.update(
        {
            "provider": "deepseek",
            "base_url": "https://api.deepseek.com",
        }
    )

    assert result.status == 400
    assert result.payload["error"]["code"] == "invalid_settings"
    assert SENSITIVE_SENTINEL not in json.dumps(result.payload)
    assert not (tmp_path / "config.toml").exists()
    assert not (tmp_path / ".env").exists()


@pytest.mark.parametrize("service", ["memory", "platform"])
def test_service_key_keep_blocks_origin_change(
    service: str,
    tmp_path: Path,
) -> None:
    current = CareConfig()
    getattr(current, service).api_key = SENSITIVE_SENTINEL
    bridge = _bridge(tmp_path, current, FakeSecureKeystore())

    result = bridge.update(
        {f"{service}_base_url": f"https://{service}.example.test"}
    )

    assert result.status == 400
    assert result.payload["error"]["code"] == "invalid_settings"
    assert SENSITIVE_SENTINEL not in json.dumps(result.payload)


@pytest.mark.parametrize("service", ["memory", "platform"])
@pytest.mark.parametrize("action", ["replace", "clear"])
def test_service_origin_change_requires_explicit_key_rebind(
    service: str,
    action: str,
    tmp_path: Path,
) -> None:
    current = CareConfig()
    getattr(current, service).api_key = SENSITIVE_SENTINEL
    payload = {
        f"{service}_base_url": f"https://{service}.example.test",
        f"{service}_api_key_action": action,
    }
    if action == "replace":
        payload[f"{service}_api_key"] = REPLACEMENT_SENTINEL

    result = _bridge(tmp_path, current, FakeSecureKeystore()).update(payload)

    assert result.status == 409
    assert result.payload["error"]["code"] == "confirmation_required"
    assert SENSITIVE_SENTINEL not in json.dumps(result.payload)
    assert REPLACEMENT_SENTINEL not in json.dumps(result.payload)


@pytest.mark.parametrize(
    "field",
    [
        "memory_base_url",
        "platform_base_url",
        "mutation_base_url",
        "validation_base_url",
    ],
)
def test_remote_http_service_endpoints_are_rejected(field: str, tmp_path: Path) -> None:
    result = _bridge(tmp_path, CareConfig(), FakeSecureKeystore()).update(
        {field: "http://remote.example.test/v1"}
    )
    assert result.status == 400
    assert result.payload["error"]["code"] == "invalid_settings"


def test_process_environment_publication_is_fail_closed_between_phases() -> None:
    old_reference = "keystore://care/mage.api_key.oldoldoldoldold4"
    new_reference = f"keystore://care/mage.api_key.{SECRET_VERSION}"
    environ = {
        "CARE_MAGE__BASE_URL": "https://openrouter.ai/api/v1",
        "CARE_MAGE__API_KEY": old_reference,
    }
    snapshots: dict[str, dict[str, str]] = {}

    desktop_settings._publish_env_updates(
        {
            "CARE_MAGE__BASE_URL": "https://api.deepseek.com",
            "CARE_MAGE__API_KEY": new_reference,
        },
        environ=environ,
        phase_hook=lambda phase, values: snapshots.update({phase: dict(values)}),
    )

    assert snapshots["secrets_cleared"]["CARE_MAGE__API_KEY"] == ""
    assert snapshots["nonsecrets_published"]["CARE_MAGE__BASE_URL"] == (
        "https://api.deepseek.com"
    )
    assert snapshots["nonsecrets_published"]["CARE_MAGE__API_KEY"] == ""
    assert snapshots["secrets_published"]["CARE_MAGE__API_KEY"] == new_reference


def test_environment_refresh_failure_after_commit_returns_success_with_warning(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    exception_secret = "sk-synthetic-environment-refresh-secret"

    class PublishFailsEnvironment(dict[str, str]):
        def __setitem__(self, key: str, value: str) -> None:
            if key == "CARE_MAGE__MODEL":
                raise RuntimeError(f"api_key={exception_secret}")
            super().__setitem__(key, value)

    config_path = tmp_path / "config.toml"
    env_path = tmp_path / ".env"
    bridge = DesktopSettingsBridge(
        config_loader=CareConfig,
        config_path=config_path,
        env_path=env_path,
        cwd=tmp_path,
        keystore_factory=FakeSecureKeystore,
        token_factory=lambda: "environment-refresh-token",
        environ=PublishFailsEnvironment(),
    )

    result = _confirm(bridge, {"model": "example/committed-model"})

    assert result.status == 200
    assert result.payload["ok"] is True
    assert result.payload["settings"]["model"] == "example/committed-model"
    assert any(
        "running app environment could not be refreshed" in warning
        for warning in result.payload["warnings"]
    )
    assert "example/committed-model" in config_path.read_text(encoding="utf-8")
    assert "example/committed-model" in env_path.read_text(encoding="utf-8")
    assert exception_secret not in json.dumps(result.payload)
    assert exception_secret not in caplog.text


def test_snapshot_refresh_failure_after_commit_returns_success_with_warning(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    exception_secret = "sk-synthetic-snapshot-refresh-secret"
    config_path = tmp_path / "config.toml"
    env_path = tmp_path / ".env"
    bridge = DesktopSettingsBridge(
        config_loader=CareConfig,
        config_path=config_path,
        env_path=env_path,
        cwd=tmp_path,
        keystore_factory=FakeSecureKeystore,
        token_factory=lambda: "snapshot-refresh-token",
        environ={},
        desktop_auth_path=tmp_path / "desktop-auth.toml",
    )
    original_load_desktop_auth = bridge._load_desktop_auth
    calls = 0

    def fail_only_after_commit() -> Any:
        nonlocal calls
        calls += 1
        if calls == 3:
            raise OSError(f"api_key={exception_secret}")
        return original_load_desktop_auth()

    monkeypatch.setattr(bridge, "_load_desktop_auth", fail_only_after_commit)

    result = _confirm(bridge, {"model": "example/snapshot-committed-model"})

    assert calls == 3
    assert result.status == 200
    assert result.payload["ok"] is True
    assert result.payload["settings"]["model"] == (
        "example/snapshot-committed-model"
    )
    assert any(
        "refreshed settings view could not be loaded" in warning
        for warning in result.payload["warnings"]
    )
    assert "example/snapshot-committed-model" in config_path.read_text(
        encoding="utf-8"
    )
    assert "example/snapshot-committed-model" in env_path.read_text(
        encoding="utf-8"
    )
    assert exception_secret not in json.dumps(result.payload)
    assert exception_secret not in caplog.text


def test_loader_failures_never_log_or_return_exception_secrets(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    loader_secret = "sk-synthetic-loader-secret"

    def failing_loader() -> CareConfig:
        raise RuntimeError(f"api_key={loader_secret}")

    bridge = DesktopSettingsBridge(
        config_loader=failing_loader,
        config_path=tmp_path / "config.toml",
        env_path=tmp_path / ".env",
        environ={},
    )

    with pytest.raises(desktop_settings.SettingsLoadError):
        bridge.snapshot()
    result = bridge.update({"model": "example/model"})

    assert result.status == 500
    assert loader_secret not in caplog.text
    assert loader_secret not in json.dumps(result.payload)


def test_keychain_fetch_failure_never_logs_backend_exception_secret(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    backend_secret = "sk-synthetic-fetch-exception-secret"
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        "[mage]\n"
        'api_key = "keystore://care/mage.api_key.aaaaaaaaaaaaaaaa"\n',
        encoding="utf-8",
    )

    class FetchFailsKeystore(FakeSecureKeystore):
        def fetch(self, service: str, key: str) -> str | None:
            raise RuntimeError(f"api_key={backend_secret}")

    monkeypatch.setattr(
        "care.runtime.keystore.detect_keystore",
        lambda: FetchFailsKeystore(),
    )
    bridge = DesktopSettingsBridge(
        config_loader=lambda: CareConfig.load(path=config_path, env={}),
        config_path=config_path,
        env_path=tmp_path / ".env",
        cwd=tmp_path,
        environ={},
    )

    snapshot = bridge.snapshot()

    assert snapshot["settings"]["api_key_present"] is True
    assert backend_secret not in json.dumps(snapshot)
    assert backend_secret not in caplog.text
    assert "RuntimeError" in caplog.text


def test_native_frontend_hydrates_saved_settings_before_the_first_run() -> None:
    source = (
        Path(__file__).parents[1] / "care" / "web_assets" / "app.js"
    ).read_text(encoding="utf-8")
    html = (
        Path(__file__).parents[1] / "care" / "web_assets" / "index.html"
    ).read_text(encoding="utf-8")

    assert "state.settingsHydration = hydrateNativeSettings();" in source
    assert "await state.settingsHydration;" in source
    assert '{ invocation: "/settings"' in source
    assert "await openSettingsDialog();" in source
    assert "radio.checked = true;" in source
    assert 'settings.interactive_run === "auto"' in source
    assert "document.documentElement.dataset.reducedMotion" in source
    assert 'present ? "Credential configured"' in source
    for field in ("memory", "platform", "upload"):
        assert f'addSecretUpdate(payload, "{field}_api_key"' in source
        assert f'id="settings-{field}-key"' in html
        assert f'id="settings-{field}-key-state"' in html
        assert f'id="settings-{field}-key-clear"' in html
    assert "Default session mode" in html
    assert "The native desktop interface is currently English" in html
