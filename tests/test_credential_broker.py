from __future__ import annotations

from dataclasses import replace
import io
import json
import os
from pathlib import Path
import socket
import subprocess
import threading
from typing import Any

import pytest

from care.config import CareConfig
import care.harness_mcp_runtime as mcp_runtime
from care.harness_mcp_runtime import (
    ARTIFACT_ROOT_ENV,
    MEMORY_ENABLED_ENV,
    SESSION_ID_ENV,
    WORKSPACE_ROOT_ENV,
    HarnessMcpRuntimeContextError,
    build_harness_mcp_runtime_context,
)
from care.runtime.credential_broker import (
    BROKER_BOOTSTRAP_FD_ENV,
    CARE_CREDENTIAL_SLOTS,
    HarnessCredentialBrokerBootstrap,
    HarnessCredentialBrokerError,
    HarnessCredentialBrokerServer,
    HarnessCredentialResolver,
    apply_harness_credentials,
    encode_harness_credential_bootstrap,
    fetch_harness_credential,
    peek_harness_credential_bootstrap,
)
import care.runtime.deepseek_host as deepseek_host
from care.runtime.deepseek_host import (
    PINNED_DSH_VERSION,
    DeepSeekHarnessCommand,
    DeepSeekHarnessHost,
    DeepSeekHarnessVersionStatus,
)
from care.runtime.keystore import (
    KeystoreInteractionRequiredError,
    MemoryKeystore,
)


_SHORT_SOCKET_ROOT = (
    Path("/private/tmp") if Path("/private/tmp").is_dir() else Path("/tmp")
)


def _set_all_credential_slots(config: CareConfig, value: str) -> None:
    for slot in CARE_CREDENTIAL_SLOTS:
        parent_name, field_name = slot.split(".", maxsplit=1)
        setattr(getattr(config, parent_name), field_name, value)


class _CountingKeystore(MemoryKeystore):
    def __init__(self) -> None:
        super().__init__()
        self.fetch_calls: list[tuple[str, str]] = []

    def fetch(self, service: str, key: str) -> str | None:
        self.fetch_calls.append((service, key))
        return super().fetch(service, key)


def test_resolver_caches_literal_and_keystore_values() -> None:
    config = CareConfig()
    config.mage.api_key = "literal-mage-secret"
    config.memory.api_key = "keystore://care/memory-api-key"
    keystore = _CountingKeystore()
    keystore.store("care", "memory-api-key", "native-memory-secret")
    factory_calls = 0

    def make_keystore() -> _CountingKeystore:
        nonlocal factory_calls
        factory_calls += 1
        return keystore

    resolver = HarnessCredentialResolver(
        config,
        keystore_factory=make_keystore,
    )

    assert resolver("mage.api_key") == "literal-mage-secret"
    assert resolver("mage.api_key") == "literal-mage-secret"
    assert factory_calls == 0
    assert resolver("memory.api_key") == "native-memory-secret"
    assert resolver("memory.api_key") == "native-memory-secret"
    assert factory_calls == 1
    assert keystore.fetch_calls == [("care", "memory-api-key")]


class _InteractionBlockedKeystore(MemoryKeystore):
    def __init__(self) -> None:
        super().__init__()
        self.fetch_calls: list[tuple[str, str]] = []

    def fetch(self, service: str, key: str) -> str | None:
        self.fetch_calls.append((service, key))
        raise KeystoreInteractionRequiredError("read", -25308)


def test_resolver_circuit_breaks_keychain_prompts_but_keeps_literals() -> None:
    config = CareConfig()
    config.mage.api_key = "keystore://care/mage-api-key"
    config.mage.web_search_api_key = "keystore://care/search-api-key"
    config.memory.api_key = "literal-memory-secret"
    keystore = _InteractionBlockedKeystore()
    factory_calls = 0

    def make_keystore() -> _InteractionBlockedKeystore:
        nonlocal factory_calls
        factory_calls += 1
        return keystore

    resolver = HarnessCredentialResolver(
        config,
        keystore_factory=make_keystore,
    )

    assert resolver("mage.api_key") is None
    assert resolver("mage.web_search_api_key") is None
    assert resolver("memory.api_key") == "literal-memory-secret"
    assert resolver("mage.api_key") is None
    assert factory_calls == 1
    assert keystore.fetch_calls == [("care", "mage-api-key")]


