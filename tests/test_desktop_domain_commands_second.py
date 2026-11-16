from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from care import cli as care_cli
from care.desktop_commands import CommandBridge
import care.desktop_domain_commands_second as second_domain
from care.desktop_domain_commands_second import (
    DesktopSecondDomainCommands,
    DesktopSecondDomainSources,
    SecondDomainRisk,
)


CHAIN = {
    "name": "Research Maestro",
    "steps": [
        {
            "number": 1,
            "title": "Answer",
            "step_type": "llm",
            "dependencies": [],
        }
    ],
}


class FakeLTM:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}

    def keys(self, *, session_id: str) -> list[str]:
        del session_id
        return list(self.values)

    def retrieve(self, key: str, *, session_id: str) -> str | None:
        del session_id
        return self.values.get(key)

    def store(self, key: str, value: str, *, session_id: str) -> None:
        del session_id
        self.values[key] = value


class FakeClient:
    def __init__(self) -> None:
        self.record = SimpleNamespace(
            entity_id="chain-1",
            version_id="version-1",
            version_number=1,
            channel="latest",
            meta={"display_name": "Research Maestro"},
            content=CHAIN,
        )
        self.target = SimpleNamespace(version_id="stable-1")
        self.promotions: list[tuple[str, str, str]] = []
        self.installs: list[dict[str, str]] = []
        self.library_query: str | None = None
        self.marketplace_query: str | None = None

    def get_chain_record(self, entity_id: str, *, channel: str = "latest") -> Any:
        if entity_id != "chain-1":
            raise KeyError(entity_id)
        if channel == "stable":
            return self.target
        return self.record

    def list_chains(self, **kwargs: Any) -> list[Any]:
        self.library_query = kwargs.get("q")
        return [
            SimpleNamespace(
                entity_id="chain-1",
                display_name="Research Maestro",
                description="A saved research chain",
                favourite=True,
                run_count=3,
                channel="latest",
                meta={"tags": ["domain:research"]},
                content=CHAIN,
            )
        ]

    def list_agent_skills(self, **kwargs: Any) -> list[Any]:
        self.marketplace_query = kwargs.get("q")
        return [
            SimpleNamespace(
                entity_id="skill-1",
                version_id="skill-version-1",
                name="PDF Reader",
                description="Read PDF files",
                tags=("pdf",),
            )
        ]

    def get_agent_skill(self, entity_id: str, *, channel: str) -> Any:
        assert channel == "latest"
        if entity_id != "skill-1":
            raise KeyError(entity_id)
        return SimpleNamespace(
            entity_id=entity_id,
            version_id="skill-version-1",
            name="PDF Reader",
            tags=("pdf",),
        )

    def save_agent_skill(self, **kwargs: str) -> dict[str, str]:
        self.installs.append(kwargs)
        return {"entity_id": kwargs["entity_id"]}

    def promote(self, entity_id: str, from_channel: str, to_channel: str) -> None:
        self.promotions.append((entity_id, from_channel, to_channel))


class FakeHub:
    def __init__(self) -> None:
        self.specs: list[dict[str, Any]] = []

    async def deploy(self, spec: dict[str, Any]) -> Any:
        self.specs.append(spec)
        return SimpleNamespace(
            name=spec["name"],
            ready=True,
            ready_reason="",
            url=f"/agents/{spec['name']}",
        )

    def agent_url(self, name: str) -> str:
        return f"http://127.0.0.1:8080/agents/{name}"

    def docs_url(self, name: str) -> str:
        return self.agent_url(name) + "/docs"


def _preflight(*, parsed: bool = True, ok: bool = True) -> Any:
    return SimpleNamespace(
        parsed=parsed,
        ok=ok,
        preflight_available=True,
        parse_errors=(),
        required_tools=(),
        missing_tools=(),
        required_mcp_servers=(),
        missing_mcp_servers=(),
        required_skills=(),
        missing_skills=(),
    )


