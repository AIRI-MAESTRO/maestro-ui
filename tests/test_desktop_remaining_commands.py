"""Hermetic tests for the native desktop's remaining slash adapters."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from care.desktop_domain_commands import DomainCommandError, DomainRisk
from care.desktop_remaining_commands import (
    DesktopRemainingCommands,
    DesktopRemainingSources,
)


class FakeMemory:
    def __init__(self) -> None:
        self.chain: dict[str, Any] = {
            "name": "Weather chain",
            "version": "1",
            "steps": [
                {
                    "number": 1,
                    "title": "Forecast",
                    "step_type": "llm",
                    "aim": "Summarise weather",
                }
            ],
        }
        self.saved: list[dict[str, Any]] = []

    def get_chain(self, chain_id: str, *, channel: str = "latest") -> Any:
        assert chain_id == "chain-1"
        assert channel == "latest"
        return json.loads(json.dumps(self.chain))

    def save_chain(self, chain: Any, **kwargs: Any) -> str:
        self.saved.append({"chain": chain, **kwargs})
        return "chain-1"


class FakePlatform:
    def __init__(self) -> None:
        self.runs: list[dict[str, Any]] = [
            {"id": "exp-1", "status": "running", "api_key": "server-secret"}
        ]
        self.state: dict[str, Any] = {
            "id": "exp-1",
            "status": "completed",
            "best_fitness": 0.9,
        }
        self.events: list[dict[str, Any]] = [
            {"event": "generation_completed", "data": {"generation": 1}},
            {"event": "completed", "data": {"token": "hidden"}},
            {"event": "heartbeat", "data": {}},
        ]
        self.start_calls: list[dict[str, Any]] = []
        self.accept_calls: list[tuple[str, str, Any]] = []

    def list_evolutions(self, *, limit: int = 100) -> dict[str, Any]:
        return {"items": list(self.runs[:limit])}

    def get_evolution(self, run_id: str) -> dict[str, Any]:
        assert run_id == "exp-1"
        return json.loads(json.dumps(self.state))

    def stream_events(self, run_id: str):
        assert run_id == "exp-1"
        yield from list(self.events)

    def start_evolution(self, **kwargs: Any) -> Any:
        self.start_calls.append(kwargs)
        return SimpleNamespace(evolution_id="exp-new", status="queued")

    def accept_individual(
        self,
        run_id: str,
        individual_id: str,
        *,
        memory: Any = None,
    ) -> dict[str, Any]:
        self.accept_calls.append((run_id, individual_id, memory))
        return {"channel": "stable", "version_id": "version-2"}


@dataclass
class FakeEdit:
    op: str = "replace"
    target_step_number: int = 1
    rationale: str = "Make the answer concise"


@dataclass
class FakeEditResult:
    before_chain_dict: dict[str, Any]
    chain_dict: dict[str, Any]
    summary: str = "Tightened the answer"
    edits: tuple[FakeEdit, ...] = (FakeEdit(),)
    needs_disambiguation: bool = False


class Harness:
    def __init__(self, workspace: Path) -> None:
        self.workspace = workspace
        self.memory = FakeMemory()
        self.platform = FakePlatform()
        self.upload_calls: list[dict[str, Any]] = []
        self.preview_calls: list[dict[str, Any]] = []
        self.clock = 100.0
        self.identifier = 0
        self.config = SimpleNamespace(
            upload=SimpleNamespace(
                url="https://upload.example.test/v1/chains",
                api_key="upload-secret-sentinel",
                auth_header="Authorization",
                timeout=12.0,
            ),
            mage=SimpleNamespace(
                base_url="https://provider.example.test/v1",
                model="model-one",
                api_key="provider-secret-sentinel",
            ),
            memory=SimpleNamespace(
                base_url="https://memory.example.test",
                api_key="memory-secret-sentinel",
            ),
            platform=SimpleNamespace(
                base_url="https://platform.example.test",
                api_key="platform-secret-sentinel",
                mutation_model="mutator",
                validation_model="judge",
                mutation_api_key="mutation-secret-sentinel",
                validation_api_key="validation-secret-sentinel",
            ),
        )

    def upload(self, **kwargs: Any) -> tuple[int, str]:
        self.upload_calls.append(kwargs)
        return 201, "created; api_key=response-secret"

    def preview(self, _config: Any, **kwargs: Any) -> FakeEditResult:
        self.preview_calls.append(kwargs)
        before = json.loads(json.dumps(kwargs["chain"]))
        after = json.loads(json.dumps(before))
        after["steps"][0]["aim"] = "Give a concise forecast"
        return FakeEditResult(before_chain_dict=before, chain_dict=after)

    def next_id(self) -> str:
        self.identifier += 1
        return f"draft-id-{self.identifier:04d}"

    def service(self) -> DesktopRemainingCommands:
        return DesktopRemainingCommands(
            DesktopRemainingSources(
                load_config=lambda: self.config,
                build_memory=lambda _config: self.memory,
                build_platform=lambda _config: self.platform,
                preview_revision=self.preview,
                upload_transport=self.upload,
                workspace_root=lambda: self.workspace,
                export_root=lambda: self.workspace / "exports",
                image_max_bytes=lambda: 1024 * 1024,
                now=lambda: self.clock,
                new_id=self.next_id,
            )
        )


def _transcript() -> list[dict[str, Any]]:
    return [
        {
            "role": "user",
            "text": "Hello <script>alert(1)</script>",
            "timestamp": "2026-08-31T12:00:00Z",
            "mode": "fast",
        },
        {
            "role": "assistant",
            "text": "api_key=transcript-secret\nDone",
            "timestamp": "2026-08-31T12:00:01Z",
            "reaction": "up",
            "provenance": {"token": "provenance-secret"},
        },
    ]


def _write_png(path: Path) -> None:
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"safe-payload")


def test_export_is_confirmed_atomic_workspace_write_and_redacts(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    service = harness.service()

    plan = service.plan(
        "export",
        ["json", "session.json"],
        transcript=_transcript(),
    )

    assert plan.risk is DomainRisk.FILESYSTEM_MUTATION
    assert plan.confirmation_scope
    assert plan.preview["overwrites_existing"] is False
    assert not (tmp_path / "exports" / "session.json").exists()

    result = plan.execute()
    output = tmp_path / "exports" / "session.json"
    assert output.is_file()
    rows = json.loads(output.read_text())
    assert rows[0]["text"] == "Hello <script>alert(1)</script>"
    assert "transcript-secret" not in output.read_text()
    assert "provenance-secret" not in output.read_text()
    assert rows[1]["provenance"]["token"] == "[redacted]"
    assert result.payload["sha256"]


def test_export_html_escapes_user_content_and_default_name_is_deterministic(
    tmp_path: Path,
) -> None:
    service = Harness(tmp_path).service()
    first = service.plan("export", ["html"], transcript=_transcript())
    second = service.plan("export", ["html"], transcript=_transcript())

    assert first.confirmation_scope == second.confirmation_scope
    first.execute()
    body = (tmp_path / "exports" / "maestro-transcript.html").read_text()
    assert "<script>alert(1)</script>" not in body
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in body
    assert "<meta charset=\"utf-8\">" in body


@pytest.mark.parametrize(
    "args",
    [
        ["txt", "out.txt"],
        ["json", "../out.json"],
        ["json", "/tmp/out.json"],
        ["json", "out.md"],
        ["json", "folder/out.json"],
    ],
)
def test_export_rejects_unsupported_or_escaping_targets(
    tmp_path: Path, args: list[str]
) -> None:
    with pytest.raises(DomainCommandError):
        Harness(tmp_path).service().plan("export", args, transcript=[])


def test_export_scope_and_execute_fail_when_existing_target_changes(
    tmp_path: Path,
) -> None:
    exports = tmp_path / "exports"
    exports.mkdir()
    target = exports / "session.md"
    target.write_text("old")
    service = Harness(tmp_path).service()
    first = service.plan("export", ["md", "session.md"], transcript=[])
    target.write_text("changed")
    second = service.plan("export", ["md", "session.md"], transcript=[])

    assert first.confirmation_scope != second.confirmation_scope
    with pytest.raises(DomainCommandError) as caught:
        first.execute()
    assert caught.value.code == "confirmation_state_changed"
    assert target.read_text() == "changed"


def test_export_rejects_symlink_target(tmp_path: Path) -> None:
    exports = tmp_path / "exports"
    exports.mkdir()
    outside = tmp_path.parent / "outside-export.txt"
    outside.write_text("do-not-touch")
    (exports / "session.md").symlink_to(outside)

    with pytest.raises(DomainCommandError) as caught:
        Harness(tmp_path).service().plan(
            "export", ["md", "session.md"], transcript=[]
        )
    assert caught.value.code == "unsafe_export_target"
    assert outside.read_text() == "do-not-touch"


def test_imgpreview_status_and_route_ready_descriptor(tmp_path: Path) -> None:
    image = tmp_path / "fixtures" / "maestric.png"
    image.parent.mkdir()
    _write_png(image)
    service = Harness(tmp_path).service()

    status = service.plan("imgpreview", ["status"]).execute()
    assert status.payload["native_preview"] is True
    assert status.payload["terminal_protocol_required"] is False

    plan = service.plan("imgpreview", ["fixtures/maestric.png"])
    assert plan.risk is DomainRisk.READ_ONLY
    result = plan.execute()
    assert result.payload["relative_path"] == "fixtures/maestric.png"
    assert result.payload["mime_type"] == "image/png"
    assert result.payload["route"].startswith(
        "/api/workspace-images/fixtures/maestric.png?sha256="
    )
    assert result.payload["route"].endswith(result.payload["sha256"])
    assert str(tmp_path) not in repr(result)


def test_imgpreview_rejects_outside_or_disguised_file(tmp_path: Path) -> None:
    outside = tmp_path.parent / "outside-image.png"
    _write_png(outside)
    disguised = tmp_path / "fake.png"
    disguised.write_text("not an image")
    service = Harness(tmp_path).service()

    with pytest.raises(DomainCommandError) as caught:
        service.plan("imgpreview", [str(outside)])
    assert caught.value.code == "unsafe_workspace_path"

    with pytest.raises(DomainCommandError) as caught:
        service.plan("imgpreview", ["fake.png"])
    assert caught.value.code == "image_content_invalid"


def test_upload_uses_exact_tui_payload_without_exposing_key(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    plan = harness.service().plan("upload", ["chain-1"])

    assert plan.risk is DomainRisk.EXTERNAL_SIDE_EFFECT
    assert harness.upload_calls == []
    assert "upload-secret-sentinel" not in repr(plan)
    result = plan.execute()

    assert len(harness.upload_calls) == 1
    call = harness.upload_calls[0]
    assert call["body"] == {"chain_id": "chain-1", "chain": harness.memory.chain}
    assert call["api_key"] == "upload-secret-sentinel"
    assert result.payload["status"] == 201
    assert "response-secret" not in repr(result)
    assert "[redacted]" in result.payload["response_preview"]


@pytest.mark.parametrize(
    "url",
    [
        "http://upload.example.test/v1",
        "http://localhost.example.test/v1",
        "https://user:password@example.test/v1",
        "https://example.test/v1?token=secret",
    ],
)
def test_upload_rejects_unsafe_endpoint(tmp_path: Path, url: str) -> None:
    harness = Harness(tmp_path)
    harness.config.upload.url = url

    with pytest.raises(DomainCommandError) as caught:
        harness.service().plan("upload", ["chain-1"])
    assert caught.value.code in {"external_url_invalid", "external_url_insecure"}


def test_upload_allows_loopback_http_and_rechecks_chain(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    harness.config.upload.url = "http://127.0.0.1:8765/upload"
    service = harness.service()
    first = service.plan("upload", ["chain-1"])
    harness.memory.chain["steps"][0]["aim"] = "changed"
    second = service.plan("upload", ["chain-1"])

    assert first.confirmation_scope != second.confirmation_scope
    with pytest.raises(DomainCommandError) as caught:
        first.execute()
    assert caught.value.code == "confirmation_state_changed"
    assert harness.upload_calls == []


@pytest.mark.parametrize(
    ("command", "section"),
    [
        (("upload", ["chain-1"]), "memory"),
        (("revise", ["chain-1", "make concise"]), "memory"),
        (("revise", ["chain-1", "make concise"]), "mage"),
        (("evolution", ["launch", "chain-1"]), "memory"),
        (("evolution", ["launch", "chain-1"]), "platform"),
    ],
)
def test_all_external_service_urls_require_https_or_loopback(
    tmp_path: Path,
    command: tuple[str, list[str]],
    section: str,
) -> None:
    harness = Harness(tmp_path)
    getattr(harness.config, section).base_url = "http://remote.example.test/v1"

    with pytest.raises(DomainCommandError) as caught:
        harness.service().plan(command[0], command[1])
    assert caught.value.code == "external_url_insecure"


@pytest.mark.parametrize(
    ("name", "args"),
    [
        ("upload", ["chain-1"]),
        ("revise", ["chain-1", "make concise"]),
        ("evolution", ["launch", "chain-1"]),
    ],
)
def test_external_chain_actions_reject_embedded_credentials(
    tmp_path: Path, name: str, args: list[str]
) -> None:
    harness = Harness(tmp_path)
    harness.memory.chain["metadata"] = {"api_key": "embedded-secret"}

    with pytest.raises(DomainCommandError) as caught:
        harness.service().plan(name, args)
    assert caught.value.code == "chain_contains_sensitive_data"
    assert "embedded-secret" not in str(caught.value)
    assert harness.upload_calls == []
    assert harness.platform.start_calls == []
    assert harness.preview_calls == []


def test_evolution_intro_list_snapshot_and_watch_are_bounded_and_redacted(
    tmp_path: Path,
) -> None:
    harness = Harness(tmp_path)
    service = harness.service()

    intro = service.plan("evolution", []).execute()
    assert "setup" in intro.payload["forms"]

    listed = service.plan("evolution", ["list"]).execute()
    assert listed.payload["runs"][0]["api_key"] == "[redacted]"
    assert "server-secret" not in repr(listed)

    snapshot = service.plan("evolution", ["exp-1"]).execute()
    assert snapshot.payload["state"]["status"] == "completed"

    watched = service.plan(
        "evolution", ["watch", "exp-1", "--limit", "2"]
    ).execute()
    assert watched.payload["count"] == 2
    assert watched.payload["terminal"] is True
    assert watched.payload["streaming"] is False
    assert watched.payload["events"][1]["data"]["token"] == "[redacted]"


def test_evolution_setup_returns_native_form_contract(tmp_path: Path) -> None:
    result = Harness(tmp_path).service().plan(
        "evolution", ["setup", "chain-1"]
    ).execute()

    assert result.payload["selected_chain"]["chain_id"] == "chain-1"
    assert result.payload["selected_chain"]["steps"] == 1
    assert result.payload["defaults"]["population"] == 8
    assert "/evolution launch" in result.payload["launch_usage"]


def test_evolution_launch_maps_form_to_existing_platform_service(tmp_path: Path) -> None:
    data = tmp_path / "eval.jsonl"
    data.write_text('{"input":"x","expected":"y"}\n')
    harness = Harness(tmp_path)
    plan = harness.service().plan(
        "evolution",
        [
            "launch",
            "chain-1",
            "--mode",
            "per_step",
            "--iterations",
            "3",
            "--population",
            "4",
            "--criteria",
            "Prefer accurate answers",
            "--data",
            "eval.jsonl",
            "--target-column",
            "expected",
            "--objective",
            "quality",
            "--mutation-max-tokens",
            "4096",
        ],
    )

    assert plan.risk is DomainRisk.EXTERNAL_SIDE_EFFECT
    assert harness.platform.start_calls == []
    result = plan.execute()
    call = harness.platform.start_calls[0]
    assert call["base_chain_id"] == "chain-1"
    assert call["base_chain_content"] == harness.memory.chain
    assert call["evolution_mode"] == "per_step"
    assert call["max_iterations"] == 3
    assert call["population_size"] == 4
    assert call["test_data_path"] == str(data.resolve())
    assert call["objectives"] == ["quality"]
    assert call["mutation_max_tokens"] == 4096
    assert result.payload["run_id"] == "exp-new"


def test_evolution_launch_scope_tracks_dataset_and_chain(tmp_path: Path) -> None:
    data = tmp_path / "eval.csv"
    data.write_text("input,expected\nx,y\n")
    harness = Harness(tmp_path)
    service = harness.service()
    first = service.plan(
        "evolution", ["launch", "chain-1", "--data", "eval.csv"]
    )
    data.write_text("input,expected\nx,z\n")
    second = service.plan(
        "evolution", ["launch", "chain-1", "--data", "eval.csv"]
    )

    assert first.confirmation_scope != second.confirmation_scope
    with pytest.raises(DomainCommandError) as caught:
        first.execute()
    assert caught.value.code == "confirmation_state_changed"
    assert harness.platform.start_calls == []


def test_evolution_accept_rechecks_state_then_passes_memory(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    service = harness.service()
    plan = service.plan("evolution", ["accept", "exp-1", "winner-1"])

    assert plan.risk is DomainRisk.EXTERNAL_SIDE_EFFECT
    assert harness.platform.accept_calls == []
    result = plan.execute()
    assert harness.platform.accept_calls == [("exp-1", "winner-1", harness.memory)]
    assert result.payload["result"]["channel"] == "stable"

    stale = service.plan("evolution", ["accept", "exp-1", "winner-1"])
    harness.platform.state["best_fitness"] = 0.95
    with pytest.raises(DomainCommandError) as caught:
        stale.execute()
    assert caught.value.code == "confirmation_state_changed"


def test_revise_is_two_phase_preview_then_confirmed_one_use_save(
    tmp_path: Path,
) -> None:
    harness = Harness(tmp_path)
    service = harness.service()
    preview_plan = service.plan(
        "revise", ["chain-1", "make", "it", "concise", "--mode", "deep"]
    )

    assert preview_plan.risk is DomainRisk.EXTERNAL_SIDE_EFFECT
    assert harness.preview_calls == []
    assert harness.memory.saved == []
    assert "provider-secret-sentinel" not in repr(preview_plan)

    preview = preview_plan.execute()
    assert preview.payload["changed"] is True
    assert preview.payload["mode"] == "deep"
    assert harness.preview_calls[0]["instruction"] == "make it concise"
    assert harness.memory.saved == []
    draft_id = preview.payload["draft_id"]

    save_plan = service.plan("revise", ["save", draft_id])
    assert save_plan.risk is DomainRisk.FILESYSTEM_MUTATION
    assert harness.memory.saved == []
    saved = save_plan.execute()
    assert saved.payload["saved"] is True
    assert harness.memory.saved[0]["entity_id"] == "chain-1"
    assert harness.memory.saved[0]["channel"] == "latest"
    assert harness.memory.saved[0]["chain"]["steps"][0]["aim"] == (
        "Give a concise forecast"
    )

    with pytest.raises(DomainCommandError) as caught:
        service.plan("revise", ["save", draft_id])
    assert caught.value.code == "revision_draft_missing"


def test_revise_accepts_exact_bounded_memory_entity_id_with_spaces(
    tmp_path: Path,
) -> None:
    harness = Harness(tmp_path)
    entity_id = "agent with space"

    def get_chain(chain_id: str, *, channel: str = "latest") -> Any:
        assert chain_id == entity_id
        assert channel == "latest"
        return json.loads(json.dumps(harness.memory.chain))

    harness.memory.get_chain = get_chain  # type: ignore[method-assign]
    preview = harness.service().plan(
        "revise",
        [entity_id, "make concise"],
    ).execute()

    assert preview.payload["chain_id"] == entity_id
    assert harness.preview_calls[0]["chain_id"] == entity_id


def test_revise_save_fails_closed_if_base_chain_changed(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    service = harness.service()
    preview = service.plan(
        "revise", ["chain-1", "make", "it", "concise"]
    ).execute()
    harness.memory.chain["steps"][0]["aim"] = "changed elsewhere"

    with pytest.raises(DomainCommandError) as caught:
        service.plan("revise", ["save", preview.payload["draft_id"]])
    assert caught.value.code == "confirmation_state_changed"
    assert harness.memory.saved == []


def test_revise_preview_scope_tracks_provider_config_and_chain(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    service = harness.service()
    first = service.plan("revise", ["chain-1", "make concise"])
    harness.config.mage.model = "model-two"
    second = service.plan("revise", ["chain-1", "make concise"])

    assert first.confirmation_scope != second.confirmation_scope
    with pytest.raises(DomainCommandError) as caught:
        first.execute()
    assert caught.value.code == "confirmation_state_changed"
    assert harness.preview_calls == []


def test_revise_discard_is_confirmed_and_removes_draft(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    service = harness.service()
    preview = service.plan(
        "revise", ["chain-1", "make concise"]
    ).execute()
    draft_id = preview.payload["draft_id"]

    discarded = service.plan("revise", ["discard", draft_id])
    assert discarded.risk is DomainRisk.FILESYSTEM_MUTATION
    discarded.execute()
    assert harness.memory.saved == []
    with pytest.raises(DomainCommandError):
        service.plan("revise", ["save", draft_id])


def test_revision_draft_expires_without_save(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    service = DesktopRemainingCommands(
        DesktopRemainingSources(
            load_config=lambda: harness.config,
            build_memory=lambda _config: harness.memory,
            build_platform=lambda _config: harness.platform,
            preview_revision=harness.preview,
            upload_transport=harness.upload,
            workspace_root=lambda: tmp_path,
            export_root=lambda: tmp_path / "exports",
            image_max_bytes=lambda: 1024,
            now=lambda: harness.clock,
            new_id=harness.next_id,
        ),
        revision_draft_ttl=5.0,
    )
    preview = service.plan("revise", ["chain-1", "make concise"]).execute()
    harness.clock += 6.0

    with pytest.raises(DomainCommandError) as caught:
        service.plan("revise", ["save", preview.payload["draft_id"]])
    assert caught.value.code == "revision_draft_missing"


def test_source_exceptions_do_not_expose_credentials(tmp_path: Path) -> None:
    harness = Harness(tmp_path)

    def failed_upload(**_kwargs: Any) -> tuple[int, str]:
        raise RuntimeError("Authorization: Bearer leak-me api_key=also-leak")

    sources = harness.service()._sources  # noqa: SLF001 - test seam cloning
    service = DesktopRemainingCommands(
        DesktopRemainingSources(
            load_config=sources.load_config,
            build_memory=sources.build_memory,
            build_platform=sources.build_platform,
            preview_revision=sources.preview_revision,
            upload_transport=failed_upload,
            workspace_root=sources.workspace_root,
            export_root=sources.export_root,
            image_max_bytes=sources.image_max_bytes,
            now=sources.now,
            new_id=sources.new_id,
        )
    )
    plan = service.plan("upload", ["chain-1"])

    with pytest.raises(DomainCommandError) as caught:
        plan.execute()
    assert "leak-me" not in str(caught.value)
    assert "also-leak" not in str(caught.value)


@pytest.mark.parametrize(
    ("name", "args"),
    [
        ("upload", []),
        ("upload", ["../../bad"]),
        ("imgpreview", ["one", "two"]),
        ("evolution", ["watch"]),
        ("evolution", ["accept", "exp-1"]),
        ("evolution", ["launch"]),
        ("evolution", ["launch", "chain-1", "--iterations", "0"]),
        ("evolution", ["launch", "chain-1", "--population", "1"]),
        ("revise", []),
        ("revise", ["chain-1"]),
        ("revise", ["save", "bad"]),
    ],
)
def test_invalid_forms_fail_before_side_effects(
    tmp_path: Path, name: str, args: list[str]
) -> None:
    harness = Harness(tmp_path)
    with pytest.raises(DomainCommandError):
        harness.service().plan(name, args)
    assert harness.upload_calls == []
    assert harness.platform.start_calls == []
    assert harness.platform.accept_calls == []
    assert harness.preview_calls == []
    assert harness.memory.saved == []