def test_apply_harness_credentials_clears_every_missing_slot() -> None:
    config = CareConfig()
    _set_all_credential_slots(config, "keystore://care/unresolved")

    apply_harness_credentials(
        config,
        {"memory.api_key": "hydrated-memory-secret"},
    )

    for slot in CARE_CREDENTIAL_SLOTS:
        parent_name, field_name = slot.split(".", maxsplit=1)
        value = getattr(getattr(config, parent_name), field_name)
        if slot == "memory.api_key":
            assert value == "hydrated-memory-secret"
        else:
            assert value is None


def test_credential_broker_roundtrip_and_cleanup() -> None:
    calls: list[str] = []

    def provider(slot: str) -> str | None:
        calls.append(slot)
        if slot == "memory.api_key":
            return "broker-memory-secret"
        return None

    server = HarnessCredentialBrokerServer(
        session_id="session-01",
        provider=provider,
        socket_root=_SHORT_SOCKET_ROOT,
    )
    server.start()
    socket_path = server.socket_path
    try:
        assert fetch_harness_credential(
            server.bootstrap,
            slot="memory.api_key",
        ) == "broker-memory-secret"
        assert fetch_harness_credential(
            server.bootstrap,
            slot="mage.api_key",
        ) is None
        assert calls == ["memory.api_key", "mage.api_key"]
    finally:
        server.close()

    assert not socket_path.exists()
    assert not socket_path.parent.exists()


def test_broker_defers_provider_cleanup_until_inflight_request_quiesces() -> None:
    entered = threading.Event()
    release = threading.Event()
    closed = threading.Event()

    class _BlockingProvider:
        def __call__(self, _slot: str) -> None:
            entered.set()
            release.wait(timeout=3.0)
            return None

        def close(self) -> None:
            closed.set()

    server = HarnessCredentialBrokerServer(
        session_id="session-01",
        provider=_BlockingProvider(),
        socket_root=_SHORT_SOCKET_ROOT,
    )
    server.start()

    def fetch() -> None:
        try:
            fetch_harness_credential(server.bootstrap, slot="memory.api_key")
        except HarnessCredentialBrokerError:
            pass

    client = threading.Thread(target=fetch)
    client.start()
    assert entered.wait(timeout=1.0)

    server.close()
    assert not closed.is_set()
    release.set()

    client.join(timeout=2.0)
    assert not client.is_alive()
    assert closed.wait(timeout=2.0)


@pytest.mark.parametrize("invalid_part", ["token", "session"])
def test_credential_broker_rejects_wrong_capability(
    invalid_part: str,
) -> None:
    provider_calls: list[str] = []

    def provider(slot: str) -> str:
        provider_calls.append(slot)
        return "must-not-be-returned"

    server = HarnessCredentialBrokerServer(
        session_id="session-01",
        provider=provider,
        socket_root=_SHORT_SOCKET_ROOT,
    )
    server.start()
    try:
        bootstrap = server.bootstrap
        if invalid_part == "token":
            bootstrap = replace(bootstrap, token="x" * len(bootstrap.token))
        else:
            bootstrap = replace(bootstrap, session_id="session-02")

        with pytest.raises(HarnessCredentialBrokerError):
            fetch_harness_credential(bootstrap, slot="memory.api_key")
        assert provider_calls == []
    finally:
        server.close()


