"""Fail-closed step-type admission for headless generation."""

from __future__ import annotations

import pytest

import mmar_mage.schemas as mage_schemas
from care.config import CareConfig, MageConfig
from care.generation import GenerationError, build_mage_config


def test_deployable_config_excludes_human_input() -> None:
    config = build_mage_config(
        CareConfig(mage=MageConfig(api_key="test")),
        deployable=True,
    )

    assert config.allowed_step_types
    assert "human_input" not in config.allowed_step_types
    assert "llm" in config.allowed_step_types


def test_deployable_config_fails_when_authoritative_registry_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delattr(mage_schemas, "VALID_STEP_TYPES")

    with pytest.raises(
        GenerationError,
        match="authoritative VALID_STEP_TYPES registry is unavailable",
    ):
        build_mage_config(
            CareConfig(mage=MageConfig(api_key="test")),
            deployable=True,
        )
