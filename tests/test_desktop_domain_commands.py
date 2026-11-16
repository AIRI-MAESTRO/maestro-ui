"""Hermetic parity tests for native desktop domain slash commands."""

from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

import pytest

from care.desktop_domain_commands import (
    DesktopDomainCommands,
    DesktopDomainSources,
    DomainCommandError,
    DomainRisk,
)


def _version(number: int) -> SimpleNamespace:
    return SimpleNamespace(
        version_number=number,
        version_id=f"vid-{number:04d}",
        created_at=f"2026-08-{number:02d}T12:00:00Z",
        evolution_meta={"fitness_score": number / 10},
        change_summary=f"change {number}",
    )


def _record(
    entity_id: str = "chain-1",
    *,
    channel: str = "latest",
    version: int = 3,
) -> SimpleNamespace:
    return SimpleNamespace(
        entity_id=entity_id,
        version_id=f"vid-{version:04d}",
        version_number=version,
        channel=channel,
        meta={"display_name": "Weather Agent"},
        content={"name": "Weather"},
    )


class FakeClient:
    def __init__(self) -> None:
        self.versions = [_version(1), _version(2), _version(3)]
        self.names: dict[str, list[Any]] = {
            "Weather Agent": [
                SimpleNamespace(entity_id="chain-1", display_name="Weather Agent")
            ]
        }
        self.diff_patch: Any = {
            "op": "replace",
            "path": "/steps/0/aim",
            "value": "api_key=super-secret",
        }
        self.pinned: list[tuple[str, str, str]] = []
        self.deleted: list[tuple[str, str]] = []

    def get_chain_record(self, entity_id: str, *, channel: str = "latest") -> Any:
        if entity_id != "chain-1":
            raise KeyError(entity_id)
        return _record(channel=channel, version=3 if channel == "latest" else 2)

    def list_chains(self, *, limit: int = 5, q: str = "") -> list[Any]:
        return list(self.names.get(q, ()))[:limit]

    def list_versions(
        self,
        entity_id: str,
        *,
        entity_type: str = "chain",
        limit: int = 20,
    ) -> list[Any]:
        assert entity_id == "chain-1"
        assert entity_type == "chain"
        return list(self.versions)[:limit]

    def diff_versions(self, entity_id: str, left: str, right: str) -> Any:
        assert (entity_id, left, right) == ("chain-1", "vid-0001", "vid-0003")
        return SimpleNamespace(patch=self.diff_patch)

    def pin_channel(self, entity_id: str, channel: str, version_id: str) -> None:
        self.pinned.append((entity_id, channel, version_id))

    def _delete_entity(self, entity_type: str, entity_id: str) -> None:
        self.deleted.append((entity_type, entity_id))


class FakeMemory:
    def __init__(self, client: FakeClient | None = None) -> None:
        self.client = client or FakeClient()
        self.chain_exists = True
        self.list_error: Exception | None = None
        self.cards = [
            {
                "entity_id": "card-1",
                "meta": {"tags": ["dataset-entry:chain-1"]},
                "content": {"task": "one"},
            },
            {
                "entity_id": "card-unrelated",
                "meta": {"tags": ["dataset-entry:other"]},
                "content": {"task": "two"},
            },
            {
                "entity_id": "card-2",
                "tags": ["dataset-entry:chain-1"],
                "content": {"task": "three"},
            },
        ]

    def get_chain(self, entity_id: str) -> Any:
        assert entity_id == "chain-1"
        return {"steps": []} if self.chain_exists else None

    def list_entities(self, *, entity_type: str, limit: int) -> list[dict[str, Any]]:
        assert entity_type == "memory_card"
        assert limit == 500
        if self.list_error:
            raise self.list_error
        return list(self.cards)


class FakeLTM:
    def __init__(self, values: dict[str, str] | None = None) -> None:
        self.values = dict(values or {})
        self.deleted: list[tuple[str, str]] = []

    def retrieve(self, key: str, *, session_id: str) -> str | None:
        return self.values.get(key)

    def delete(self, key: str, *, session_id: str) -> bool:
        self.deleted.append((key, session_id))
        return self.values.pop(key, None) is not None


@dataclass
class FakeDeployment:
    name: str = "weather"
    display_name: str = "Weather"
    version: str = "v2"
    ready: bool = True
    ready_reason: str = ""
    entity_id: str | None = "chain-1"
    channel: str | None = "stable"
    chain_file: str | None = "/private/deploy/secret.json"
    source: str = "memory"
    deployed_at: str = "2026-08-31T12:00:00Z"
    runs: int = 4


