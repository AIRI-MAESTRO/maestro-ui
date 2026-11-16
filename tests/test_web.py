"""Keyless tests for Maestro's stdlib web transport."""

from __future__ import annotations

import asyncio
import copy
import hashlib
import http.client
import json
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Iterator

import pytest

from care.config import CareConfig, MageConfig, StagePolicy
from care.desktop_settings import DesktopSettingsBridge
from care.desktop_sessions import DesktopSessionStore
from care.runtime.carl_streamer import (
    HumanInputRequested,
    StepCompleted,
    StepEvent,
    StepStarted,
)
from care.runtime.mage_poster import LLMChunk, StageCompleted, StageStarted
from care.runtime.keystore import Keystore
from care.desktop_commands import CommandBridge
from care.web import (
    MAX_REQUEST_BYTES,
    RunManager,
    RuntimeEventSink,
    _default_production_runner,
    _execute_chain_runtime,
    _platform_evolution_configured,
    _production_config_state_sha256,
    _prepare_chain_runtime,
    _resolve_production_policies,
    create_server,
    json_safe,
    map_runtime_message,
)


_MISSING = object()
_AUTH_COOKIES: dict[int, str] = {}


class _WebTestKeystore(Keystore):
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


@contextmanager
def _live_server(
    tmp_path: Path,
    runner: Any,
    *,
    max_history: int = 16,
    command_bridge: CommandBridge | None = None,
    settings_bridge: DesktopSettingsBridge | None = None,
) -> Iterator[tuple[Any, RunManager, int]]:
    assets = tmp_path / "assets"
    assets.mkdir()
    (assets / "index.html").write_text(
        '<!doctype html><link rel="stylesheet" href="styles.css"><main>Maestro</main>',
        encoding="utf-8",
    )
    (assets / "styles.css").write_text("main { color: #234; }", encoding="utf-8")
    session_store = DesktopSessionStore(
        workspace_root=tmp_path,
        state_dir=tmp_path / "state",
    )
    manager = RunManager(
        runner=runner,
        session_store=session_store,
        max_history=max_history,
    )
    server = create_server(
        host="127.0.0.1",
        port=0,
        run_manager=manager,
        session_store=session_store,
        command_bridge=command_bridge,
        settings_bridge=settings_bridge,
        assets_dir=assets,
        workspace_root=tmp_path,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.server_address[1]
    bootstrap = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        launch_path = server.launch_url.removeprefix(server.base_url)
        bootstrap.request("GET", "/" + launch_path)
        response = bootstrap.getresponse()
        response.read()
        assert response.status == 303
        cookie = response.getheader("Set-Cookie")
        assert cookie is not None
        _AUTH_COOKIES[port] = cookie.split(";", 1)[0]
    finally:
        bootstrap.close()
    try:
        yield server, manager, port
    finally:
        _AUTH_COOKIES.pop(port, None)
        server.shutdown()
        server.server_close()
        manager.shutdown(wait=True)
        thread.join(2)


def _request(
    port: int,
    method: str,
    path: str,
    payload: Any = _MISSING,
    *,
    headers: dict[str, str] | None = None,
) -> tuple[int, dict[str, str], bytes]:
    body: bytes | None = None
    request_headers = dict(headers or {})
    if port in _AUTH_COOKIES:
        request_headers.setdefault("Cookie", _AUTH_COOKIES[port])
    if payload is not _MISSING:
        body = json.dumps(payload).encode("utf-8")
        request_headers.setdefault("Content-Type", "application/json")
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        connection.request(method, path, body=body, headers=request_headers)
        response = connection.getresponse()
        data = response.read()
        return response.status, dict(response.getheaders()), data
    finally:
        connection.close()


def _json_request(*args: Any, **kwargs: Any) -> tuple[int, dict[str, str], Any]:
    status, headers, raw = _request(*args, **kwargs)
    return status, headers, json.loads(raw.decode("utf-8"))


def _poll_http_run(port: int, run_id: str, wanted: set[str]) -> dict[str, Any]:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        status, _, run = _json_request(port, "GET", f"/api/runs/{run_id}")
        assert status == 200
        if run["status"] in wanted:
            return run
        time.sleep(0.01)
    raise AssertionError(f"run {run_id} did not reach {wanted}")


def test_json_safe_handles_cycles_models_non_finite_and_secrets() -> None:
    @dataclass
    class Row:
        value: int
        api_key: str

    circular: list[Any] = []
    circular.append(circular)
    projected = json_safe(
        {
            "row": Row(3, "do-not-leak"),
            "values": [float("nan"), Path("x"), circular],
            "authorization": "Bearer secret",
        },
    )
    assert projected["row"] == {"value": 3}
    assert projected["authorization"] == "[redacted]"
    assert projected["values"][0] is None
    assert projected["values"][1] == "x"
    assert projected["values"][2][0] == "<circular reference>"
    json.dumps(projected, allow_nan=False)


def test_native_production_policies_merge_all_config_overrides() -> None:
    config = CareConfig()
    defaults = _resolve_production_policies(config)
    assert defaults.run is StagePolicy.ASK
    assert defaults.save is StagePolicy.AUTO
    assert defaults.baseline is StagePolicy.AUTO
    assert defaults.evolve is StagePolicy.AUTO

    override = config.chat.mode.production
    override.run = StagePolicy.AUTO
    override.save = StagePolicy.ASK
    override.baseline = StagePolicy.SKIP
    override.evolve = StagePolicy.ASK

    policies = _resolve_production_policies(config)
    assert policies.run is StagePolicy.AUTO
    assert policies.save is StagePolicy.ASK
    assert policies.baseline is StagePolicy.SKIP
    assert policies.evolve is StagePolicy.ASK


def test_native_production_platform_requires_explicit_configuration() -> None:
    config = CareConfig()
    assert not _platform_evolution_configured(config)

    config.platform.base_url = "http://localhost:8000/"
    assert not _platform_evolution_configured(config)

    config.platform.api_key = "keychain://care/platform"
    assert _platform_evolution_configured(config)

    config = CareConfig()
    config.platform.base_url = "https://platform.example/api"
    assert _platform_evolution_configured(config)

    config = CareConfig()
    config.chat.mode.production.evolve = StagePolicy.SKIP
    assert _platform_evolution_configured(config)


def test_production_state_digest_binds_config_chain_and_tool_contents(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("care.config.DEFAULT_CONFIG_PATH", tmp_path / "global.toml")
    tools = tmp_path / "tools"
    cache = tmp_path / "synthesized"
    tools.mkdir()
    cache.mkdir()
    custom = tools / "weather.py"
    custom.write_text("VALUE = 1\n", encoding="utf-8")
    (cache / "weather.json").write_text(
        '{"name":"weather","source":"return 1"}',
        encoding="utf-8",
    )
    config = CareConfig(mage=MageConfig(api_key="provider-secret"))
    config.tools.path = tools
    config.tools.synthesized_tools_path = cache
    chain = {"name": "Weather", "steps": [{"number": 1}]}

    original = _production_config_state_sha256(config, chain)
    custom.write_text("VALUE = 2\n", encoding="utf-8")
    tool_changed = _production_config_state_sha256(config, chain)
    config.mage.model = "another-model"
    config_changed = _production_config_state_sha256(config, chain)
    chain["steps"][0]["number"] = 2
    chain_changed = _production_config_state_sha256(config, chain)

    assert len(original) == 64
    assert len({original, tool_changed, config_changed, chain_changed}) == 4
    assert "provider-secret" not in original


@pytest.mark.asyncio
async def test_production_tool_snapshot_lives_through_entire_saga(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tools = tmp_path / "tools"
    cache = tmp_path / "cache"
    tools.mkdir()
    cache.mkdir()
    (tools / "custom.py").write_text("VALUE = 'A'\n", encoding="utf-8")
    cached = cache / "weather.json"
    cached.write_text('{"name":"weather","source":"A"}', encoding="utf-8")
    config = CareConfig()
    config.tools.path = tools
    config.tools.synthesized_tools_path = cache
    snapshot_roots: list[Path] = []

    async def run_saga(_context: Any, snapshot_config: Any) -> dict[str, bool]:
        snapshot_cache = Path(snapshot_config.tools.synthesized_tools_path)
        snapshot_roots.append(snapshot_cache.parent)
        assert (snapshot_cache / "weather.json").read_text(encoding="utf-8") == (
            '{"name":"weather","source":"A"}'
        )
        cached.write_text('{"name":"weather","source":"B"}', encoding="utf-8")
        await asyncio.sleep(0)
        # Baseline and later evolution both retain A even though the live cache
        # changed while the saga was running.
        assert (snapshot_cache / "weather.json").read_text(encoding="utf-8") == (
            '{"name":"weather","source":"A"}'
        )
        return {"ok": True}

    monkeypatch.setattr(
        "care.web._run_default_production_with_snapshot",
        run_saga,
    )

    assert await _default_production_runner(object(), config) == {"ok": True}
    assert len(snapshot_roots) == 1
    assert not snapshot_roots[0].exists()


@pytest.mark.asyncio
async def test_production_state_guard_blocks_before_runtime_or_user_tool_loading(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def must_not_build(_config: Any) -> Any:
        pytest.fail("provider/runtime setup must not run after a state mismatch")

    monkeypatch.setattr(
        "care.runtime.llm_client.build_carl_llm_client",
        must_not_build,
    )

    with pytest.raises(RuntimeError, match="execution was blocked"):
        await _execute_chain_runtime(
            SimpleNamespace(prompt="weather"),
            CareConfig(),
            {"name": "Agent", "steps": []},
            prepared=True,
            allow_tool_synthesis=False,
            expected_state="a" * 64,
            state_guard=lambda: "b" * 64,
        )


@pytest.mark.asyncio
async def test_production_state_guard_rechecks_after_runtime_assembly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    guard_states = iter(("a" * 64, "b" * 64))

    monkeypatch.setattr(
        "care.runtime.llm_client.build_carl_llm_client",
        lambda _config: object(),
    )
    monkeypatch.setattr(
        "mmar_carl.ReasoningChain.from_dict",
        lambda *_args, **_kwargs: SimpleNamespace(steps=[]),
    )
    monkeypatch.setattr(
        "care.runtime.executor.build_run_context",
        lambda **_kwargs: SimpleNamespace(),
    )

    async def must_not_execute(*_args: Any, **_kwargs: Any) -> Any:
        calls.append("execute")
        return None

    monkeypatch.setattr(
        "care.runtime.executor.execute_chain_async",
        must_not_execute,
    )
    context = SimpleNamespace(
        prompt="weather",
        sink=lambda _source: SimpleNamespace(post_message=lambda _message: True),
        add_cancel_callback=lambda _callback: None,
        emit=lambda *_args, **_kwargs: None,
    )

    monkeypatch.setattr(
        "care.web._production_config_state_sha256",
        lambda *_args, **_kwargs: "a" * 64,
    )
    with pytest.raises(RuntimeError, match="execution was blocked"):
        await _execute_chain_runtime(
            context,
            CareConfig(),
            {"name": "Agent", "steps": []},
            prepared=True,
            allow_tool_synthesis=False,
            expected_state="a" * 64,
            state_guard=lambda: next(guard_states),
        )
    assert calls == []


@pytest.mark.asyncio
async def test_prepare_chain_normalizes_literals_before_production_save(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def no_rewrite(*_args: Any, **_kwargs: Any) -> dict[str, bool]:
        return {"rewrote": False}

    monkeypatch.setattr(
        "care.runtime.llm_client.build_carl_llm_client",
        lambda _config: object(),
    )
    monkeypatch.setattr(
        "care.tool_planning.augment_chain_for_live_data",
        no_rewrite,
    )
    original = {
        "name": "Weather",
        "steps": [
            {
                "number": 1,
                "step_type": "tool",
                "step_config": {
                    "tool_name": "web_search",
                    "input_mapping": {"query": "weather in Moscow"},
                },
            }
        ],
    }
    context = SimpleNamespace(prompt="weather", emit=lambda *_args: None)

    prepared = await _prepare_chain_runtime(context, CareConfig(), original)

    assert original["steps"][0]["step_config"]["input_mapping"]["query"] == (
        "weather in Moscow"
    )
    assert prepared["steps"][0]["step_config"]["input_mapping"]["query"] == (
        '"weather in Moscow"'
    )


@pytest.mark.asyncio
async def test_production_runtime_imports_verified_tool_snapshot_not_live_source(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tools = tmp_path / "tools"
    cache = tmp_path / "cache"
    tools.mkdir()
    cache.mkdir()
    source = tools / "custom.py"
    source.write_text("SAFE = True\n", encoding="utf-8")
    config = CareConfig()
    config.tools.path = tools
    config.tools.synthesized_tools_path = cache
    chain = {"name": "Agent", "steps": []}
    expected = _production_config_state_sha256(config, chain)
    seen_snapshot_paths: list[Path] = []

    monkeypatch.setattr(
        "care.runtime.llm_client.build_carl_llm_client",
        lambda _config: object(),
    )
    monkeypatch.setattr(
        "mmar_carl.ReasoningChain.from_dict",
        lambda *_args, **_kwargs: SimpleNamespace(steps=[]),
    )

    def build_context(**kwargs: Any) -> SimpleNamespace:
        snapshot_path = Path(kwargs["config"].tools.path)
        seen_snapshot_paths.append(snapshot_path)
        assert snapshot_path != tools
        assert (snapshot_path / "custom.py").read_text(encoding="utf-8") == (
            "SAFE = True\n"
        )
        # A live edit after confirmation is never imported. The final source
        # guard still blocks execution, while the imported path stays frozen.
        source.write_text("raise RuntimeError('changed')\n", encoding="utf-8")
        return SimpleNamespace()

    monkeypatch.setattr("care.runtime.executor.build_run_context", build_context)
    monkeypatch.setattr(
        "care.runtime.executor.execute_chain_async",
        lambda *_args, **_kwargs: pytest.fail("changed state must not execute"),
    )
    context = SimpleNamespace(
        prompt="weather",
        sink=lambda _source: SimpleNamespace(post_message=lambda _message: True),
        add_cancel_callback=lambda _callback: None,
        emit=lambda *_args, **_kwargs: None,
    )

    with pytest.raises(RuntimeError, match="execution was blocked"):
        await _execute_chain_runtime(
            context,
            config,
            chain,
            prepared=True,
            allow_tool_synthesis=False,
            expected_state=expected,
            state_guard=lambda: _production_config_state_sha256(config, chain),
        )
    assert len(seen_snapshot_paths) == 1
    assert not seen_snapshot_paths[0].exists()


@pytest.mark.asyncio
async def test_native_production_runner_uses_deployable_saga_and_records_receipts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from care.desktop_production import BaselineReceipt
    from care.memory import CareMemory

    config = CareConfig(mage=MageConfig(api_key="provider-key"))
    calls: list[Any] = []

    class Memory:
        def list_entities(self, **kwargs: Any) -> list[Any]:
            calls.append(("lookup", kwargs))
            return []

        def save_chain(self, chain: Any, **kwargs: Any) -> str:
            calls.append(("save", chain, kwargs))
            return "agent-1"

    memory = Memory()
    monkeypatch.setattr(
        CareMemory,
        "from_config",
        classmethod(lambda _cls, _config: memory),
    )
    monkeypatch.setattr(
        "care.capability_priming.build_capabilities_for_generation",
        lambda _config, *, query: {"query": query},
    )

    def build_generator(_config: Any, **kwargs: Any) -> object:
        calls.append(("build_generator", kwargs))
        return object()

    async def run_generation(_generator: Any, query: str, **kwargs: Any) -> Any:
        calls.append(("generate", query, kwargs))
        return SimpleNamespace(
            chain_dict={
                "name": "Agent",
                "steps": [{"number": 1, "step_type": "llm", "title": "Answer"}],
            },
            metadata={"mode": "deep", "model": "model-1"},
        )

    monkeypatch.setattr("care.generation.build_mage_generator", build_generator)
    monkeypatch.setattr("care.generation.run_generation", run_generation)
    monkeypatch.setattr(
        "care.desktop_production._default_validate_chain",
        lambda chain: copy.deepcopy(chain),
    )
    monkeypatch.setattr(
        "care.desktop_production._default_record_baseline",
        lambda _memory, _request: BaselineReceipt("card-1", "success"),
    )
    monkeypatch.setattr(
        "care.web._production_config_state_sha256",
        lambda *_args, **_kwargs: "a" * 64,
    )

    async def prepare_runtime(_context: Any, _config: Any, chain: Any) -> Any:
        prepared = copy.deepcopy(chain)
        prepared["steps"][0]["title"] = "Prepared answer"
        calls.append(("prepare", copy.deepcopy(prepared)))
        return prepared

    async def execute_runtime(
        _context: Any,
        _config: Any,
        chain: Any,
        **kwargs: Any,
    ) -> Any:
        calls.append(("execute", copy.deepcopy(chain), kwargs))
        return {"success": True, "answer": "baseline answer"}, dict(chain)

    monkeypatch.setattr("care.web._prepare_chain_runtime", prepare_runtime)
    monkeypatch.setattr("care.web._execute_chain_runtime", execute_runtime)
    monkeypatch.setattr(
        "care.carl_summary.summarise_carl_result",
        lambda _result: "baseline answer",
    )

    class Context:
        prompt = "Build a reusable agent"
        generation_prompt = "Prior context\n\nCurrent request:\nBuild a reusable agent"
        mode = "deep"
        execute = True

        def __init__(self) -> None:
            self.cancel_event = asyncio.Event()
            self.events: list[tuple[str, Any]] = []
            self.cancel_callbacks: list[Any] = []
            self.questions: list[str] = []

        def add_cancel_callback(self, callback: Any) -> None:
            self.cancel_callbacks.append(callback)

        def sink(self, _source: str) -> Any:
            return SimpleNamespace(post_message=lambda _message: True)

        def emit(self, event_type: str, data: Any) -> None:
            self.events.append((event_type, data))

        async def request_human_input(self, prompt: str) -> str:
            self.questions.append(prompt)
            return "yes"

    context = Context()
    result = await _default_production_runner(context, config)

    assert result["answer"] == "baseline answer"
    assert result["production_state"] == "completed"
    assert result["production"]["state"] == "completed"
    assert result["production"]["chain_id"] == "agent-1"
    assert result["production"]["baseline"]["card_id"] == "card-1"
    assert result["production"]["stages"][-1]["code"] == "platform_unavailable"
    assert context.questions and "Reply yes" in context.questions[0]
    assert '"chain_sha256"' in context.questions[0]
    assert "Confirmation:" in context.questions[0]
    assert "state aaaaaaaaaaaa" in context.questions[0]
    assert next(item for item in calls if item[0] == "build_generator")[1][
        "deployable"
    ] is True
    assert [item[0] for item in calls].index("save") < [
        item[0] for item in calls
    ].index("execute")
    saved_chain = next(item for item in calls if item[0] == "save")[1]
    executed = next(item for item in calls if item[0] == "execute")
    assert saved_chain["steps"][0]["title"] == "Prepared answer"
    assert executed[1] == {
        "name": "Agent",
        "steps": [
            {"number": 1, "step_type": "llm", "title": "Prepared answer"}
        ],
    }
    assert executed[2]["prepared"] is True
    assert executed[2]["allow_tool_synthesis"] is False
    assert executed[2]["expected_state"] == "a" * 64
    assert executed[2]["state_guard"]() == "a" * 64
    assert any(event_type == "production.stage" for event_type, _ in context.events)
    assert any(event_type == "production.completed" for event_type, _ in context.events)


@pytest.mark.asyncio
async def test_native_production_execute_off_uses_deployable_preview_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression for ProductionRequest lacking a ``deployable`` attribute."""

    from care.memory import CareMemory

    config = CareConfig(mage=MageConfig(api_key="provider-key"))
    build_calls: list[dict[str, Any]] = []
    typed_inputs: list[dict[str, Any]] = []

    monkeypatch.setattr(
        "care.capability_priming.build_capabilities_for_generation",
        lambda _config, *, query: {"query": query},
    )

    def build_generator(_config: Any, **kwargs: Any) -> object:
        build_calls.append(kwargs)
        return object()

    async def run_generation(_generator: Any, _query: str, **_kwargs: Any) -> Any:
        return SimpleNamespace(
            chain_dict={
                "name": "Weather agent",
                "steps": [
                    {"number": 1, "step_type": "llm", "title": "Weather"}
                ],
            },
            metadata={"mode": "deep"},
        )

    async def prepare_runtime(_context: Any, _config: Any, chain: Any) -> Any:
        return copy.deepcopy(chain)

    class FakeReasoningChain:
        @classmethod
        def from_dict(cls, chain: Any, **_kwargs: Any) -> object:
            typed_inputs.append(copy.deepcopy(chain))
            return object()

    monkeypatch.setattr("care.generation.build_mage_generator", build_generator)
    monkeypatch.setattr("care.generation.run_generation", run_generation)
    monkeypatch.setattr("care.web._prepare_chain_runtime", prepare_runtime)
    monkeypatch.setattr("mmar_carl.ReasoningChain", FakeReasoningChain)
    monkeypatch.setattr(
        CareMemory,
        "from_config",
        classmethod(lambda _cls, _config: pytest.fail("preview must not open Memory")),
    )

    class Context:
        prompt = "какая погода в Москве?"
        generation_prompt = prompt
        mode = "deep"
        execute = False

        def __init__(self) -> None:
            self.cancel_event = asyncio.Event()
            self.events: list[tuple[str, Any]] = []

        def add_cancel_callback(self, _callback: Any) -> None:
            return None

        def sink(self, _source: str) -> Any:
            return SimpleNamespace(post_message=lambda _message: True)

        def emit(self, event_type: str, data: Any) -> None:
            self.events.append((event_type, data))

    result = await _default_production_runner(Context(), config)

    assert build_calls[0]["deployable"] is True
    assert typed_inputs == [result["chain"]]
    assert result["production"] == {
        "state": "preview_only",
        "committed": False,
        "deployable": True,
    }
    assert "Execute was off" in result["answer"]


@pytest.mark.asyncio
async def test_native_production_runner_marks_partial_baseline_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from care.desktop_production import (
        BaselineReceipt,
        ProductionCode,
        ProductionStage,
        ProductionStageResult,
        ProductionStageState,
        ProductionTransactionResult,
        ProductionTransactionState,
    )
    from care.memory import CareMemory

    config = CareConfig(mage=MageConfig(api_key="provider-key"))
    config.platform.base_url = ""
    config.platform.master_base_url = ""
    monkeypatch.setattr(
        CareMemory,
        "from_config",
        classmethod(lambda _cls, _config: object()),
    )
    monkeypatch.setattr(
        "care.capability_priming.build_capabilities_for_generation",
        lambda _config, *, query: {"query": query},
    )

    async def execute_runtime(
        _context: Any,
        _config: Any,
        chain: Any,
        **_kwargs: Any,
    ) -> Any:
        return {"success": True, "answer": "baseline answer"}, dict(chain)

    monkeypatch.setattr("care.web._execute_chain_runtime", execute_runtime)
    monkeypatch.setattr(
        "care.carl_summary.summarise_carl_result",
        lambda _result: "baseline answer",
    )

    class PartialService:
        def __init__(self, **kwargs: Any) -> None:
            self.run_baseline = kwargs["run_baseline"]

        async def execute(self, _request: Any, *, cancellation: Any) -> Any:
            await self.run_baseline(
                SimpleNamespace(chain_dict={"name": "Agent", "steps": []}),
                cancellation,
            )
            return ProductionTransactionResult(
                state=ProductionTransactionState.PARTIAL,
                stages=(
                    ProductionStageResult(
                        ProductionStage.RUN,
                        ProductionStageState.DONE,
                        ProductionCode.OK,
                        "Baseline execution succeeded.",
                    ),
                    ProductionStageResult(
                        ProductionStage.EVOLVE,
                        ProductionStageState.FAILED,
                        ProductionCode.EVOLUTION_FAILED,
                        "Platform evolution submission failed.",
                    ),
                ),
                events=(),
                chain_id="agent-1",
                display_name="Agent",
                baseline=BaselineReceipt("card-1", "success"),
            )

    monkeypatch.setattr(
        "care.desktop_production.DesktopProductionService",
        PartialService,
    )

    class Context:
        prompt = "Build a reusable agent"
        generation_prompt = "Build a reusable agent"
        mode = "deep"
        execute = True

        def __init__(self) -> None:
            self.cancel_event = asyncio.Event()
            self.events: list[tuple[str, Any]] = []

        def add_cancel_callback(self, _callback: Any) -> None:
            return None

        def emit(self, event_type: str, data: Any) -> None:
            self.events.append((event_type, data))

    context = Context()
    result = await _default_production_runner(context, config)

    assert result["production_state"] == "partial"
    assert result["production"]["state"] == "partial"
    assert result["answer"].startswith("Production transaction is partial:")
    assert "Incomplete stages: evolve." in result["answer"]
    assert result["answer"].endswith("baseline answer")
    assert context.events[-1][1]["state"] == "partial"


@pytest.mark.parametrize(
    ("message", "source", "event_type", "data"),
    [
        (
            StageStarted("domain_analysis"),
            "generation",
            "generation.stage.started",
            {"stage": "domain_analysis"},
        ),
        (
            StageCompleted("plan", {"steps": 2}),
            "generation",
            "generation.stage.completed",
            {"stage": "plan", "result": {"steps": 2}},
        ),
        (
            LLMChunk("describe", "hello"),
            "generation",
            "generation.chunk",
            {"stage": "describe", "delta": "hello"},
        ),
        (
            StepStarted(2, "Synthesis"),
            "execution",
            "execution.step.started",
            {"step_number": 2, "step_title": "Synthesis"},
        ),
        (
            StepCompleted(
                {
                    "step_number": 1,
                    "step_title": "Parallel A",
                    "success": True,
                    "result": "A",
                },
            ),
            "execution",
            "execution.step.completed",
            {
                "step_number": 1,
                "step_title": "Parallel A",
                "success": True,
                "error": None,
                "result": {
                    "step_number": 1,
                    "step_title": "Parallel A",
                    "success": True,
                    "result": "A",
                },
            },
        ),
        (
            StepEvent(2, "agent.tool", {"name": "search"}),
            "execution",
            "execution.step.event",
            {
                "step_number": 2,
                "event_type": "agent.tool",
                "payload": {"name": "search"},
            },
        ),
    ],
)
def test_runtime_event_mapping(
    message: Any,
    source: str,
    event_type: str,
    data: dict[str, Any],
) -> None:
    assert map_runtime_message(message, source=source) == (event_type, data)


def test_run_manager_bounds_history_and_events() -> None:
    async def runner(context: Any) -> dict[str, str]:
        context.emit("test.one", {"n": 1})
        context.emit("test.two", {"n": 2})
        context.emit("test.three", {"n": 3})
        return {"answer": context.prompt}

    manager = RunManager(runner=runner, max_history=2, max_events=3)
    first = manager.submit("first", execute=False)
    assert manager.wait(first["id"])["status"] == "completed"
    second = manager.submit("second", execute=False)
    assert manager.wait(second["id"])["status"] == "completed"
    third = manager.submit("third", execute=False)
    assert manager.wait(third["id"])["status"] == "completed"

    ids = {run["id"] for run in manager.list_runs()}
    assert ids == {second["id"], third["id"]}
    assert len(manager.get_events(third["id"])) == 3
    manager.shutdown(wait=True)


def test_real_server_serves_static_health_and_security_headers(tmp_path: Path) -> None:
    async def runner(_context: Any) -> dict[str, str]:
        return {"answer": "ok"}

    with _live_server(tmp_path, runner) as (_server, _manager, port):
        status, headers, body = _request(port, "GET", "/")
        assert status == 200
        assert b"Maestro" in body
        assert headers["Content-Security-Policy"].startswith("default-src 'self'")
        assert headers["X-Content-Type-Options"] == "nosniff"
        assert headers["Referrer-Policy"] == "no-referrer"

        status, headers, health = _json_request(port, "GET", "/api/health")
        assert status == 200
        assert health["status"] == "ok"
        assert health["history"] == (
            "persistent workspace sessions plus bounded process-local runs"
        )
        assert headers["Cache-Control"] == "no-store"

        status, _, _ = _request(port, "GET", "/%2e%2e/pyproject.toml")
        assert status == 404


def test_persistent_session_endpoints_and_generation_context(tmp_path: Path) -> None:
    async def runner(context: Any) -> dict[str, str]:
        return {"answer": context.generation_prompt}

    with _live_server(tmp_path, runner) as (_server, _manager, port):
        status, _, initial = _json_request(port, "GET", "/api/session")
        assert status == 200
        first_session_id = initial["session_id"]
        assert initial["turns"] == []

        status, _, first = _json_request(
            port,
            "POST",
            "/api/runs",
            {"prompt": "first request", "mode": "fast", "execute": False},
        )
        assert status == 202
        first = _poll_http_run(port, first["id"], {"completed"})
        assert first["session_id"] == first_session_id
        assert first["result"] == {"answer": "first request"}

        status, _, second = _json_request(
            port,
            "POST",
            "/api/runs",
            {"prompt": "second request", "mode": "deep", "execute": False},
        )
        assert status == 202
        second = _poll_http_run(port, second["id"], {"completed"})
        enriched = second["result"]["answer"]
        assert "Previous conversation in this Maestro session" in enriched
        assert "User: first request" in enriched
        assert "Assistant: first request" in enriched
        assert enriched.endswith("Current request:\nsecond request")

        status, _, current = _json_request(port, "GET", "/api/session")
        assert status == 200
        assert current["generation_mode"] == "deep"
        assert [turn["role"] for turn in current["turns"]] == [
            "user",
            "assistant",
            "user",
            "assistant",
        ]

        status, _, fresh = _json_request(
            port,
            "POST",
            "/api/session/new",
            {"session_mode": "production", "generation_mode": "deep"},
        )
        assert status == 201
        assert fresh["session_id"] != first_session_id
        assert fresh["session_mode"] == "production"
        assert fresh["turns"] == []

        status, _, listed = _json_request(port, "GET", "/api/sessions")
        assert status == 200
        assert {item["session_id"] for item in listed["sessions"]} == {
            first_session_id,
            fresh["session_id"],
        }

        status, _, resumed = _json_request(
            port,
            "POST",
            "/api/session/resume",
            {"target": first_session_id},
        )
        assert status == 200
        assert resumed["session_id"] == first_session_id
        assert len(resumed["turns"]) == 4

        status, _, cleared = _json_request(
            port,
            "POST",
            "/api/session/clear",
            {},
        )
        assert status == 200
        assert cleared["session_id"] == first_session_id
        assert cleared["turns"] == []


def test_production_revision_pointer_is_persisted_in_session_provenance(
    tmp_path: Path,
) -> None:
    async def runner(_context: Any) -> dict[str, Any]:
        return {
            "answer": "Agent saved.",
            "production": {
                "state": "completed",
                "revision_pointer": {
                    "entity_id": "agent-123",
                    "channel": "latest",
                },
            },
        }

    with _live_server(tmp_path, runner) as (_server, _manager, port):
        status, _, created = _json_request(
            port,
            "POST",
            "/api/runs",
            {
                "prompt": "build an agent",
                "session_mode": "production",
                "execute": True,
            },
        )
        assert status == 202
        completed = _poll_http_run(port, created["id"], {"completed"})
        assert completed["result"]["production"]["revision_pointer"] == {
            "entity_id": "agent-123",
            "channel": "latest",
        }

        status, _, session = _json_request(port, "GET", "/api/session")
        assert status == 200
        assert session["turns"][-1]["provenance"] == {
            "run_id": created["id"],
            "status": "completed",
            "production_revision_pointer": {
                "entity_id": "agent-123",
                "channel": "latest",
            },
        }


def test_duplicate_production_result_does_not_repoint_session_followups(
    tmp_path: Path,
) -> None:
    async def runner(_context: Any) -> dict[str, Any]:
        return {
            "answer": "Matching agent already exists.",
            "production": {
                "state": "skipped",
                "duplicate": {
                    "entity_id": "existing-agent",
                    "display_name": "Existing agent",
                },
            },
        }

    with _live_server(tmp_path, runner) as (_server, _manager, port):
        status, _, created = _json_request(
            port,
            "POST",
            "/api/runs",
            {
                "prompt": "build an agent",
                "session_mode": "production",
                "execute": True,
            },
        )
        assert status == 202
        _poll_http_run(port, created["id"], {"completed"})

        status, _, session = _json_request(port, "GET", "/api/session")
        assert status == 200
        provenance = session["turns"][-1]["provenance"]
        assert "production_revision_pointer" not in provenance


def test_session_mode_change_starts_fresh_context_after_turns(tmp_path: Path) -> None:
    async def runner(context: Any) -> dict[str, str]:
        return {"answer": context.prompt}

    with _live_server(tmp_path, runner) as (_server, _manager, port):
        status, _, created = _json_request(
            port,
            "POST",
            "/api/runs",
            {"prompt": "chat turn", "session_mode": "interactive"},
        )
        assert status == 202
        _poll_http_run(port, created["id"], {"completed"})
        status, _, before = _json_request(port, "GET", "/api/session")
        assert status == 200

        status, _, changed = _json_request(
            port,
            "POST",
            "/api/session/modes",
            {"session_mode": "production", "generation_mode": "deep"},
        )
        assert status == 200
        assert changed["session_id"] != before["session_id"]
        assert changed["session_mode"] == "production"
        assert changed["generation_mode"] == "deep"
        assert changed["turns"] == []


def test_workspace_image_route_is_authenticated_digest_bound_and_no_store(
    tmp_path: Path,
) -> None:
    async def runner(_context: Any) -> None:
        return None

    image = tmp_path / "preview.png"
    content = b"\x89PNG\r\n\x1a\n" + b"synthetic-image-body"
    image.write_bytes(content)
    digest = hashlib.sha256(content).hexdigest()
    with _live_server(tmp_path, runner) as (_server, _manager, port):
        status, headers, body = _request(
            port,
            "GET",
            f"/api/workspace-images/preview.png?sha256={digest}",
        )
        assert status == 200
        assert body == content
        assert headers["Content-Type"] == "image/png"
        assert headers["Cache-Control"] == "no-store"

        status, _, _ = _request(
            port,
            "GET",
            f"/api/workspace-images/preview.png?sha256={'0' * 64}",
        )
        assert status == 409
        status, _, _ = _request(port, "GET", "/api/workspace-images/preview.png")
        assert status == 404


def test_workspace_file_route_writes_utf8_atomically_below_workspace(
    tmp_path: Path,
) -> None:
    async def runner(_context: Any) -> None:
        return None

    with _live_server(tmp_path, runner) as (_server, _manager, port):
        status, headers, result = _json_request(
            port,
            "POST",
            "/api/workspace-files",
            {"path": "generated/example.py", "content": "print('маэстро')\n"},
        )

        assert status == 201
        assert headers["Cache-Control"] == "no-store"
        assert result["path"] == "generated/example.py"
        assert result["lines"] == 1
        assert "content" not in result
        saved = tmp_path / "generated" / "example.py"
        assert saved.read_text(encoding="utf-8") == "print('маэстро')\n"
        assert result["sha256"] == hashlib.sha256(saved.read_bytes()).hexdigest()

        status, _, result = _json_request(
            port,
            "POST",
            "/api/workspace-files",
            {"path": "generated/example.py", "content": "replacement\n"},
        )
        assert status == 201
        assert result["bytes"] == len(b"replacement\n")
        assert saved.read_text(encoding="utf-8") == "replacement\n"


@pytest.mark.parametrize("path", ["../escape.py", "/tmp/escape.py", ".", ""])
def test_workspace_file_route_rejects_escape_and_invalid_targets(
    tmp_path: Path,
    path: str,
) -> None:
    async def runner(_context: Any) -> None:
        return None

    with _live_server(tmp_path, runner) as (_server, _manager, port):
        status, _, error = _json_request(
            port,
            "POST",
            "/api/workspace-files",
            {"path": path, "content": "do not write"},
        )
        assert status == 400
        assert error["error"]["code"] == "invalid_request"



def test_launch_token_is_one_time_and_api_requires_session_cookie(
    tmp_path: Path,
) -> None:
    async def runner(_context: Any) -> None:
        return None

    assets = tmp_path / "auth-assets"
    assets.mkdir()
    (assets / "index.html").write_text("ok", encoding="utf-8")
    session_store = DesktopSessionStore(
        workspace_root=tmp_path,
        state_dir=tmp_path / "auth-state",
    )
    manager = RunManager(runner=runner, session_store=session_store)
    server = create_server(
        host="127.0.0.1",
        port=0,
        run_manager=manager,
        session_store=session_store,
        assets_dir=assets,
        workspace_root=tmp_path,
    )
    launch_path = "/" + server.launch_url.removeprefix(server.base_url)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.server_address[1]

    def raw(path: str, *, cookie: str | None = None) -> tuple[int, Any, bytes]:
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        try:
            headers = {"Cookie": cookie} if cookie else {}
            connection.request("GET", path, headers=headers)
            response = connection.getresponse()
            return response.status, response.getheader("Set-Cookie"), response.read()
        finally:
            connection.close()

    try:
        status, _, _ = raw("/api/health")
        assert status == 401
        status, _, _ = raw("/?token=wrong")
        assert status == 403
        status, set_cookie, _ = raw(launch_path)
        assert status == 303
        cookie = set_cookie.split(";", 1)[0]
        assert "HttpOnly" in set_cookie
        assert "SameSite=Strict" in set_cookie
        status, _, _ = raw(launch_path)
        assert status == 403
        status, _, body = raw("/api/health", cookie=cookie)
        assert status == 200
        assert json.loads(body)["status"] == "ok"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(2)


def test_real_server_run_api_and_sse_replay(tmp_path: Path) -> None:
    async def runner(context: Any) -> dict[str, Any]:
        context.emit("generation.stage.started", {"stage": "plan"})
        await asyncio.sleep(0.01)
        context.emit("generation.completed", {"chain": {"steps": []}})
        return {"chain": {"steps": []}}

    with _live_server(tmp_path, runner) as (_server, _manager, port):
        status, _, created = _json_request(
            port,
            "POST",
            "/api/runs",
            {"prompt": "Build a plan", "mode": "fast", "execute": False},
        )
        assert status == 202
        run = _poll_http_run(port, created["id"], {"completed"})
        assert run["result"] == {"chain": {"steps": []}}

        status, _, listing = _json_request(port, "GET", "/api/runs")
        assert status == 200
        assert listing["runs"][0]["id"] == created["id"]

        status, headers, stream = _request(
            port,
            "GET",
            f"/api/runs/{created['id']}/events",
        )
        text = stream.decode("utf-8")
        assert status == 200
        assert headers["Content-Type"].startswith("text/event-stream")
        assert "event: run.created" in text
        assert "event: generation.stage.started" in text
        assert "event: run.completed" in text


def test_real_server_command_registry_and_in_process_execution(tmp_path: Path) -> None:
    async def runner(_context: Any) -> None:
        return None

    calls: list[int] = []

    def catalog_handler(args: Any, stdout: Any, _stderr: Any) -> int:
        calls.append(1)
        stdout.write(f"json={args.json}\n")
        return 0

    bridge = CommandBridge(handler_overrides={"catalog": catalog_handler})
    with _live_server(
        tmp_path,
        runner,
        command_bridge=bridge,
    ) as (_server, _manager, port):
        status, _, registry = _json_request(port, "GET", "/api/commands")
        assert status == 200
        assert registry["legacy_counts"] == {
            "top_level": 32,
            "leaf_routes": 37,
        }
        assert registry["counts"]["top_level"] == 33
        assert registry["counts"]["leaf_routes"] == 38
        assert registry["counts"]["aliases"] == 3
        assert registry["counts"]["slash_commands"] == 44
        assert registry["policy_complete"] is True

        status, _, result = _json_request(
            port,
            "POST",
            "/api/commands",
            {"command": "maestro catalog --json"},
        )
        assert status == 200
        assert result["ok"] is True
        assert result["stdout"] == "json=True\n"
        assert result["command"]["name"] == "catalog"
        assert calls == [1]


def test_command_jobs_are_background_cancellable_and_one_use(tmp_path: Path) -> None:
    async def runner(_context: Any) -> None:
        return None

    calls: list[int] = []

    def catalog_handler(args: Any, stdout: Any, _stderr: Any) -> int:
        calls.append(1)
        stdout.write(f"json={args.json}\n")
        return 0

    bridge = CommandBridge(handler_overrides={"catalog": catalog_handler})
    with _live_server(
        tmp_path,
        runner,
        command_bridge=bridge,
    ) as (_server, _manager, port):
        status, _, submitted = _json_request(
            port,
            "POST",
            "/api/command-jobs",
            {"command": "maestro catalog --json"},
        )
        assert status == 202
        assert submitted["status"] in {"queued", "running", "completed"}
        assert "stdout" not in submitted
        job_id = submitted["id"]

        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            status, _, job = _json_request(
                port,
                "GET",
                f"/api/command-jobs/{job_id}",
            )
            assert status == 200
            if job["status"] in {"completed", "failed", "cancelled"}:
                break
            time.sleep(0.01)
        else:
            raise AssertionError("command job did not finish")
        assert job["status"] == "completed"
        assert job["result_available"] is True

        status, _, transcript = _json_request(
            port,
            "POST",
            f"/api/command-jobs/{job_id}/result",
            {},
        )
        assert status == 200
        assert transcript["result"]["http_status"] == 200
        assert transcript["result"]["payload"]["stdout"] == "json=True\n"
        assert calls == [1]

        status, _, replay = _json_request(
            port,
            "POST",
            f"/api/command-jobs/{job_id}/result",
            {},
        )
        assert status == 409
        assert replay["error"]["code"] == "command_job_conflict"

        status, _, events = _json_request(
            port,
            "GET",
            "/api/command-jobs/events?after=0&limit=10",
        )
        assert status == 200
        assert {event["type"] for event in events["events"]} >= {
            "command_job.queued",
            "command_job.started",
            "command_job.completed",
            "command_job.result_consumed",
        }


def test_command_api_confirmation_is_server_side_and_secret_safe(
    tmp_path: Path,
) -> None:
    async def runner(_context: Any) -> None:
        return None

    calls: list[str] = []

    def init_handler(args: Any, stdout: Any, _stderr: Any, *, stdin: Any) -> int:
        assert stdin.read() == ""
        calls.append(args.api_key)
        stdout.write(f"configured {args.api_key}\n")
        return 0

    bridge = CommandBridge(handler_overrides={"init": init_handler})
    secret = "desktop-super-secret"
    command = f"init --api-key {secret} --non-interactive"
    with _live_server(
        tmp_path,
        runner,
        command_bridge=bridge,
    ) as (_server, _manager, port):
        status, _, challenge = _json_request(
            port,
            "POST",
            "/api/commands",
            {"command": command},
        )
        assert status == 409
        assert challenge["error"]["code"] == "confirmation_required"
        assert secret not in json.dumps(challenge)
        assert calls == []

        token = challenge["confirmation"]["token"]
        status, _, result = _json_request(
            port,
            "POST",
            "/api/commands",
            {"command": command, "confirmation_token": token},
        )
        assert status == 200
        assert result["ok"] is True
        assert result["stdout"] == "configured [redacted]\n"
        assert secret not in json.dumps(result)
        assert calls == [secret]

        status, _, replay = _json_request(
            port,
            "POST",
            "/api/commands",
            {"command": command, "confirmation_token": token},
        )
        assert status == 409
        assert replay["error"]["code"] == "confirmation_invalid"
        assert calls == [secret]


def test_command_api_validation_and_method_contract(tmp_path: Path) -> None:
    async def runner(_context: Any) -> None:
        return None

    with _live_server(tmp_path, runner) as (_server, _manager, port):
        status, _, error = _json_request(
            port,
            "POST",
            "/api/commands",
            {"command": "catalog", "extra": True},
        )
        assert status == 400
        assert error["error"]["code"] == "invalid_request"

        status, _, error = _json_request(
            port,
            "POST",
            "/api/commands",
            {"command": None},
        )
        assert status == 400
        assert error["error"]["code"] == "invalid_command"

        status, _, raw = _request(port, "HEAD", "/api/commands")
        assert status == 405
        assert raw == b""

        status, _, error = _json_request(port, "PUT", "/api/commands", {})
        assert status == 405
        assert error["error"]["code"] == "method_not_allowed"


def test_settings_api_is_secret_safe_confirmed_and_transport_fenced(
    tmp_path: Path,
) -> None:
    async def runner(_context: Any) -> None:
        return None

    secret = "SYNTHETIC_SETTINGS_SECRET"
    keystore = _WebTestKeystore()
    config = CareConfig(
        mage=MageConfig(
            api_key=secret,
            base_url="https://openrouter.ai/api/v1",
            model="example/old-model",
        )
    )
    bridge = DesktopSettingsBridge(
        config_loader=lambda: config,
        config_path=tmp_path / "settings.toml",
        env_path=tmp_path / ".env",
        cwd=tmp_path,
        keystore_factory=lambda: keystore,
        token_factory=lambda: "settings-http-token",
        secret_key_factory=lambda: "0123456789abcdef",
        environ={},
    )

    with _live_server(tmp_path, runner, settings_bridge=bridge) as (
        _server,
        _manager,
        port,
    ):
        status, _, snapshot = _json_request(port, "GET", "/api/settings")
        assert status == 200
        assert snapshot["settings"]["api_key_present"] is True
        assert "api_key" not in snapshot["settings"]
        assert secret not in json.dumps(snapshot)

        payload = {"model": "example/new-model"}
        status, _, challenge = _json_request(
            port,
            "POST",
            "/api/settings",
            payload,
        )
        assert status == 409
        assert challenge["error"]["code"] == "confirmation_required"
        assert secret not in json.dumps(challenge)

        status, _, error = _json_request(
            port,
            "POST",
            "/api/settings",
            payload,
            headers={"Origin": "https://evil.example"},
        )
        assert status == 403
        assert error["error"]["code"] == "cross_origin"

        status, _, error = _json_request(
            port,
            "POST",
            "/api/settings",
            _MISSING,
            headers={"Content-Type": "text/plain", "Content-Length": "0"},
        )
        assert status == 415
        assert error["error"]["code"] == "unsupported_media_type"

        status, _, error = _json_request(
            port,
            "GET",
            "/api/settings",
            headers={"Cookie": ""},
        )
        assert status == 401
        assert error["error"]["code"] == "unauthorized"

        status, _, error = _json_request(
            port,
            "GET",
            "/api/settings",
            headers={"Host": "evil.example"},
        )
        assert status == 421
        assert error["error"]["code"] == "invalid_host"

        status, _, raw = _request(port, "HEAD", "/api/settings")
        assert status == 405
        assert raw == b""
        status, _, error = _json_request(port, "PUT", "/api/settings", {})
        assert status == 405
        assert error["error"]["code"] == "method_not_allowed"

        status, _, saved = _json_request(
            port,
            "POST",
            "/api/settings",
            {**payload, "confirmation_token": challenge["confirmation"]["token"]},
        )
        assert status == 200
        assert saved["ok"] is True
        assert secret not in json.dumps(saved)
        assert secret not in (tmp_path / "settings.toml").read_text(encoding="utf-8")
        assert secret not in (tmp_path / ".env").read_text(encoding="utf-8")


def test_settings_get_loader_failure_has_stable_response_and_secret_safe_log(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def runner(_context: Any) -> None:
        return None

    secret = "sk-synthetic-web-loader-secret"

    def failing_loader() -> CareConfig:
        raise RuntimeError(f"api_key={secret}")

    bridge = DesktopSettingsBridge(
        config_loader=failing_loader,
        config_path=tmp_path / "settings.toml",
        env_path=tmp_path / ".env",
        cwd=tmp_path,
        environ={},
    )
    with _live_server(tmp_path, runner, settings_bridge=bridge) as (
        _server,
        _manager,
        port,
    ):
        status, _, error = _json_request(port, "GET", "/api/settings")

    assert status == 500
    assert error["error"]["code"] == "settings_load_failed"
    assert secret not in json.dumps(error)
    assert secret not in caplog.text


def test_real_server_cancel_is_idempotent_and_single_terminal_event(
    tmp_path: Path,
) -> None:
    entered = threading.Event()

    async def runner(_context: Any) -> None:
        entered.set()
        await asyncio.sleep(60)

    with _live_server(tmp_path, runner) as (_server, manager, port):
        status, _, created = _json_request(
            port,
            "POST",
            "/api/runs",
            {"prompt": "wait"},
        )
        assert status == 202
        assert entered.wait(2)
        for _ in range(2):
            status, _, _ = _json_request(
                port,
                "POST",
                f"/api/runs/{created['id']}/cancel",
                {},
            )
            assert status == 202
        run = _poll_http_run(port, created["id"], {"cancelled"})
        assert run["status"] == "cancelled"
        terminal = [
            event
            for event in manager.get_events(created["id"])
            if event["type"] == "run.cancelled"
        ]
        assert len(terminal) == 1


def test_cooperative_cancel_preserves_typed_production_receipts(
    tmp_path: Path,
) -> None:
    entered = threading.Event()

    async def runner(context: Any) -> dict[str, Any]:
        entered.set()
        await context.cancel_event.wait()
        return {
            "answer": "Cancelled after save.",
            "production_state": "cancelled",
            "production": {
                "state": "cancelled",
                "chain_id": "agent-committed",
                "revision_pointer": {
                    "entity_id": "agent-committed",
                    "channel": "latest",
                },
            },
        }

    store = DesktopSessionStore(
        workspace_root=tmp_path,
        state_dir=tmp_path / "state-cooperative",
    )
    manager = RunManager(
        runner=runner,
        session_store=store,
        cancel_grace_seconds=0.5,
    )
    try:
        created = manager.submit(
            "build",
            session_mode="production",
            execute=True,
        )
        assert entered.wait(2)
        manager.cancel_run(created["id"])
        terminal = manager.wait(created["id"], timeout=2)

        assert terminal["status"] == "cancelled"
        assert terminal["result"]["production"]["chain_id"] == "agent-committed"
        events = manager.get_events(created["id"])
        cancelled = [event for event in events if event["type"] == "run.cancelled"]
        assert len(cancelled) == 1
        assert cancelled[0]["data"]["result"]["production_state"] == "cancelled"
        session = store.current_session()
        assert session.turns[-1].provenance["production_revision_pointer"] == {
            "entity_id": "agent-committed",
            "channel": "latest",
        }
    finally:
        manager.shutdown(wait=True)


def test_cancel_callback_registered_after_signal_is_delivered_immediately() -> None:
    entered = threading.Event()
    callback_called = threading.Event()

    async def runner(context: Any) -> dict[str, str]:
        entered.set()
        await context.cancel_event.wait()
        # The manager has already copied and cleared its original callback
        # list by the time this wait resumes.
        context.add_cancel_callback(callback_called.set)
        return {"answer": "cooperatively stopped"}

    manager = RunManager(runner=runner, cancel_grace_seconds=0.5)
    try:
        created = manager.submit("wait")
        assert entered.wait(2)
        manager.cancel_run(created["id"])
        terminal = manager.wait(created["id"], timeout=2)

        assert terminal["status"] == "cancelled"
        assert callback_called.is_set()
    finally:
        manager.shutdown(wait=True)


def test_completed_sync_production_commit_wins_over_late_cancel_request() -> None:
    entered = threading.Event()
    release = threading.Event()

    async def runner(_context: Any) -> dict[str, Any]:
        entered.set()
        # Model a synchronous HTTP submit that temporarily blocks the owner
        # loop and returns a committed Platform receipt.
        assert release.wait(2)
        return {
            "answer": "Evolution submitted.",
            "production_state": "completed",
            "production": {
                "state": "completed",
                "chain_id": "agent-1",
                "evolution": {"evolution_id": "evo-1", "status": "queued"},
                "revision_pointer": {
                    "entity_id": "agent-1",
                    "channel": "latest",
                },
            },
        }

    manager = RunManager(runner=runner, cancel_grace_seconds=0.1)
    try:
        created = manager.submit("evolve", session_mode="production")
        assert entered.wait(2)
        manager.cancel_run(created["id"])
        release.set()
        terminal = manager.wait(created["id"], timeout=2)

        assert terminal["status"] == "completed"
        assert terminal["cancel_requested"] is True
        assert terminal["result"]["cancel_requested_after_commit"] is True
        assert terminal["result"]["production"]["evolution"] == {
            "evolution_id": "evo-1",
            "status": "queued",
        }
        event_types = [event["type"] for event in manager.get_events(created["id"])]
        assert event_types[-1] == "run.completed"
        assert "run.cancelled" not in event_types
    finally:
        release.set()
        manager.shutdown(wait=True)


def test_real_server_human_input_resolves_loop_owned_future(tmp_path: Path) -> None:
    async def runner(context: Any) -> dict[str, str]:
        answer = await context.request_human_input("Your name?")
        return {"answer": f"Hello, {answer}"}

    with _live_server(tmp_path, runner) as (_server, _manager, port):
        status, _, created = _json_request(
            port,
            "POST",
            "/api/runs",
            {"prompt": "greet"},
        )
        assert status == 202
        waiting = _poll_http_run(port, created["id"], {"waiting_for_input"})
        request = waiting["waiting_for_input"]
        assert request["prompt"] == "Your name?"

        status, _, _ = _json_request(
            port,
            "POST",
            f"/api/runs/{created['id']}/input",
            {"request_id": request["request_id"], "value": "Ada"},
        )
        assert status == 202
        completed = _poll_http_run(port, created["id"], {"completed"})
        assert completed["result"] == {"answer": "Hello, Ada"}
        assert completed["waiting_for_input"] is None


def test_post_security_content_type_origin_and_size_limits(tmp_path: Path) -> None:
    async def runner(_context: Any) -> None:
        return None

    with _live_server(tmp_path, runner) as (_server, _manager, port):
        status, _, error = _json_request(
            port,
            "POST",
            "/api/runs",
            _MISSING,
            headers={"Content-Type": "text/plain", "Content-Length": "0"},
        )
        assert status == 415
        assert error["error"]["code"] == "unsupported_media_type"

        status, _, error = _json_request(
            port,
            "POST",
            "/api/runs",
            {"prompt": "x"},
            headers={"Origin": "https://evil.example"},
        )
        assert status == 403
        assert error["error"]["code"] == "cross_origin"

        status, _, error = _json_request(
            port,
            "POST",
            "/api/runs",
            {"prompt": "x"},
            headers={"Origin": f"https://127.0.0.1:{port}"},
        )
        assert status == 403
        assert error["error"]["code"] == "cross_origin"

        oversized = b"{" + b" " * MAX_REQUEST_BYTES + b"}"
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        try:
            connection.request(
                "POST",
                "/api/runs",
                body=oversized,
                headers={
                    "Content-Type": "application/json",
                    "Cookie": _AUTH_COOKIES[port],
                },
            )
            response = connection.getresponse()
            assert response.status == 413
            response.read()
        finally:
            connection.close()


def test_runtime_sink_registers_carl_human_input_without_serialising_future() -> None:
    async def runner(context: Any) -> dict[str, str]:
        loop = asyncio.get_running_loop()
        future: asyncio.Future[str] = loop.create_future()
        sink = RuntimeEventSink(context._manager, context.id, source="execution")
        sink.post_message(HumanInputRequested("Continue?", future))
        return {"answer": await future}

    manager = RunManager(runner=runner)
    created = manager.submit("hitl")
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        snapshot = manager.get_run(created["id"])
        if snapshot["status"] == "waiting_for_input":
            break
        time.sleep(0.01)
    else:
        raise AssertionError("human input event was not registered")
    request_id = snapshot["waiting_for_input"]["request_id"]
    event = manager.get_events(created["id"])[-1]
    assert event["data"] == {"request_id": request_id, "prompt": "Continue?"}
    assert "future" not in json.dumps(event)
    manager.provide_human_input(created["id"], "yes", request_id=request_id)
    assert manager.wait(created["id"])["result"] == {"answer": "yes"}
    manager.shutdown(wait=True)


def test_create_server_rejects_non_loopback_bind(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="loopback-only"):
        create_server(host="0.0.0.0", port=0, assets_dir=tmp_path)


def test_create_server_supports_ipv6_loopback_when_available(tmp_path: Path) -> None:
    session_store = DesktopSessionStore(
        workspace_root=tmp_path,
        state_dir=tmp_path / "ipv6-state",
    )
    try:
        server = create_server(
            host="::1",
            port=0,
            assets_dir=tmp_path,
            workspace_root=tmp_path,
            session_store=session_store,
        )
    except OSError:
        pytest.skip("IPv6 loopback is unavailable on this host")
    try:
        assert server.server_address[0] == "::1"
    finally:
        server.server_close()
