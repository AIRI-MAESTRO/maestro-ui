"""Platform evolution submit ships synthesized tools as python_code."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from care.config import CareConfig
from care.platform import CarePlatform, EvolutionRef


def _minimal_chain() -> dict:
    return {
        "version": "1.0",
        "steps": [
            {
                "number": 1,
                "title": "Use tool",
                "step_type": "tool",
                "step_config": {
                    "tool_name": "get_exchange_rate",
                    "input_mapping": {"pair": "$sample.input"},
                },
            },
            {
                "number": 2,
                "title": "Answer",
                "step_type": "llm",
                "dependencies": [1],
                "aim": "Summarize using tool output",
                "stage_action": "One sentence from $history[-1]",
            },
        ],
    }


class TestPlatformBundledToolsSubmit:
    def test_start_evolution_includes_python_code(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path,
    ) -> None:
        from care import tool_synthesis

        cfg = CareConfig()
        cfg.tools.synthesized_tools_path = tmp_path / "tools"
        tool_synthesis._save_cached_tool(
            cfg.tools,
            "get_exchange_rate",
            "def get_exchange_rate(pair: str) -> str:\n    return pair",
            ["pair"],
            "fx",
        )

        monkeypatch.setattr("care.config.CareConfig.load", lambda: cfg)
        monkeypatch.setattr(CarePlatform, "_sync_llm_registry", lambda *a, **k: None)

        captured: dict = {}

        def _fake_start(**kwargs: object) -> object:
            captured.update(kwargs)
            from care.platform import EvolutionRef

            return EvolutionRef(
                evolution_id="exp_x",
                base_chain_id="c1",
                status="queued",
            )

        plat = CarePlatform(MagicMock())
        monkeypatch.setattr(plat, "_start_chain_experiment", _fake_start)

        plat.start_evolution(
            base_chain_id="c1",
            base_chain_content=_minimal_chain(),
        )

        code = captured.get("python_code")
        assert isinstance(code, str)
        assert "def get_exchange_rate" in code
        chain = captured["base_chain_content"]
        assert chain["steps"][0]["step_config"]["tool_name"] == "get_exchange_rate"

    def test_explicit_config_snapshot_is_used_without_live_reload(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path,
    ) -> None:
        from care import tool_synthesis

        frozen = CareConfig()
        frozen.tools.synthesized_tools_path = tmp_path / "frozen-tools"
        frozen.platform.mutation_model = "frozen-model"
        tool_synthesis._save_cached_tool(
            frozen.tools,
            "get_exchange_rate",
            "def get_exchange_rate(pair: str) -> str:\n    return pair",
            ["pair"],
            "fx",
        )
        sync_configs: list[CareConfig] = []
        monkeypatch.setattr(
            "care.config.CareConfig.load",
            lambda: pytest.fail("explicit Platform config must not reload live state"),
        )
        monkeypatch.setattr(
            CarePlatform,
            "_sync_llm_registry",
            staticmethod(
                lambda config, **_kwargs: sync_configs.append(config)
            ),
        )
        captured: dict = {}
        platform = CarePlatform(MagicMock(), config=frozen)

        def _fake_start(**kwargs: object) -> EvolutionRef:
            captured.update(kwargs)
            return EvolutionRef(
                evolution_id="exp-frozen",
                base_chain_id="c1",
            )

        monkeypatch.setattr(
            platform,
            "_start_chain_experiment",
            _fake_start,
        )

        platform.start_evolution(
            base_chain_id="c1",
            base_chain_content=_minimal_chain(),
        )

        assert sync_configs[0].platform.mutation_model == "frozen-model"
        assert "def get_exchange_rate" in captured["python_code"]