def test_bootstrap_socketpair_peek_is_repeatable_and_repr_is_redacted(
    tmp_path: Path,
) -> None:
    bootstrap = HarnessCredentialBrokerBootstrap(
        socket_path=(tmp_path / "private.sock").resolve(),
        session_id="session-01",
        token="sensitive-bootstrap-token-1234567890",
    )
    parent, child = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    frame = encode_harness_credential_bootstrap(bootstrap)
    try:
        parent.sendall(frame)
        assert peek_harness_credential_bootstrap(child.fileno()) == bootstrap
        assert peek_harness_credential_bootstrap(child.fileno()) == bootstrap
        assert bootstrap.token not in repr(bootstrap)
        assert "redacted" in repr(bootstrap)
    finally:
        for index in range(len(frame)):
            frame[index] = 0
        parent.close()
        child.close()


def test_bootstrap_peek_ignores_plain_pipe() -> None:
    read_fd, write_fd = os.pipe()
    try:
        assert peek_harness_credential_bootstrap(read_fd) is None
    finally:
        os.close(read_fd)
        os.close(write_fd)


def _runtime_env(tmp_path: Path, *, session_id: str = "session-01") -> dict[str, str]:
    return {
        SESSION_ID_ENV: session_id,
        ARTIFACT_ROOT_ENV: str(tmp_path / "artifacts"),
        WORKSPACE_ROOT_ENV: str(tmp_path),
        MEMORY_ENABLED_ENV: "true",
    }


def test_runtime_rejects_credential_broker_from_another_session(
    tmp_path: Path,
) -> None:
    bootstrap = HarnessCredentialBrokerBootstrap(
        socket_path=(tmp_path / "unused.sock").resolve(),
        session_id="other-session",
        token="sensitive-bootstrap-token-1234567890",
    )

    with pytest.raises(HarnessMcpRuntimeContextError) as raised:
        build_harness_mcp_runtime_context(
            _runtime_env(tmp_path),
            credential_broker=bootstrap,
        )

    assert raised.value.code == "credential_broker_session_mismatch"
    assert bootstrap.token not in str(raised.value)


class _RuntimeMemory:
    def get_chain(self, entity_id: str, *, channel: str) -> dict[str, object]:
        return {"id": entity_id, "channel": channel, "steps": []}


def test_runtime_loads_raw_config_and_hydrates_it_from_broker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw_config = CareConfig()
    _set_all_credential_slots(raw_config, "keystore://care/unresolved")
    load_calls: list[dict[str, Any]] = []
    fetch_calls: list[str] = []

    def fake_load(_cls: type[CareConfig], **kwargs: Any) -> CareConfig:
        load_calls.append(kwargs)
        return raw_config

    def fake_fetch(
        _bootstrap: HarnessCredentialBrokerBootstrap,
        *,
        slot: str,
    ) -> str | None:
        fetch_calls.append(slot)
        if slot == "memory.api_key":
            return "broker-memory-secret"
        return None

    monkeypatch.setattr(mcp_runtime.CareConfig, "load", classmethod(fake_load))
    monkeypatch.setattr(mcp_runtime, "fetch_harness_credential", fake_fetch)
    monkeypatch.setattr(
        mcp_runtime.CareMemory,
        "from_config",
        classmethod(lambda _cls, _config: _RuntimeMemory()),
    )
    bootstrap = HarnessCredentialBrokerBootstrap(
        socket_path=(tmp_path / "unused.sock").resolve(),
        session_id="session-01",
        token="sensitive-bootstrap-token-1234567890",
    )

    context = build_harness_mcp_runtime_context(
        _runtime_env(tmp_path),
        credential_broker=bootstrap,
    )

    assert load_calls == [
        {
            "path": None,
            "env": _runtime_env(tmp_path),
            "cwd": tmp_path,
            "resolve_secrets": False,
        }
    ]
    assert fetch_calls == list(CARE_CREDENTIAL_SLOTS)
    assert context.config is raw_config
    assert context.config.memory.api_key == "broker-memory-secret"
    for slot in CARE_CREDENTIAL_SLOTS:
        if slot == "memory.api_key":
            continue
        parent_name, field_name = slot.split(".", maxsplit=1)
        assert getattr(getattr(context.config, parent_name), field_name) is None