def _sources(
    client: FakeClient,
    *,
    ltm: FakeLTM | None = None,
    complete: Any = None,
    run_chain: Any = None,
    hub: FakeHub | None = None,
    promotion_gate: Any = None,
    marketplace_search: Any = None,
) -> DesktopSecondDomainSources:
    config = SimpleNamespace(
        mage=SimpleNamespace(
            api_key="configured" if complete else "",
            base_url="https://api.example.test/v1",
            model="example-model",
            provider="custom",
            web_search_provider="",
            web_search_api_key=None,
        ),
        memory=SimpleNamespace(
            base_url="https://memory.example.test",
            api_key=None,
            timeout=10.0,
        ),
        context=SimpleNamespace(
            ltm_enabled=True,
            ltm_session_id="default",
            ltm_dir="/unused/test-ltm",
        ),
        hub=SimpleNamespace(
            base_url="http://127.0.0.1:8080",
            autostart=True,
            port=8080,
            agent_server_cmd=("carl-agent-hub", "serve"),
        ),
        tools=SimpleNamespace(
            auto_synthesize_tools=True,
            enable_code_exec=True,
            verify_cached_tools=True,
        ),
        sandbox=SimpleNamespace(kind="docker"),
    )
    memory = SimpleNamespace(client=client)
    ltm = ltm or FakeLTM()
    return DesktopSecondDomainSources(
        load_config=lambda: config,
        build_memory=lambda _config: memory,
        build_ltm=lambda _config: ltm,
        ltm_session_id=lambda _config: "default",
        recall_ltm=lambda store, session_id, max_chars: "\n".join(
            f"{key}:{store.retrieve(key, session_id=session_id)}"
            for key in store.keys(session_id=session_id)
        )[:max_chars],
        inspect_ltm=lambda _config, session_id, max_chars: "\n".join(
            f"{key}:{ltm.retrieve(key, session_id=session_id)}"
            for key in ltm.keys(session_id=session_id)
        )[:max_chars],
        build_ltm_complete=lambda _config: complete,
        marketplace_search=(
            marketplace_search or (lambda _client, _query: SimpleNamespace(listings=()))
        ),
        validate_chain=lambda _chain: _preflight(),
        run_chain=(
            run_chain
            or (
                lambda *_args, **_kwargs: {
                    "success": True,
                    "steps_completed": 1,
                    "final_answer": "done",
                }
            )
        ),
        bundle_tools=lambda _chain, _config: [],
        deploy_gate=lambda _chain, _names: [],
        ensure_hub=lambda _config: hub or FakeHub(),
        promotion_gate=(
            promotion_gate
            or (
                lambda *_args, **_kwargs: SimpleNamespace(
                    ok=True,
                    checks=(
                        SimpleNamespace(
                            name="artifact",
                            passed=True,
                            skipped=False,
                            detail="clean",
                        ),
                    ),
                )
            )
        ),
        token_factory=lambda: "agent-credential-1234567890",
    )


def _bridge(sources: DesktopSecondDomainSources) -> CommandBridge:
    counter = iter(("confirm-1", "confirm-2", "confirm-3", "confirm-4"))
    return CommandBridge(
        second_domain_commands=DesktopSecondDomainCommands(sources),
        token_factory=lambda: next(counter),
    )


def _confirm(bridge: CommandBridge, command: str) -> tuple[Any, str]:
    first = bridge.execute(command)
    assert first.status == 409
    assert first.payload["error"]["code"] == "confirmation_required"
    return first, first.payload["confirmation"]["token"]


def test_library_browse_and_query_return_structured_memory_rows() -> None:
    client = FakeClient()
    commands = DesktopSecondDomainCommands(_sources(client))

    browse = commands.plan("library", []).execute()
    assert browse.payload["action"] == "browse"
    assert browse.payload["rows"][0]["entity_id"] == "chain-1"
    assert browse.payload["rows"][0]["domain"] == "research"

    search = commands.plan("library", ["research", "maestro"]).execute()
    assert search.payload["action"] == "search"
    assert client.library_query == "research maestro"


def test_marketplace_empty_browses_and_query_searches() -> None:
    client = FakeClient()
    searched: list[str] = []

    def search(_client: Any, query: str) -> Any:
        searched.append(query)
        return SimpleNamespace(
            listings=(
                SimpleNamespace(
                    entity_id="skill-2",
                    name="Research Search",
                    description="Search papers",
                    score=0.9,
                    tags=("research",),
                    matched_via="skill_description",
                    snippet="papers",
                ),
            )
        )

    commands = DesktopSecondDomainCommands(_sources(client, marketplace_search=search))
    browse = commands.plan("marketplace", []).execute()
    assert browse.payload["action"] == "browse"
    assert browse.payload["rows"][0]["entity_id"] == "skill-1"
    assert browse.payload["install_form"].startswith("install")

    result = commands.plan("marketplace", ["search", "papers"]).execute()
    assert searched == ["search papers"]
    assert result.payload["rows"][0]["score"] == 0.9