class FakeHub:
    def __init__(self) -> None:
        self.healthy = True
        self.deployments = [FakeDeployment()]
        self.calls: list[tuple[str, str | None]] = []
        self.reload_result = (True, FakeDeployment(version="v3"))

    async def health(self) -> dict[str, Any] | None:
        self.calls.append(("health", None))
        return {"status": "ok"} if self.healthy else None

    async def list_deployments(self) -> list[FakeDeployment]:
        self.calls.append(("list", None))
        return list(self.deployments)

    async def get_deployment(self, name: str) -> FakeDeployment:
        self.calls.append(("get", name))
        return self.deployments[0]

    async def undeploy(self, name: str) -> None:
        self.calls.append(("undeploy", name))

    async def reload(self, name: str) -> tuple[bool, FakeDeployment]:
        self.calls.append(("reload", name))
        return self.reload_result

    def agent_url(self, name: str) -> str:
        return f"http://user:password@127.0.0.1:8080/agents/{name}?token=secret"

    def docs_url(self, name: str) -> str:
        return self.agent_url(name).split("?", 1)[0] + "/docs?api_key=secret"


def _service(
    *,
    memory: FakeMemory | None = None,
    ltm: FakeLTM | None = None,
    hub: FakeHub | None = None,
    context: str = "",
) -> DesktopDomainCommands:
    fake_memory = memory or FakeMemory()
    fake_ltm = ltm
    fake_hub = hub or FakeHub()
    config = SimpleNamespace(
        hub=SimpleNamespace(base_url="http://127.0.0.1:8080", timeout=3.0),
        context=SimpleNamespace(ltm_enabled=fake_ltm is not None),
    )
    return DesktopDomainCommands(
        DesktopDomainSources(
            load_config=lambda: config,
            build_memory=lambda _config: fake_memory,
            build_ltm=lambda _config: fake_ltm,
            load_context=lambda _config: context,
            recall_ltm=lambda store, session_id, *, max_chars: "\n".join(
                f"- {key}: {value}" for key, value in store.values.items()
            )[:max_chars],
            ltm_session_id=lambda _config: "desktop-test",
            build_hub=lambda _config: fake_hub,
        )
    )


def test_versions_lists_exact_tui_projection_and_resolves_unique_name() -> None:
    plan = _service().plan("versions", ["Weather", "Agent"])

    assert plan.risk is DomainRisk.READ_ONLY
    result = plan.execute()
    assert result.ok is True
    assert [row["version_number"] for row in result.payload["versions"]] == [3, 2, 1]
    assert result.payload["versions"][0]["channels"] == ["latest"]
    assert result.payload["versions"][1]["channels"] == ["stable"]
    assert "Weather Agent" in result.stdout


def test_versions_ambiguous_name_fails_closed() -> None:
    memory = FakeMemory()
    memory.client.names["weather"] = [
        SimpleNamespace(entity_id="chain-1", display_name="Weather One"),
        SimpleNamespace(entity_id="chain-2", display_name="Weather Two"),
    ]

    with pytest.raises(DomainCommandError) as caught:
        _service(memory=memory).plan("versions", ["weather"])

    assert caught.value.code == "chain_reference_ambiguous"
    assert caught.value.status == 409


def test_versions_diff_is_bounded_and_secret_redacted_but_keeps_json_pointer() -> None:
    result = _service().plan(
        "versions",
        ["chain-1", "diff", "vid-0001", "vid-0003"],
    ).execute()

    rendered = repr(result)
    assert "super-secret" not in rendered
    assert "[redacted]" in rendered
    assert result.payload["patch"]["path"] == "/steps/0/aim"


def test_memory_show_redacts_secrets_and_local_paths() -> None:
    context = (
        "## Global user context (/Users/alice/.config/care/CARE.md)\n"
        "api_key=top-secret\nworkspace=/private/tmp/project"
    )
    ltm = FakeLTM({"preferred_model": "local", "token": "hidden-token"})

    result = _service(ltm=ltm, context=context).plan("memory", ["show"]).execute()

    rendered = repr(result)
    assert "/Users/alice" not in rendered
    assert "/private/tmp" not in rendered
    assert "top-secret" not in rendered
    assert "hidden-token" not in rendered
    assert "[redacted]" in rendered
    assert result.payload["editable"] is False


def test_memory_edit_is_explicitly_unavailable() -> None:
    with pytest.raises(DomainCommandError) as caught:
        _service().plan("memory", ["edit"])

    assert caught.value.code == "slash_action_unsupported"
    assert caught.value.status == 501


def test_memory_forget_is_confirmable_and_does_not_mutate_during_planning() -> None:
    ltm = FakeLTM({"preferred_language": "Russian"})
    service = _service(ltm=ltm)

    plan = service.plan("memory", ["forget", "preferred_language"])
    assert plan.risk is DomainRisk.FILESYSTEM_MUTATION
    assert plan.preview == {
        "action": "forget",
        "key": "preferred_language",
        "present": True,
    }
    assert plan.confirmation_scope
    assert ltm.deleted == []

    result = plan.execute()
    assert result.payload["removed"] is True
    assert ltm.deleted == [("preferred_language", "desktop-test")]


def test_memory_forget_scope_changes_when_the_value_changes() -> None:
    ltm = FakeLTM({"role": "researcher"})
    service = _service(ltm=ltm)
    first = service.plan("memory", ["forget", "role"])
    ltm.values["role"] = "owner"
    second = service.plan("memory", ["forget", "role"])

    assert first.confirmation_scope != second.confirmation_scope


