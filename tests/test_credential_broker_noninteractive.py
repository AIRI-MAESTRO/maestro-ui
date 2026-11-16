"""Hermetic tests for background credential reads (never the real Keychain)."""

from __future__ import annotations

from care.config import CareConfig
from care.runtime.credential_broker import HarnessCredentialResolver
from care.runtime.keystore import (
    KeystoreAccessTimeoutError,
    KeystoreInteractionRequiredError,
    MemoryKeystore,
    make_keystore_url,
)


class _NonInteractiveKeystore(MemoryKeystore):
    def __init__(self, *, interaction_required: bool = False) -> None:
        super().__init__()
        self.interaction_required = interaction_required
        self.interactive_calls: list[tuple[str, str]] = []
        self.noninteractive_calls: list[tuple[str, str]] = []

    def fetch(self, service: str, key: str) -> str | None:
        self.interactive_calls.append((service, key))
        raise AssertionError("background resolver attempted an interactive fetch")

    def fetch_noninteractive(self, service: str, key: str) -> str | None:
        self.noninteractive_calls.append((service, key))
        if self.interaction_required:
            raise KeystoreInteractionRequiredError(
                "non-interactive fetch",
                -25308,
            )
        return self._store.get((service, key))


def test_resolver_uses_noninteractive_read_and_caches_success() -> None:
    config = CareConfig()
    config.mage.api_key = make_keystore_url("care", "mage.api_key")
    keystore = _NonInteractiveKeystore()
    keystore.store("care", "mage.api_key", "sk-background")
    resolver = HarnessCredentialResolver(
        config,
        keystore_factory=lambda: keystore,
    )

    assert resolver("mage.api_key") == "sk-background"
    assert resolver("mage.api_key") == "sk-background"
    assert keystore.noninteractive_calls == [("care", "mage.api_key")]
    assert keystore.interactive_calls == []


def test_resolver_auth_failure_never_prompts_and_breaks_keychain_reads() -> None:
    config = CareConfig()
    config.mage.api_key = make_keystore_url("care", "mage.api_key")
    config.memory.api_key = make_keystore_url("care", "memory.api_key")
    config.platform.api_key = "literal-still-available"
    keystore = _NonInteractiveKeystore(interaction_required=True)
    resolver = HarnessCredentialResolver(
        config,
        keystore_factory=lambda: keystore,
    )

    assert resolver("mage.api_key") is None
    assert resolver("memory.api_key") is None
    assert resolver("platform.api_key") == "literal-still-available"
    assert keystore.noninteractive_calls == [("care", "mage.api_key")]
    assert keystore.interactive_calls == []


def test_resolver_timeout_circuit_breaks_remaining_keychain_reads() -> None:
    config = CareConfig()
    config.mage.api_key = make_keystore_url("care", "mage.api_key")
    config.memory.api_key = make_keystore_url("care", "memory.api_key")

    class _TimedOutKeystore(_NonInteractiveKeystore):
        def fetch_noninteractive(self, service: str, key: str) -> str | None:
            self.noninteractive_calls.append((service, key))
            raise KeystoreAccessTimeoutError("non-interactive fetch", 2.0)

    keystore = _TimedOutKeystore()
    resolver = HarnessCredentialResolver(
        config,
        keystore_factory=lambda: keystore,
    )

    assert resolver("mage.api_key") is None
    assert resolver("memory.api_key") is None
    assert keystore.noninteractive_calls == [("care", "mage.api_key")]
    assert keystore.interactive_calls == []