def test_marketplace_install_is_confirmation_gated_and_one_use() -> None:
    client = FakeClient()
    bridge = _bridge(_sources(client))
    command = "/marketplace install skill-1"
    first, token = _confirm(bridge, command)
    assert first.payload["preview"]["name"] == "PDF Reader"
    assert client.installs == []

    done = bridge.execute(command, confirmation_token=token)
    assert done.payload["ok"] is True
    assert client.installs == [{"entity_id": "skill-1", "name": "PDF Reader"}]
    replay = bridge.execute(command, confirmation_token=token)
    assert replay.status == 409
    assert replay.payload["error"]["code"] == "confirmation_invalid"


def test_remember_provider_merge_runs_only_after_confirmation() -> None:
    client = FakeClient()
    ltm = FakeLTM()
    calls: list[tuple[str, str]] = []

    def complete(system: str, user: str) -> str:
        calls.append((system, user))
        return '{"facts":[{"key":"preferred_style","value":"concise"}]}'

    bridge = _bridge(_sources(client, ltm=ltm, complete=complete))
    command = "/remember I prefer concise answers"
    first, token = _confirm(bridge, command)
    assert (
        first.payload["confirmation"]["risk"]
        == SecondDomainRisk.PROVIDER_EXECUTION.value
    )
    assert calls == []
    assert ltm.values == {}

    done = bridge.execute(command, confirmation_token=token)
    assert done.payload["ok"] is True
    assert len(calls) == 1
    assert ltm.values == {"preferred_style": "concise"}


def test_remember_without_model_uses_adapted_raw_fallback() -> None:
    client = FakeClient()
    ltm = FakeLTM()
    bridge = _bridge(_sources(client, ltm=ltm, complete=None))
    command = "/remember Мой язык русский"
    first, token = _confirm(bridge, command)
    assert first.payload["preview"]["mode"] == "raw_fallback"
    assert (
        first.payload["confirmation"]["risk"]
        == SecondDomainRisk.FILESYSTEM_MUTATION.value
    )

    done = bridge.execute(command, confirmation_token=token)
    assert done.payload["ok"] is True
    assert next(iter(ltm.values.values())) == "Мой язык русский"


def test_remember_state_change_invalidates_confirmation() -> None:
    client = FakeClient()
    ltm = FakeLTM()
    bridge = _bridge(_sources(client, ltm=ltm))
    command = "/remember stable note"
    _first, token = _confirm(bridge, command)
    ltm.values["other"] = "changed while dialog was open"

    refused = bridge.execute(command, confirmation_token=token)
    assert refused.status == 409
    assert refused.payload["error"]["code"] == "confirmation_invalid"
    assert not any(value == "stable note" for value in ltm.values.values())


def test_run_inspect_is_read_only_and_execute_is_provider_gated() -> None:
    client = FakeClient()
    calls: list[dict[str, Any]] = []

    def run_chain(*_args: Any, **kwargs: Any) -> dict[str, Any]:
        calls.append(kwargs)
        return {"success": True, "steps_completed": 1, "final_answer": "done"}

    bridge = _bridge(_sources(client, run_chain=run_chain))
    inspected = bridge.execute("/run chain-1")
    assert inspected.status == 200
    assert inspected.payload["payload"]["executed"] is False
    assert inspected.payload["payload"]["steps"] == 1

    command = "/run chain-1 --execute --input topic=agents --task 'Answer now'"
    first, token = _confirm(bridge, command)
    assert first.payload["preview"]["input_keys"] == ["topic"]
    assert calls == []
    done = bridge.execute(command, confirmation_token=token)
    assert done.payload["ok"] is True
    assert len(calls) == 1
    assert calls[0]["inputs"] == {"topic": "agents"}


