"""Focused tests for batched config credential resolution."""

from __future__ import annotations

import care.config as config_module
import care.runtime.keystore as keystore_module
from care.config import CareConfig
from care.runtime.keystore import (
    KeystoreError,
    KeystoreInteractionRequiredError,
)


class _ScriptedKeystore:
    def __init__(self, outcomes: dict[tuple[str, str], object]) -> None:
        self.outcomes = outcomes
        self.calls: list[tuple[str, str]] = []

    def fetch(self, service: str, key: str) -> str | None:
        self.calls.append((service, key))
        outcome = self.outcomes[(service, key)]
        if isinstance(outcome, Exception):
            raise outcome
        assert outcome is None or isinstance(outcome, str)
        return outcome


def test_interaction_failure_stops_remaining_keychain_reads(monkeypatch) -> None:
    backend = _ScriptedKeystore(
        {
            ("care", "mage.api_key"): KeystoreInteractionRequiredError(
                "fetch",
                -25308,
            ),
        }
    )
    monkeypatch.setattr(keystore_module, "detect_keystore", lambda: backend)
    config = CareConfig.model_validate(
        {
            "mage": {
                "api_key": "keystore://care/mage.api_key",
                "web_search_api_key": "keystore://care/mage.web_search_api_key",
            },
            "memory": {"api_key": "keystore://care/memory.api_key"},
            "platform": {"api_key": "literal-platform-key"},
        }
    )

    config_module._resolve_api_key_secrets(config)

    assert backend.calls == [("care", "mage.api_key")]
    assert config.mage.api_key is None
    assert config.mage.web_search_api_key is None
    assert config.memory.api_key is None
    assert config.platform.api_key == "literal-platform-key"


def test_ordinary_keystore_failure_does_not_trip_interaction_breaker(
    monkeypatch,
) -> None:
    backend = _ScriptedKeystore(
        {
            ("care", "mage.api_key"): KeystoreError("synthetic backend failure"),
            ("care", "mage.web_search_api_key"): "search-key",
            ("care", "memory.api_key"): "memory-key",
        }
    )
    monkeypatch.setattr(keystore_module, "detect_keystore", lambda: backend)
    config = CareConfig.model_validate(
        {
            "mage": {
                "api_key": "keystore://care/mage.api_key",
                "web_search_api_key": "keystore://care/mage.web_search_api_key",
            },
            "memory": {"api_key": "keystore://care/memory.api_key"},
        }
    )

    config_module._resolve_api_key_secrets(config)

    assert backend.calls == [
        ("care", "mage.api_key"),
        ("care", "mage.web_search_api_key"),
        ("care", "memory.api_key"),
    ]
    assert config.mage.api_key is None
    assert config.mage.web_search_api_key == "search-key"
    assert config.memory.api_key == "memory-key"
