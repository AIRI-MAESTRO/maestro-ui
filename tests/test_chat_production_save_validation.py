"""Production saves require successful CARL typed-runtime admission."""

from __future__ import annotations

import sys
import types
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from care.screens.chat import ChatScreen


class _Memory:
    def __init__(self) -> None:
        self.save_chain = MagicMock(return_value="must-not-save")


def _screen(memory: _Memory) -> tuple[SimpleNamespace, list[dict[str, Any]]]:
    posted: list[dict[str, Any]] = []

    def _post_line(
        role: str,
        text: str,
        *,
        severity: str | None = None,
        **_: Any,
    ) -> None:
        posted.append({"role": role, "text": text, "severity": severity})

    return SimpleNamespace(
        app=SimpleNamespace(memory=memory),
        _post_line=_post_line,
    ), posted


def _mage_result() -> SimpleNamespace:
    return SimpleNamespace(
        chain_dict={
            "name": "Invalid",
            "steps": [
                {
                    "step_type": "llm",
                    "number": 1,
                    "title": "Broken",
                    "dependencies": [99],
                }
            ],
        }
    )


@pytest.mark.asyncio
async def test_production_save_aborts_when_carl_is_unavailable() -> None:
    memory = _Memory()
    screen, posted = _screen(memory)

    with patch.dict(sys.modules, {"mmar_carl": None}):
        result = await ChatScreen._save_chain_production(
            screen,
            task="Save this chain",
            mage_result=_mage_result(),
        )

    assert result is None
    memory.save_chain.assert_not_called()
    assert posted[-1]["severity"] == "error"
    assert "typed-validated" in posted[-1]["text"]
    assert "was not saved" in posted[-1]["text"]


@pytest.mark.asyncio
async def test_production_save_aborts_when_typed_validation_fails() -> None:
    memory = _Memory()
    screen, posted = _screen(memory)

    class _ReasoningChain:
        @staticmethod
        def from_dict(chain: dict[str, Any], *, use_typed_steps: bool) -> Any:
            assert use_typed_steps is True
            raise ValueError("dependency 99 does not exist")

    fake_carl = types.ModuleType("mmar_carl")
    fake_carl.ReasoningChain = _ReasoningChain  # type: ignore[attr-defined]
    with patch.dict(sys.modules, {"mmar_carl": fake_carl}):
        result = await ChatScreen._save_chain_production(
            screen,
            task="Save this chain",
            mage_result=_mage_result(),
        )

    assert result is None
    memory.save_chain.assert_not_called()
    assert posted[-1]["severity"] == "error"
    assert "typed validation" in posted[-1]["text"]
    assert "dependency 99 does not exist" in posted[-1]["text"]