def test_run_version_change_invalidates_confirmation() -> None:
    client = FakeClient()
    calls: list[bool] = []
    bridge = _bridge(
        _sources(
            client,
            run_chain=lambda *_args, **_kwargs: calls.append(True) or {"success": True},
        )
    )
    command = "/run chain-1 --execute"
    _first, token = _confirm(bridge, command)
    client.record.version_id = "version-2"
    refused = bridge.execute(command, confirmation_token=token)
    assert refused.payload["error"]["code"] == "confirmation_invalid"
    assert calls == []


def test_run_runtime_change_invalidates_confirmation() -> None:
    client = FakeClient()
    calls: list[bool] = []
    sources = _sources(
        client,
        run_chain=lambda *_args, **_kwargs: calls.append(True) or {"success": True},
    )
    config = sources.load_config()
    bridge = _bridge(sources)
    command = "/run chain-1 --execute"
    _first, token = _confirm(bridge, command)
    config.mage.model = "different-model"

    refused = bridge.execute(command, confirmation_token=token)

    assert refused.status == 409
    assert refused.payload["error"]["code"] == "confirmation_invalid"
    assert calls == []


def test_deploy_gate_and_hub_effects_run_in_correct_phases() -> None:
    client = FakeClient()
    hub = FakeHub()
    ensures: list[bool] = []
    sources = _sources(client, hub=hub)
    sources = DesktopSecondDomainSources(
        **{
            **sources.__dict__,
            "ensure_hub": lambda _config: ensures.append(True) or hub,
        }
    )
    bridge = _bridge(sources)
    command = "/deploy chain-1 --channel latest --name research-agent"
    first, token = _confirm(bridge, command)
    assert first.payload["preview"]["gate_passed"] is True
    assert ensures == []
    assert hub.specs == []

    done = bridge.execute(command, confirmation_token=token)
    assert done.payload["ok"] is True
    assert ensures == [True]
    assert hub.specs[0]["entity_id"] == "chain-1"
    assert "agent-credential-1234567890" not in done.payload["stdout"]
    assert "credential_once" not in done.payload["payload"]
    assert done.payload["ephemeral_secret"] == {
        "kind": "credential",
        "value": "agent-credential-1234567890",
        "expires_in_seconds": 60,
    }


def test_deploy_gate_failure_never_offers_confirmation_or_calls_hub() -> None:
    client = FakeClient()
    hub = FakeHub()
    sources = _sources(client, hub=hub)
    sources = DesktopSecondDomainSources(
        **{**sources.__dict__, "deploy_gate": lambda _chain, _names: ["missing tool"]}
    )
    bridge = _bridge(sources)
    result = bridge.execute("/deploy chain-1 --channel latest")
    assert result.status == 409
    assert result.payload["error"]["code"] == "deploy_gate_failed"
    assert hub.specs == []


def test_deploy_credential_is_single_bounded_ephemeral_value() -> None:
    client = FakeClient()
    hub = FakeHub()
    sources = _sources(client, hub=hub)
    sources = replace(
        sources,
        token_factory=lambda: "x" * 129,
    )
    bridge = _bridge(sources)
    command = "/deploy chain-1 --channel latest"
    _first, token = _confirm(bridge, command)

    refused = bridge.execute(command, confirmation_token=token)

    assert refused.status == 500
    assert refused.payload["error"]["code"] == "credential_generation_failed"
    assert hub.specs == []
    assert "x" * 20 not in repr(refused.payload)


def test_deploy_runtime_change_invalidates_confirmation() -> None:
    client = FakeClient()
    hub = FakeHub()
    sources = _sources(client, hub=hub)
    config = sources.load_config()
    bridge = _bridge(sources)
    command = "/deploy chain-1 --channel latest"
    _first, token = _confirm(bridge, command)
    config.hub.base_url = "https://hub.example.test"

    refused = bridge.execute(command, confirmation_token=token)

    assert refused.status == 409
    assert refused.payload["error"]["code"] == "confirmation_invalid"
    assert hub.specs == []


def test_deploy_rejects_remote_plaintext_hub_before_credential_generation() -> None:
    client = FakeClient()
    hub = FakeHub()
    sources = _sources(client, hub=hub)
    sources.load_config().hub.base_url = "http://hub.example.test:8080"
    generated: list[bool] = []
    sources = replace(
        sources,
        token_factory=lambda: generated.append(True) or "x" * 24,
    )

    refused = _bridge(sources).execute("/deploy chain-1 --channel latest")

    assert refused.status == 409
    assert refused.payload["error"]["code"] == "hub_endpoint_insecure"
    assert generated == []
    assert hub.specs == []