def test_rollback_resolves_previous_version_and_only_mutates_on_execute() -> None:
    memory = FakeMemory()
    plan = _service(memory=memory).plan("rollback", ["chain-1"])

    assert plan.risk is DomainRisk.FILESYSTEM_MUTATION
    assert plan.preview["from_label"] == "v2"  # stable head in the fake
    assert plan.preview["to_label"] == "v1"
    assert memory.client.pinned == []

    result = plan.execute()
    assert result.ok is True
    assert memory.client.pinned == [("chain-1", "stable", "vid-0001")]


def test_rollback_supports_unique_name_and_explicit_target() -> None:
    memory = FakeMemory()
    plan = _service(memory=memory).plan(
        "rollback",
        ["Weather", "Agent", "--channel", "latest", "--to=vid-0001"],
    )
    assert plan.preview["entity_id"] == "chain-1"
    assert plan.preview["to_version"] == "vid-0001"
    plan.execute()
    assert memory.client.pinned == [("chain-1", "latest", "vid-0001")]


def test_forget_preview_is_read_only_and_force_deletes_chain_plus_linked_cards() -> None:
    memory = FakeMemory()
    service = _service(memory=memory)

    preview = service.plan("forget", ["chain-1"])
    assert preview.risk is DomainRisk.READ_ONLY
    preview_result = preview.execute()
    assert preview_result.payload["dataset_cards"] == ["card-1", "card-2"]
    assert memory.client.deleted == []

    force = service.plan("forget", ["chain-1", "--force"])
    assert force.risk is DomainRisk.FILESYSTEM_MUTATION
    assert force.preview["dataset_count"] == 2
    assert memory.client.deleted == []
    result = force.execute()

    assert result.ok is True
    assert memory.client.deleted == [
        ("chain", "chain-1"),
        ("memory_card", "card-1"),
        ("memory_card", "card-2"),
    ]
    assert ("memory_card", "card-unrelated") not in memory.client.deleted


def test_forget_fails_closed_when_linked_dataset_cannot_be_enumerated() -> None:
    memory = FakeMemory()
    memory.list_error = RuntimeError("/private/config api_key=do-not-leak")

    with pytest.raises(DomainCommandError) as caught:
        _service(memory=memory).plan("forget", ["chain-1", "--force"])

    assert caught.value.code == "domain_operation_failed"
    assert "/private" not in str(caught.value)
    assert "do-not-leak" not in str(caught.value)
    assert memory.client.deleted == []


def test_deployments_list_and_docs_return_safe_urls_without_local_paths() -> None:
    hub = FakeHub()
    service = _service(hub=hub)

    listed = service.plan("deployments", []).execute()
    row = listed.payload["deployments"][0]
    assert row["agent_url"] == "http://127.0.0.1:8080/agents/weather"
    assert row["docs_url"] == "http://127.0.0.1:8080/agents/weather/docs"
    rendered = repr(listed)
    assert "password" not in rendered
    assert "token=secret" not in rendered
    assert "/private/deploy" not in rendered

    docs = service.plan("deployments", ["docs", "weather"]).execute()
    assert docs.payload["opened"] is False
    assert docs.payload["url"].endswith("/agents/weather/docs")


def test_deployment_mutations_have_external_risk_and_exact_effects() -> None:
    hub = FakeHub()
    service = _service(hub=hub)

    undeploy = service.plan("deployments", ["undeploy", "weather"])
    assert undeploy.risk is DomainRisk.EXTERNAL_SIDE_EFFECT
    assert any("Undeploy" in effect for effect in undeploy.effects)
    assert ("undeploy", "weather") not in hub.calls
    undeploy.execute()
    assert ("undeploy", "weather") in hub.calls

    reload_plan = service.plan("deployments", ["reload", "weather"])
    assert ("reload", "weather") not in hub.calls
    result = reload_plan.execute()
    assert result.payload["reloaded"] is True
    assert result.payload["deployment"]["version"] == "v3"


@pytest.mark.parametrize(
    ("name", "args"),
    [
        ("versions", []),
        ("versions", ["chain-1", "diff", "only-one"]),
        ("memory", ["show", "extra"]),
        ("memory", ["forget"]),
        ("rollback", ["chain-1", "--unknown", "x"]),
        ("forget", ["chain-1", "other"]),
        ("forget", ["chain-1", "--force", "--force"]),
        ("deployments", ["undeploy"]),
        ("deployments", ["reload", "../../bad"]),
        ("deployments", ["docs", "weather", "extra"]),
    ],
)
def test_unsupported_or_ambiguous_forms_fail_before_domain_calls(
    name: str,
    args: list[str],
) -> None:
    with pytest.raises(DomainCommandError) as caught:
        _service().plan(name, args)

    assert caught.value.code in {
        "slash_arguments_invalid",
        "slash_action_unsupported",
    }