class _HostProcess:
    def __init__(self) -> None:
        self.stdout = io.StringIO("dsh web: http://127.0.0.1:4567\n")
        self.code: int | None = None

    def poll(self) -> int | None:
        return self.code

    def terminate(self) -> None:
        self.code = 0

    def kill(self) -> None:
        self.code = -9

    def wait(self, timeout: float | None = None) -> int:
        if self.code is None:
            raise subprocess.TimeoutExpired("dsh", timeout)
        return self.code


class _HostProvider:
    def __init__(self) -> None:
        self.close_calls = 0

    def __call__(self, _slot: str) -> str | None:
        return "provider-secret-must-not-leak"

    def close(self) -> None:
        self.close_calls += 1


def test_host_provider_uses_only_inherited_fd_and_cleans_broker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    servers: list[HarnessCredentialBrokerServer] = []

    class _LocalBroker(HarnessCredentialBrokerServer):
        def __init__(self, *, session_id: str, provider: Any) -> None:
            super().__init__(
                session_id=session_id,
                provider=provider,
                socket_root=_SHORT_SOCKET_ROOT,
            )
            servers.append(self)

    monkeypatch.setattr(
        deepseek_host,
        "HarnessCredentialBrokerServer",
        _LocalBroker,
    )
    calls: list[tuple[list[str], dict[str, Any]]] = []

    def popen(argv: list[str], **kwargs: Any) -> _HostProcess:
        calls.append((argv, kwargs))
        return _HostProcess()

    provider = _HostProvider()
    care_python = tmp_path / "python"
    care_python.write_bytes(b"python")
    host = DeepSeekHarnessHost(
        command=DeepSeekHarnessCommand(
            ("/opt/bin/dsh",),
            "installed",
            observed_version=PINNED_DSH_VERSION,
        ),
        workspace=tmp_path,
        dsh_home=tmp_path / "dsh-home",
        care_python=care_python,
        session_id="session-01",
        credential_provider=provider,
        environ={
            "SAFE_SETTING": "retained",
            "OPENAI_API_KEY": "ambient-openai-secret",
            "AUTHORIZATION": "ambient-authorization-secret",
            "DATABASE_PASSWORD": "ambient-database-secret",
            "CARE_MEMORY__API_KEY": "ambient-care-secret",
        },
        version_probe=lambda command: DeepSeekHarnessVersionStatus(
            expected=PINNED_DSH_VERSION,
            observed=PINNED_DSH_VERSION,
            compatible=True,
            provenance=command.provenance,
        ),
        popen=popen,
    )

    host.start()
    assert len(calls) == 1
    assert len(servers) == 1
    socket_path = servers[0].socket_path
    bootstrap_token = servers[0].bootstrap.token
    argv, kwargs = calls[0]
    spawned_env = dict(kwargs["env"])
    inherited_fds = kwargs["pass_fds"]

    assert socket_path.exists()
    assert spawned_env["SAFE_SETTING"] == "retained"
    assert "OPENAI_API_KEY" not in spawned_env
    assert "AUTHORIZATION" not in spawned_env
    assert "DATABASE_PASSWORD" not in spawned_env
    assert "CARE_MEMORY__API_KEY" not in spawned_env
    assert spawned_env[BROKER_BOOTSTRAP_FD_ENV].isdigit()
    assert inherited_fds == (int(spawned_env[BROKER_BOOTSTRAP_FD_ENV]),)
    launch_boundary = json.dumps(
        {"argv": argv, "env": spawned_env},
        sort_keys=True,
    )
    assert bootstrap_token not in launch_boundary
    assert str(socket_path) not in launch_boundary
    assert "provider-secret-must-not-leak" not in launch_boundary

    host.stop()
    host.stop()

    assert not socket_path.exists()
    assert not socket_path.parent.exists()
    assert provider.close_calls == 1