def test_deploy_exact_id_transport_failure_never_falls_back_to_fuzzy_name() -> None:
    class BrokenClient(FakeClient):
        def get_chain_record(self, entity_id: str, *, channel: str = "latest") -> Any:
            del entity_id, channel
            raise RuntimeError("temporary transport failure")

    client = BrokenClient()
    hub = FakeHub()
    result = _bridge(_sources(client, hub=hub)).execute(
        "/deploy chain-1 --channel latest"
    )

    assert result.status == 502
    assert result.payload["error"]["code"] == "domain_operation_failed"
    assert hub.specs == []


def test_deploy_name_fallback_requires_an_exact_unique_name() -> None:
    client = FakeClient()
    hub = FakeHub()
    result = _bridge(_sources(client, hub=hub)).execute(
        "/deploy Research --channel latest"
    )

    assert result.status == 404
    assert result.payload["error"]["code"] == "chain_not_found"
    assert hub.specs == []


def test_promote_gate_runs_only_after_provider_confirmation() -> None:
    client = FakeClient()
    gate_calls: list[bool] = []

    def gate(*_args: Any, **_kwargs: Any) -> Any:
        gate_calls.append(True)
        return SimpleNamespace(
            ok=True,
            checks=(
                SimpleNamespace(
                    name="baseline run", passed=True, skipped=False, detail="recorded"
                ),
            ),
        )

    bridge = _bridge(_sources(client, promotion_gate=gate))
    command = "/promote chain-1"
    first, token = _confirm(bridge, command)
    assert (
        first.payload["confirmation"]["risk"]
        == SecondDomainRisk.PROVIDER_EXECUTION.value
    )
    assert gate_calls == []
    assert client.promotions == []

    done = bridge.execute(command, confirmation_token=token)
    assert done.payload["ok"] is True
    assert gate_calls == [True]
    assert client.promotions == [("chain-1", "latest", "stable")]


def test_promote_force_skips_gate_but_still_requires_confirmation() -> None:
    client = FakeClient()

    def forbidden(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("force must skip the gate")

    bridge = _bridge(_sources(client, promotion_gate=forbidden))
    command = "/promote chain-1 --force"
    first, token = _confirm(bridge, command)
    assert (
        first.payload["confirmation"]["risk"]
        == SecondDomainRisk.EXTERNAL_SIDE_EFFECT.value
    )
    done = bridge.execute(command, confirmation_token=token)
    assert done.payload["ok"] is True
    assert client.promotions == [("chain-1", "latest", "stable")]


def test_promote_failed_gate_records_no_channel_mutation() -> None:
    client = FakeClient()
    bridge = _bridge(
        _sources(
            client,
            promotion_gate=lambda *_args, **_kwargs: SimpleNamespace(
                ok=False,
                checks=(
                    SimpleNamespace(
                        name="baseline run",
                        passed=False,
                        skipped=False,
                        detail="failed",
                    ),
                ),
            ),
        )
    )
    command = "/promote chain-1"
    _first, token = _confirm(bridge, command)
    refused = bridge.execute(command, confirmation_token=token)
    assert refused.payload["ok"] is False
    assert refused.payload["payload"]["promoted"] is False
    assert client.promotions == []


def test_promote_target_transport_failure_aborts_before_confirmation() -> None:
    class BrokenTargetClient(FakeClient):
        def get_chain_record(self, entity_id: str, *, channel: str = "latest") -> Any:
            if channel == "stable":
                raise RuntimeError("target service unavailable")
            return super().get_chain_record(entity_id, channel=channel)

    client = BrokenTargetClient()
    result = _bridge(_sources(client)).execute("/promote chain-1 --force")

    assert result.status == 502
    assert result.payload["error"]["code"] == "domain_operation_failed"
    assert client.promotions == []


def test_promote_target_change_invalidates_confirmation() -> None:
    client = FakeClient()
    bridge = _bridge(_sources(client))
    command = "/promote chain-1 --force"
    _first, token = _confirm(bridge, command)
    client.target.version_id = "stable-2"

    refused = bridge.execute(command, confirmation_token=token)

    assert refused.status == 409
    assert refused.payload["error"]["code"] == "confirmation_invalid"
    assert client.promotions == []


def test_remember_planning_does_not_create_ltm_directory(tmp_path: Path) -> None:
    ltm_dir = tmp_path / "not-created-by-plan"
    config = SimpleNamespace(
        context=SimpleNamespace(
            ltm_enabled=True,
            ltm_session_id="audit",
            ltm_dir=str(ltm_dir),
        ),
        mage=SimpleNamespace(api_key="", base_url=""),
    )
    sources = replace(
        DesktopSecondDomainSources(),
        load_config=lambda: config,
    )

    plan = DesktopSecondDomainCommands(sources).plan("remember", ["safe", "note"])

    assert plan.preview["action"] == "remember"
    assert not ltm_dir.exists()


def test_default_run_history_projection_redacts_and_bounds_runtime_data(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "SYNTHETIC_HISTORY_SECRET"
    recorded: list[dict[str, Any]] = []

    async def failed_executor(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError(f"api_key={secret}")

    monkeypatch.setattr(
        care_cli,
        "_BUILD_CARL_EXECUTOR_OVERRIDE",
        lambda: failed_executor,
    )
    monkeypatch.setattr(
        care_cli,
        "_record_cli_run",
        lambda **kwargs: recorded.append(kwargs),
    )
    config = SimpleNamespace()
    with pytest.raises(RuntimeError):
        second_domain._default_run_chain(
            config,
            SimpleNamespace(),
            "chain-1",
            CHAIN,
            task=f"api_key={secret}",
            inputs={},
            save_result=None,
        )

    assert len(recorded) == 1
    assert secret not in repr(recorded[0])
    assert "[redacted]" in recorded[0]["error"]

    async def successful_executor(*_args: Any, **_kwargs: Any) -> Any:
        return SimpleNamespace(
            success=True,
            final_answer=f"token={secret}" + "z" * 10_000,
            step_results=[{"api_key": secret}],
            token_usage={"prompt": 3, "completion": 4},
        )

    recorded.clear()
    monkeypatch.setattr(
        care_cli,
        "_BUILD_CARL_EXECUTOR_OVERRIDE",
        lambda: successful_executor,
    )
    second_domain._default_run_chain(
        config,
        SimpleNamespace(),
        "chain-1",
        CHAIN,
        task="safe",
        inputs={},
        save_result=None,
    )
    projection = recorded[0]["result"].to_dict()
    assert secret not in repr(projection)
    assert len(projection["final_answer"]) <= 2_000
    assert projection["steps_completed"] == 1
    assert projection["token_usage"] == {"prompt": 3, "completion": 4}


def test_default_run_uses_confirmed_config_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = SimpleNamespace(marker="confirmed")
    captured: list[Any] = []
    recorded: list[dict[str, Any]] = []

    async def executor(*_args: Any, **_kwargs: Any) -> Any:
        return SimpleNamespace(
            success=True,
            final_answer="safe",
            step_results=[],
            token_usage={},
        )

    monkeypatch.setattr(care_cli, "_BUILD_CARL_EXECUTOR_OVERRIDE", None)
    monkeypatch.setattr(
        second_domain,
        "_build_carl_executor_from_config",
        lambda supplied: captured.append(supplied) or executor,
    )
    monkeypatch.setattr(
        care_cli,
        "_record_cli_run",
        lambda **kwargs: recorded.append(kwargs),
    )

    result = second_domain._default_run_chain(
        config,
        SimpleNamespace(),
        "chain-1",
        CHAIN,
        task=None,
        inputs={},
        save_result=None,
    )

    assert result["success"] is True
    assert captured == [config]
    assert len(recorded) == 1


def test_registry_reports_forms_and_only_five_unavailable_slashes() -> None:
    registry = CommandBridge().registry()
    by_name = {entry["name"]: entry for entry in registry["slash_commands"]}
    assert registry["counts"]["slash_commands"] == 44
    assert registry["counts"]["slash_domain"] >= 11
    assert registry["counts"]["slash_unavailable"] <= 5
    assert by_name["library"]["parity"] == "adapted"
    assert by_name["deploy"]["parity"] == "exact"
    assert (
        "install <entity-id> [--name NAME]" in by_name["marketplace"]["supported_forms"]
    )
    assert by_name["run"]["confirmation"] == "conditional"
