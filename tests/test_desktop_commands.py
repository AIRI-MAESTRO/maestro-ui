"""Safe in-process command parity for the Maestro desktop surface."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
from pathlib import Path
import shlex
from typing import Any

import pytest

from care.desktop_commands import (
    MAX_COMMAND_CHARS,
    MAX_COMMAND_OUTPUT_CHARS,
    CommandBridge,
    DesktopCommandSources,
)
from care.desktop_domain_commands import (
    DomainCommandError,
    DomainCommandPlan,
    DomainCommandResult,
    DomainRisk,
)
from care.desktop_tui_parity_commands import (
    DesktopTuiParityCommands,
    DesktopTuiParitySources,
    LocalVoiceBackend,
)
from care.runtime.local_run_history import LocalRunEntry
from care.runtime.profiles import ProfileInfo
from care.runtime.workspace_context import current_workspace_root
from care.sandbox.trust import SkillTrustStore


def _confirm(bridge: CommandBridge, command: str):
    challenge = bridge.execute(command)
    assert challenge.status == 409
    assert challenge.payload["error"]["code"] == "confirmation_required"
    token = challenge.payload["confirmation"]["token"]
    return bridge.execute(command, confirmation_token=token)


def test_registry_covers_audited_cli_tree_and_leaf_routes() -> None:
    registry = CommandBridge().registry()

    # 32/37 is the audited CLI surface before adding the explicit `tui`
    # compatibility launcher.  Live counts are dynamic and may be 33/38 once
    # that launcher is present, but the source invariant stays visible.
    assert registry["legacy_counts"] == {"top_level": 32, "leaf_routes": 37}
    assert registry["counts"]["top_level"] in {32, 33}
    assert registry["counts"]["leaf_routes"] in {37, 38}
    assert registry["policy_complete"] is True
    assert registry["unclassified_routes"] == []

    top_names = {item["name"] for item in registry["commands"]}
    assert top_names - {"tui"} == {
        "catalog",
        "validate",
        "import",
        "memory",
        "evolve",
        "help",
        "run",
        "generate",
        "app",
        "web",
        "replay",
        "lineage",
        "search",
        "diff",
        "favourite",
        "versions",
        "rollback",
        "promote",
        "forget",
        "revise",
        "export",
        "compose",
        "deploy",
        "deployments",
        "metrics",
        "dataset",
        "remember",
        "notes",
        "marketplace",
        "init",
        "doctor",
        "migrate-secrets",
    }
    assert {
        "catalog",
        "memory",
        "generate",
        "run",
        "dataset",
        "doctor",
        "app",
        "web",
    } <= top_names
    assert all(
        {"canonical_name", "invocation", "description", "source", "risk", "available"}
        <= set(item)
        for item in registry["commands"]
    )
    leaves = {item["invocation"] for item in registry["leaf_routes"]}
    assert {"memory ls", "memory history", "memory show"} <= leaves
    assert {
        "dataset list",
        "dataset add",
        "dataset run",
        "dataset export",
    } <= leaves
    launchers = {
        item["invocation"]: item
        for item in registry["leaf_routes"]
        if item["invocation"] in {"app", "web", "tui"}
    }
    assert launchers["app"]["available"] is False
    assert launchers["app"]["scope"] == "launcher"
    assert launchers["web"]["available"] is False
    assert launchers["web"]["scope"] == "developer"
    if "tui" in launchers:
        assert launchers["tui"]["available"] is False
        assert "terminal" in launchers["tui"]["unavailable_reason"]


def test_registry_exposes_only_reviewed_executable_slash_aliases() -> None:
    aliases = {
        item["invocation"]: item["target"]
        for item in CommandBridge().registry()["aliases"]
    }
    assert aliases == {
        "/dataset": "dataset",
        "/metrics": "metrics",
        "/status": "doctor",
    }
    assert "/theme" not in aliases
    assert "/settings" not in aliases


def test_registry_audits_all_44_tui_slash_names_without_false_parity() -> None:
    from care.screens.chat import _COMMAND_HANDLERS

    registry = CommandBridge().registry()
    entries = {item["name"]: item for item in registry["slash_commands"]}
    assert set(entries) == {
        "artifacts",
        "blocks",
        "branch",
        "clear",
        "cost",
        "dataset",
        "deploy",
        "deployments",
        "edit",
        "evolution",
        "exit",
        "export",
        "forget",
        "help",
        "history",
        "imgpreview",
        "library",
        "log",
        "logs",
        "marketplace",
        "memory",
        "metrics",
        "mode",
        "multi",
        "new",
        "profile",
        "promote",
        "quit",
        "remember",
        "resume",
        "revise",
        "rollback",
        "run",
        "runs",
        "sandbox",
        "sessions",
        "settings",
        "status",
        "subagents",
        "theme",
        "tour",
        "upload",
        "versions",
        "voice",
    }
    assert set(entries) == set(_COMMAND_HANDLERS)
    assert registry["counts"]["slash_commands"] == 44
    assert registry["counts"]["slash_local"] == 19
    assert registry["counts"]["slash_cli"] == 3
    assert registry["counts"]["slash_backend"] == 4
    assert registry["counts"]["slash_domain"] == 18
    assert registry["counts"]["slash_unavailable"] == 0
    assert (
        registry["counts"]["slash_local"]
        + registry["counts"]["slash_cli"]
        + registry["counts"]["slash_backend"]
        + registry["counts"]["slash_domain"]
        + registry["counts"]["slash_unavailable"]
        == registry["counts"]["slash_commands"]
    )
    assert registry["slash_audit"] == {
        "registered": 44,
        "exact_name_overlap": 15,
        "slash_only": 29,
        "cli_only": 18,
        "union": 62,
    }
    assert entries["exit"]["classification"] == "local"
    assert entries["exit"]["alias_of"] == "/quit"
    assert entries["runs"]["classification"] == "local"
    assert entries["runs"]["alias_of"] == "/history"
    assert "process-local" in entries["runs"]["description"]
    adapted_local = {
        "artifacts",
        "blocks",
        "branch",
        "edit",
        "resume",
        "sessions",
        "settings",
        "subagents",
        "tour",
    }
    assert all(entries[name]["classification"] == "local" for name in adapted_local)
    assert all(entries[name]["available"] for name in adapted_local)
    assert all(entries[name]["parity"] == "adapted" for name in adapted_local)
    assert "workspace" in entries["blocks"]["description"]
    assert "save <N> <workspace-relative-path>" in entries["blocks"]["supported_forms"]
    assert "process-local" in entries["branch"]["description"]
    assert "persistent" in entries["sessions"]["description"]
    assert entries["status"]["classification"] == "cli"
    assert entries["status"]["target"] == "doctor"
    backend_names = {
        name for name, entry in entries.items() if entry["classification"] == "backend"
    }
    assert backend_names == {"cost", "log", "logs", "profile"}
    assert all(entries[name]["available"] for name in backend_names)
    assert all(entries[name]["risk"] == "read_only" for name in backend_names)
    domain_names = {
        name for name, entry in entries.items() if entry["classification"] == "domain"
    }
    assert domain_names == {
        "deploy",
        "deployments",
        "evolution",
        "export",
        "forget",
        "imgpreview",
        "library",
        "marketplace",
        "memory",
        "promote",
        "remember",
        "revise",
        "rollback",
        "run",
        "sandbox",
        "upload",
        "versions",
        "voice",
    }
    assert all(entries[name]["available"] for name in domain_names)
    assert entries["versions"]["parity"] == "exact"
    assert entries["rollback"]["parity"] == "exact"
    assert entries["forget"]["parity"] == "exact"
    assert entries["deployments"]["parity"] == "adapted"
    assert entries["memory"]["parity"] == "adapted"
    assert "edit" in entries["memory"]["supported_forms"]
    assert entries["sandbox"]["supported_forms"] == [
        "[list]",
        "revoke <exact-sha256>",
    ]
    assert entries["voice"]["supported_forms"] == [
        "status",
        "transcribe <workspace-audio>",
    ]
    assert (
        sum(
            entry["classification"]
            in {"local", "cli", "backend", "domain", "unavailable"}
            for entry in entries.values()
        )
        == 44
    )
    assert all(entry["classification"] != "unavailable" for entry in entries.values())


@pytest.mark.parametrize(
    "command",
    [
        "/tour",
        "/sessions",
        "/resume latest",
        "/edit list",
        "/artifacts",
        "/blocks copy 1",
        "/subagents",
        "/branch checkpoint-1",
    ],
)
def test_frontend_local_slash_commands_never_cross_the_backend(command: str) -> None:
    result = CommandBridge().execute(command)

    assert result.status == 409
    assert result.payload["error"]["code"] == "local_command"


def test_frontend_local_command_registry_uses_safe_dom_and_workspace_file_save() -> None:
    source = (Path(__file__).parents[1] / "care" / "web_assets" / "app.js").read_text(
        encoding="utf-8"
    )

    for name in (
        "artifacts",
        "blocks",
        "branch",
        "edit",
        "resume",
        "sessions",
        "subagents",
        "tour",
    ):
        assert f'{{ invocation: "/{name}"' in source
    assert ".innerHTML" not in source
    assert "eval(" not in source
    assert 'querySelectorAll("pre code")' in source
    assert "navigator.clipboard.writeText" in source
    assert '`${API_ROOT}/workspace-files`' in source
    assert "workspace-relative-path" in source
    assert "File saving is intentionally unavailable" not in source
    assert "production_revision_pointer" in source
    assert "syncProductionPointerFromSession" in source
    assert "`/revise ${quoteCommandArgument(productionPointer.entity_id)}" in source
    assert "data.revision_pointer || data.duplicate" not in source


@pytest.mark.parametrize(
    "command",
    [
        "catalog --json",
        "care catalog --json",
        "maestro catalog --json",
        "/catalog --json",
    ],
)
def test_prefixes_and_direct_slash_form_reuse_argparse(command: str) -> None:
    seen: list[Any] = []

    def handler(args: Any, stdout: Any, _stderr: Any) -> int:
        seen.append(args)
        stdout.write("catalog-result\n")
        return 0

    result = CommandBridge(handler_overrides={"catalog": handler}).execute(command)

    assert result.status == 200
    assert result.payload["ok"] is True
    assert result.payload["stdout"] == "catalog-result\n"
    assert result.payload["command"]["name"] == "catalog"
    assert len(seen) == 1


def test_bound_workspace_roots_relative_cli_paths_without_changing_process_cwd(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process_cwd = tmp_path / "process-cwd"
    workspace = tmp_path / "selected-workspace"
    process_cwd.mkdir()
    workspace.mkdir()
    (workspace / "chain.json").write_text("{}", encoding="utf-8")
    monkeypatch.chdir(process_cwd)
    observed: dict[str, Any] = {}

    def handler(args: Any, stdout: Any, _stderr: Any) -> int:
        observed["file"] = args.file
        observed["root"] = current_workspace_root()
        stdout.write(args.file.read_text(encoding="utf-8"))
        return 0

    result = CommandBridge(
        workspace_root=workspace,
        handler_overrides={"validate": handler},
    ).execute("validate chain.json")

    assert result.status == 200
    assert result.payload["ok"] is True
    assert result.payload["stdout"] == "{}"
    assert observed == {
        "file": workspace / "chain.json",
        "root": workspace,
    }
    assert Path.cwd() == process_cwd
    assert current_workspace_root() == process_cwd


def test_bound_workspace_blocks_unsafe_read_only_paths(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside"
    workspace.mkdir()
    outside.mkdir()
    outside_file = outside / "chain.json"
    outside_file.write_text("{}", encoding="utf-8")
    linked_file = workspace / "linked.json"
    linked_file.symlink_to(outside_file)
    calls: list[Path] = []

    def handler(args: Any, _stdout: Any, _stderr: Any) -> int:
        calls.append(args.file)
        return 0

    bridge = CommandBridge(
        workspace_root=workspace,
        handler_overrides={"validate": handler},
    )
    commands = (
        f"validate {shlex.quote('nested/../chain.json')}",
        f"validate {shlex.quote(str(outside_file))}",
        f"validate {shlex.quote(str(linked_file))}",
    )

    results = [bridge.execute(command) for command in commands]

    assert calls == []
    assert all(result.status == 400 for result in results)
    assert all(
        result.payload["error"]["code"] == "parse_error"
        for result in results
    )
    assert "parent-directory traversal" in results[0].payload["stderr"]
    assert all(
        "selected workspace" in result.payload["stderr"]
        for result in results[1:]
    )


def test_bound_workspace_blocks_effectful_output_before_confirmation(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    called = False

    def handler(_args: Any, _stdout: Any, _stderr: Any) -> int:
        nonlocal called
        called = True
        return 0

    result = CommandBridge(
        workspace_root=workspace,
        handler_overrides={"export": handler},
    ).execute("export ../bundle.tar.gz chain-1")

    assert result.status == 400
    assert result.payload["error"]["code"] == "parse_error"
    assert result.payload["executed"] is False
    assert "confirmation" not in result.payload
    assert called is False
    assert not (tmp_path / "bundle.tar.gz").exists()


def test_bound_workspace_blocks_run_file_before_provider_confirmation(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    called = False

    def handler(_args: Any, _stdout: Any, _stderr: Any) -> int:
        nonlocal called
        called = True
        return 0

    result = CommandBridge(
        workspace_root=workspace,
        handler_overrides={"run": handler},
    ).execute("run chain-1 --execute --file ../secret.txt")

    assert result.status == 400
    assert result.payload["error"]["code"] == "parse_error"
    assert result.payload["executed"] is False
    assert "confirmation" not in result.payload
    assert called is False


def test_bound_workspace_blocks_glob_results_that_follow_symlinks(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside"
    workspace.mkdir()
    outside.mkdir()
    (outside / "secret.json").write_text(
        '{"steps": [], "secret_marker": "must-not-be-read"}',
        encoding="utf-8",
    )
    (workspace / "linked").symlink_to(outside, target_is_directory=True)

    result = CommandBridge(workspace_root=workspace).execute(
        "import '*/*.json'"
    )

    assert result.status == 200
    assert result.payload["ok"] is False
    assert result.payload["exit_code"] == 2
    assert result.payload["error"]["code"] == "command_failed"
    assert "selected workspace" in result.payload["stderr"]
    assert "must-not-be-read" not in repr(result.payload)


def test_bound_workspace_blocks_unsafe_glob_patterns_before_dispatch(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside"
    workspace.mkdir()
    outside.mkdir()
    (workspace / "linked").symlink_to(outside, target_is_directory=True)
    called = False

    def handler(_args: Any, _stdout: Any, _stderr: Any) -> int:
        nonlocal called
        called = True
        return 0

    bridge = CommandBridge(
        workspace_root=workspace,
        handler_overrides={"import": handler},
    )
    patterns = (
        "../outside/*.json",
        str(outside / "*.json"),
        "linked/*.json",
    )

    results = [
        bridge.execute(f"import {shlex.quote(pattern)}")
        for pattern in patterns
    ]

    assert called is False
    assert all(result.status == 400 for result in results)
    assert all(
        result.payload["error"]["code"] == "parse_error"
        for result in results
    )


def test_unbound_bridge_preserves_legacy_absolute_path_inputs(
    tmp_path: Path,
) -> None:
    source = tmp_path / "chain.json"
    source.write_text("{}", encoding="utf-8")
    observed: list[Path] = []

    def handler(args: Any, stdout: Any, _stderr: Any) -> int:
        observed.append(args.file)
        stdout.write(args.file.read_text(encoding="utf-8"))
        return 0

    result = CommandBridge(
        handler_overrides={"validate": handler}
    ).execute(f"validate {shlex.quote(str(source))}")

    assert result.status == 200
    assert result.payload["ok"] is True
    assert observed == [source]
    assert result.payload["stdout"] == "{}"


def test_bound_workspace_roots_project_config_lookup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from care.config import CareConfig

    process_cwd = tmp_path / "process-cwd"
    workspace = tmp_path / "selected-workspace"
    process_cwd.mkdir()
    workspace.mkdir()
    (process_cwd / "care.toml").write_text(
        "[chat]\ndefault_mode = 'interactive'\n",
        encoding="utf-8",
    )
    (workspace / "care.toml").write_text(
        "[chat]\ndefault_mode = 'production'\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(process_cwd)

    def handler(_args: Any, stdout: Any, _stderr: Any) -> int:
        stdout.write(CareConfig.load(env={}).chat.default_mode)
        return 0

    result = CommandBridge(
        workspace_root=workspace,
        handler_overrides={"catalog": handler},
    ).execute("catalog")

    assert result.status == 200
    assert result.payload["stdout"] == "production"
    assert Path.cwd() == process_cwd


def test_command_bridge_rejects_unvalidated_workspace_roots(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="absolute"):
        CommandBridge(workspace_root=Path("relative"))

    real = tmp_path / "real"
    real.mkdir()
    linked = tmp_path / "linked"
    linked.symlink_to(real, target_is_directory=True)
    with pytest.raises(ValueError, match="non-symlink"):
        CommandBridge(workspace_root=linked)


def test_status_alias_targets_doctor() -> None:
    def handler(args: Any, stdout: Any, _stderr: Any) -> int:
        stdout.write(f"offline={args.no_probes}\n")
        return 0

    result = CommandBridge(handler_overrides={"doctor": handler}).execute(
        "/status --no-probes"
    )

    assert result.status == 200
    assert result.payload["command"]["name"] == "doctor"
    assert result.payload["stdout"] == "offline=True\n"


def test_native_only_slash_command_never_falls_through_to_cli() -> None:
    called = False

    def handler(*_args: Any) -> int:
        nonlocal called
        called = True
        return 0

    result = CommandBridge(handler_overrides={"revise": handler}).execute(
        "/imgpreview status"
    )

    assert result.status == 200
    assert result.payload["ok"] is True
    assert result.payload["payload"]["native_preview"] is True
    assert called is False


class _StubDomainCommands:
    def __init__(self, factory: Any) -> None:
        self.factory = factory
        self.calls: list[tuple[str, list[str]]] = []

    def plan(self, name: str, args: list[str]) -> DomainCommandPlan:
        self.calls.append((name, list(args)))
        return self.factory(name, list(args))


class _StubRemainingCommands:
    def __init__(self) -> None:
        self.calls: list[tuple[str, list[str], Any]] = []

    def plan(
        self,
        name: str,
        args: list[str],
        *,
        transcript: Any = None,
    ) -> DomainCommandPlan:
        self.calls.append((name, list(args), transcript))
        return _domain_plan(
            name,
            risk=DomainRisk.FILESYSTEM_MUTATION,
            scope=str(transcript),
            effects=("Write the current transcript.",),
        )


def _domain_plan(
    name: str,
    *,
    risk: DomainRisk = DomainRisk.READ_ONLY,
    scope: str = "",
    effects: tuple[str, ...] = (),
    execute: Any = None,
) -> DomainCommandPlan:
    return DomainCommandPlan(
        name=name,
        risk=risk,
        reason="Reviewed domain effect.",
        effects=effects,
        confirmation_scope=scope,
        preview={"target": "chain-1", "scope": scope},
        execute=execute
        or (
            lambda: DomainCommandResult(
                stdout="domain-ok\n",
                payload={"adapter": name},
            )
        ),
    )


def test_read_only_domain_command_executes_without_argparse_fallthrough() -> None:
    domain = _StubDomainCommands(lambda name, _args: _domain_plan(name))
    cli_called = False

    def cli_handler(*_args: Any) -> int:
        nonlocal cli_called
        cli_called = True
        return 0

    result = CommandBridge(
        domain_commands=domain,
        handler_overrides={"versions": cli_handler},
    ).execute("/versions chain-1")

    assert result.status == 200
    assert result.payload["ok"] is True
    assert result.payload["stdout"] == "domain-ok\n"
    assert result.payload["payload"] == {"adapter": "versions"}
    assert result.payload["command"]["source"] == "desktop_domain"
    assert result.payload["command"]["risk"] == "read_only"
    assert domain.calls == [("versions", ["chain-1"])]
    assert cli_called is False


def test_export_transcript_reaches_remaining_adapter_on_confirmation_retry() -> None:
    remaining = _StubRemainingCommands()
    transcript = [{"role": "user", "text": "hello"}]
    bridge = CommandBridge(
        remaining_commands=remaining,  # type: ignore[arg-type]
        token_factory=lambda: "export-token",
    )

    challenge = bridge.execute("/export md", transcript=transcript)
    assert challenge.status == 409
    done = bridge.execute(
        "/export md",
        confirmation_token="export-token",
        transcript=transcript,
    )

    assert done.status == 200
    assert done.payload["ok"] is True
    assert remaining.calls == [
        ("export", ["md"], transcript),
        ("export", ["md"], transcript),
    ]


def test_domain_mutation_confirmation_contains_preview_and_exact_effects() -> None:
    executed: list[str] = []
    domain = _StubDomainCommands(
        lambda name, _args: _domain_plan(
            name,
            risk=DomainRisk.FILESYSTEM_MUTATION,
            scope="resolved-state-a",
            effects=(
                "Soft-delete chain chain-1.",
                "Soft-delete 2 linked dataset card(s).",
            ),
            execute=lambda: (
                executed.append("done")
                or DomainCommandResult(
                    stdout="forgot\n",
                    payload={"deleted": 3},
                )
            ),
        )
    )
    bridge = CommandBridge(
        domain_commands=domain,
        token_factory=lambda: "domain-token",
    )

    challenge = bridge.execute("/forget chain-1 --force")

    assert challenge.status == 409
    assert challenge.payload["error"] == {
        "code": "confirmation_required",
        "message": "Reviewed domain effect.",
    }
    assert challenge.payload["confirmation"]["token"] == "domain-token"
    assert challenge.payload["confirmation"]["risk"] == ("filesystem_write_or_mutation")
    assert challenge.payload["effects"] == [
        "Soft-delete chain chain-1.",
        "Soft-delete 2 linked dataset card(s).",
    ]
    assert challenge.payload["preview"] == {
        "target": "chain-1",
        "scope": "resolved-state-a",
    }
    assert challenge.payload["command"]["source"] == "desktop_domain"
    assert executed == []

    result = bridge.execute(
        "/forget chain-1 --force",
        confirmation_token="domain-token",
    )
    assert result.status == 200
    assert result.payload["ok"] is True
    assert result.payload["payload"] == {"deleted": 3}
    assert executed == ["done"]


def test_domain_confirmation_is_bound_to_resolved_state_fingerprint() -> None:
    state = {"scope": "before"}
    executed: list[str] = []

    def factory(name: str, _args: list[str]) -> DomainCommandPlan:
        return _domain_plan(
            name,
            risk=DomainRisk.EXTERNAL_SIDE_EFFECT,
            scope=state["scope"],
            effects=("Reload deployment 'weather'.",),
            execute=lambda: (
                executed.append(state["scope"])
                or DomainCommandResult(stdout="reloaded\n", payload={})
            ),
        )

    bridge = CommandBridge(
        domain_commands=_StubDomainCommands(factory),
        token_factory=lambda: "state-token",
    )
    challenge = bridge.execute("/deployments reload weather")
    assert challenge.status == 409
    state["scope"] = "after"

    rejected = bridge.execute(
        "/deployments reload weather",
        confirmation_token="state-token",
    )

    assert rejected.status == 409
    assert rejected.payload["error"]["code"] == "confirmation_invalid"
    assert executed == []


def test_domain_validation_error_fails_closed_without_executing() -> None:
    class RejectingDomain:
        def plan(self, name: str, args: list[str]) -> DomainCommandPlan:
            raise DomainCommandError(
                "slash_action_unsupported",
                "Desktop /memory edit is unavailable.",
                status=501,
            )

    result = CommandBridge(tui_parity_commands=RejectingDomain()).execute(
        "/memory edit"
    )

    assert result.status == 501
    assert result.payload["error"]["code"] == "slash_action_unsupported"
    assert result.payload["command"]["source"] == "desktop_domain"


def test_faulty_domain_adapter_exception_does_not_leak_details() -> None:
    def explode() -> DomainCommandResult:
        raise RuntimeError("/private/config.toml api_key=super-secret")

    domain = _StubDomainCommands(
        lambda name, _args: _domain_plan(name, execute=explode)
    )
    result = CommandBridge(domain_commands=domain).execute("/versions chain-1")

    assert result.status == 502
    assert result.payload["error"]["code"] == "domain_operation_failed"
    rendered = repr(result.payload)
    assert "/private/config.toml" not in rendered
    assert "super-secret" not in rendered
    assert "Traceback" not in rendered


def test_parser_errors_and_incomplete_groups_are_stable() -> None:
    malformed = CommandBridge().execute("catalog 'unterminated")
    assert malformed.status == 400
    assert malformed.payload["error"]["code"] == "invalid_command"

    unknown = CommandBridge().execute("catalog; echo should-not-run")
    assert unknown.status == 400
    assert unknown.payload["error"]["code"] == "parse_error"
    assert "invalid choice: 'catalog;'" in unknown.payload["stderr"]

    group = CommandBridge().execute("memory")
    assert group.status == 400
    assert group.payload["error"]["code"] in {"incomplete_command", "parse_error"}


def test_read_only_preview_executes_without_confirmation_but_apply_does_not() -> None:
    calls: list[bool] = []

    def handler(args: Any, stdout: Any, _stderr: Any) -> int:
        calls.append(args.apply)
        stdout.write("ok\n")
        return 0

    bridge = CommandBridge(handler_overrides={"import": handler})
    preview = bridge.execute("import example.json")
    challenge = bridge.execute("import example.json --apply")

    assert preview.status == 200
    assert calls == [False]
    assert challenge.status == 409
    assert challenge.payload["command"]["risk"] == "filesystem_write_or_mutation"
    assert calls == [False]

    applied = bridge.execute(
        "import example.json --apply",
        confirmation_token=challenge.payload["confirmation"]["token"],
    )
    assert applied.status == 200
    assert calls == [False, True]


@pytest.mark.parametrize(
    ("preview", "mutating"),
    [
        ("forget chain-1", "forget chain-1 --force"),
        ("compose one two", "compose one two --yes"),
        ("migrate-secrets --dry-run", "migrate-secrets"),
        ("run chain-1", "run chain-1 --export out.json"),
    ],
)
def test_conditional_risk_does_not_overconfirm_previews(
    preview: str,
    mutating: str,
) -> None:
    route = {
        "forget": "forget",
        "compose": "compose",
        "migrate-secrets": "migrate-secrets",
        "run": "run",
    }[preview.split()[0]]

    def handler(_args: Any, stdout: Any, _stderr: Any) -> int:
        stdout.write("handled\n")
        return 0

    bridge = CommandBridge(handler_overrides={route: handler})
    assert bridge.execute(preview).status == 200
    assert bridge.execute(mutating).status == 409


@pytest.mark.parametrize(
    ("command", "route", "risk"),
    [
        ("generate 'make a plan'", "generate", "provider_or_tool_execution"),
        ("run chain-1 --execute", "run", "provider_or_tool_execution"),
        ("dataset run chain-1", "dataset run", "provider_or_tool_execution"),
        ("revise chain-1 improve", "revise", "provider_or_tool_execution"),
        ("evolve chain-1", "evolve", "external_side_effect"),
        ("deploy chain-1", "deploy", "external_side_effect"),
    ],
)
def test_provider_and_external_commands_require_confirmation(
    command: str,
    route: str,
    risk: str,
) -> None:
    called = False

    def handler(_args: Any, _stdout: Any, _stderr: Any) -> int:
        nonlocal called
        called = True
        return 0

    result = CommandBridge(handler_overrides={route: handler}).execute(command)

    assert result.status == 409
    assert result.payload["error"]["code"] == "confirmation_required"
    assert result.payload["confirmation"]["risk"] == risk
    assert called is False


def test_confirmation_is_command_bound_one_time_and_replay_protected() -> None:
    calls: list[str] = []

    def handler(args: Any, _stdout: Any, _stderr: Any) -> int:
        calls.append(args.entity_id)
        return 0

    bridge = CommandBridge(handler_overrides={"favourite": handler})
    challenge = bridge.execute("favourite chain-a")
    token = challenge.payload["confirmation"]["token"]

    mismatch = bridge.execute("favourite chain-b", confirmation_token=token)
    assert mismatch.status == 409
    assert mismatch.payload["error"]["code"] == "confirmation_invalid"
    assert calls == []

    challenge = bridge.execute("favourite chain-a")
    token = challenge.payload["confirmation"]["token"]
    accepted = bridge.execute("favourite chain-a", confirmation_token=token)
    replay = bridge.execute("favourite chain-a", confirmation_token=token)

    assert accepted.status == 200
    assert calls == ["chain-a"]
    assert replay.status == 409
    assert replay.payload["error"]["code"] == "confirmation_invalid"


def test_confirmation_expires() -> None:
    now = [10.0]
    bridge = CommandBridge(
        handler_overrides={"favourite": lambda *_args: 0},
        confirmation_ttl_seconds=5,
        clock=lambda: now[0],
    )
    challenge = bridge.execute("favourite chain-a")
    now[0] = 16.0
    result = bridge.execute(
        "favourite chain-a",
        confirmation_token=challenge.payload["confirmation"]["token"],
    )
    assert result.status == 409
    assert result.payload["error"]["code"] == "confirmation_invalid"


def test_init_gets_empty_stdin_and_secret_values_are_redacted_everywhere() -> None:
    secret = "sk-live-ultra-secret"

    def handler(
        args: Any,
        stdout: Any,
        stderr: Any,
        *,
        stdin: Any,
    ) -> int:
        assert stdin.read() == ""
        stdout.write(f"received {args.api_key}\n")
        stderr.write(f"provider rejected token {args.api_key}\n")
        return 2

    bridge = CommandBridge(handler_overrides={"init": handler})
    command = f"init --api-key {secret} --non-interactive"
    challenge = bridge.execute(command)
    rendered_challenge = repr(challenge.payload)
    assert secret not in rendered_challenge
    assert "[redacted]" in challenge.payload["command"]["invocation"]

    result = bridge.execute(
        command,
        confirmation_token=challenge.payload["confirmation"]["token"],
    )
    rendered = repr(result.payload)
    assert secret not in rendered
    assert result.payload["stdout"] == "received [redacted]\n"
    assert result.payload["stderr"] == "provider rejected token [redacted]\n"


def test_output_and_command_sizes_are_bounded() -> None:
    def handler(_args: Any, stdout: Any, stderr: Any) -> int:
        stdout.write("x" * (MAX_COMMAND_OUTPUT_CHARS * 2))
        stderr.write("y" * (MAX_COMMAND_OUTPUT_CHARS * 2))
        return 0

    result = CommandBridge(handler_overrides={"catalog": handler}).execute("catalog")
    assert result.status == 200
    assert len(result.payload["stdout"]) <= MAX_COMMAND_OUTPUT_CHARS
    assert len(result.payload["stderr"]) <= MAX_COMMAND_OUTPUT_CHARS
    assert result.payload["truncated"] == {"stdout": True, "stderr": True}

    oversized = CommandBridge().execute("x" * (MAX_COMMAND_CHARS + 1))
    assert oversized.status == 400
    assert oversized.payload["error"]["code"] == "invalid_command"


@pytest.mark.parametrize(
    ("command", "needle"),
    [
        ("app", "already running"),
        ("web", "developer"),
    ],
)
def test_nested_launchers_return_information_instead_of_starting(
    command: str,
    needle: str,
) -> None:
    result = CommandBridge().execute(command)
    assert result.status == 200
    assert result.payload["ok"] is True
    assert result.payload["executed"] is False
    assert needle in result.payload["stdout"]


def test_handler_exception_is_bounded_and_has_no_traceback_or_secret() -> None:
    secret = "do-not-leak-me"

    def handler(args: Any, _stdout: Any, _stderr: Any) -> int:
        raise RuntimeError(f"bad key: {args.api_key}")

    bridge = CommandBridge(handler_overrides={"init": handler})
    command = f"init --api-key={secret} --non-interactive"
    result = _confirm(bridge, command)

    assert result.status == 200
    assert result.payload["error"]["code"] == "command_exception"
    assert secret not in repr(result.payload)
    assert "Traceback" not in result.payload["stderr"]


def test_registry_returns_a_defensive_copy() -> None:
    bridge = CommandBridge()
    first = bridge.registry()
    first["commands"].clear()
    assert bridge.registry()["commands"]


def test_future_unclassified_parser_leaf_fails_closed_even_with_token() -> None:
    calls: list[str] = []

    def handler(_args: Any, _stdout: Any, _stderr: Any) -> int:
        calls.append("called")
        return 0

    def parser_factory() -> argparse.ArgumentParser:
        parser = argparse.ArgumentParser(prog="maestro")
        sub = parser.add_subparsers(dest="subcommand")
        future = sub.add_parser("future-command", help="A newly added command.")
        future.set_defaults(_handler=handler)
        return parser

    bridge = CommandBridge(
        parser_factory=parser_factory,
        handler_overrides={"future-command": handler},
    )
    registry = bridge.registry()
    assert registry["policy_complete"] is False
    assert registry["unclassified_routes"] == ["future-command"]
    assert registry["leaf_routes"][0]["available"] is False

    first = bridge.execute("future-command")
    forged = bridge.execute(
        "future-command",
        confirmation_token="attacker-supplied-token",
    )
    for result in (first, forged):
        assert result.status == 501
        assert result.payload["error"]["code"] == "command_policy_missing"
        assert "confirmation" not in result.payload
    assert calls == []


def test_profile_adapter_lists_only_safe_metadata() -> None:
    secret_path = Path("/private/credentials/prod.toml")
    sources = DesktopCommandSources(
        list_profiles=lambda: [
            ProfileInfo(
                name="prod",
                path=secret_path,
                size_bytes=42,
                mtime=1_700_000_000.0,
            )
        ],
        active_profile_name=lambda: "prod",
    )

    result = CommandBridge(desktop_sources=sources).execute("/profile list")

    assert result.status == 200
    assert result.payload["command"]["source"] == "desktop_backend"
    assert result.payload["command"]["command_id"] == "slash.profile"
    assert result.payload["payload"] == {
        "active_profile": "prod",
        "default_profile_active": False,
        "profiles": [
            {
                "name": "prod",
                "active": True,
                "size_bytes": 42,
                "modified_at": 1_700_000_000.0,
            }
        ],
        "count": 1,
        "truncated": False,
    }
    rendered = repr(result.payload)
    assert str(secret_path) not in rendered
    assert ".toml" not in rendered


def test_log_adapters_filter_bound_and_redact_without_exposing_path(
    tmp_path: Path,
) -> None:
    log_path = tmp_path / "care-app-test.log"
    log_path.write_text(
        "\n".join(
            [
                "2026-01-01 [INFO] care.worker: token=first-secret",
                "2026-01-01 [WARNING] care.worker: --api-key second-secret",
                "2026-01-01 [ERROR] care.other: Authorization: Bearer third-secret",
                "2026-01-01 [CRITICAL] care.worker: Bearer fourth-secret",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    sources = DesktopCommandSources(active_log_path=lambda: log_path)

    result = CommandBridge(desktop_sources=sources).execute(
        "/logs --level WARNING --module care.worker --limit 4"
    )

    assert result.status == 200
    data = result.payload["payload"]
    assert data["file_name"] == "care-app-test.log"
    assert data["query"] == {
        "level": "WARNING",
        "module": "care.worker",
        "limit": 4,
    }
    assert len(data["lines"]) == 2
    rendered = repr(result.payload)
    for secret in ("first-secret", "second-secret", "third-secret", "fourth-secret"):
        assert secret not in rendered
    assert "[redacted]" in rendered
    assert str(tmp_path) not in rendered

    positional = CommandBridge(desktop_sources=sources).execute(
        "/log CRITICAL care.worker"
    )
    assert positional.status == 200
    assert positional.payload["payload"]["query"]["level"] == "CRITICAL"
    assert len(positional.payload["payload"]["lines"]) == 1


@pytest.mark.parametrize(
    "command",
    [
        "/log --level TRACE",
        "/logs --limit 0",
        "/log --limit 501",
        "/log --module ../../credentials",
        "/logs --unknown value",
    ],
)
def test_log_adapter_rejects_unbounded_or_unsupported_filters(command: str) -> None:
    called = False

    def active_path() -> None:
        nonlocal called
        called = True
        return None

    result = CommandBridge(
        desktop_sources=DesktopCommandSources(active_log_path=active_path)
    ).execute(command)

    assert result.status == 400
    assert result.payload["error"]["code"] == "slash_arguments_invalid"
    assert called is False


def test_cost_adapter_returns_rollups_without_raw_run_details() -> None:
    seen_limits: list[int] = []
    runs = [
        LocalRunEntry(
            run_id="run-secret-1",
            chain_id="chain-a",
            chain_name="Chain A",
            status="success",
            duration_seconds=2.5,
            tokens_in=10,
            tokens_out=5,
            cost_usd=0.25,
            mode="production",
            provider="local",
            error="credential=must-not-leak",
            replay_path="/private/replay.json",
            extra={"password": "must-not-leak"},
        ),
        LocalRunEntry(
            run_id="run-secret-2",
            chain_id="chain-a",
            chain_name="Chain A",
            status="failure",
            duration_seconds=1.5,
            tokens_in=3,
            tokens_out=2,
            cost_usd=0.05,
            mode="production",
            provider="local",
        ),
    ]

    def load_runs(*, limit: int) -> list[LocalRunEntry]:
        seen_limits.append(limit)
        return runs[:limit]

    result = CommandBridge(
        desktop_sources=DesktopCommandSources(load_local_runs=load_runs)
    ).execute("/cost --limit 2")

    assert result.status == 200
    assert seen_limits == [2]
    data = result.payload["payload"]
    assert data["overall"] == {
        "runs": 2,
        "successful_runs": 1,
        "failed_runs": 1,
        "tokens_in": 13,
        "tokens_out": 7,
        "tokens_total": 20,
        "cost_usd": 0.3,
        "total_duration_seconds": 4.0,
        "success_rate": 0.5,
    }
    assert data["by_provider"][0]["key"] == "local"
    rendered = repr(result.payload)
    for forbidden in ("run-secret", "must-not-leak", "/private/replay.json"):
        assert forbidden not in rendered


def test_sandbox_adapter_lists_and_confirmation_gates_exact_revoke(
    tmp_path: Path,
) -> None:
    full_sha = "a" * 64
    trust_path = tmp_path / "trusted.json"
    store = SkillTrustStore.load(path=trust_path)
    store.trust(
        sha256=full_sha,
        uri="file:///private/skills/credential-secret",
        name="pdf-reader",
        approved_at=datetime(2026, 1, 2, tzinfo=timezone.utc),
        allowed_tools=("Read", "Bash(token=tool-secret)"),
    )
    service = DesktopTuiParityCommands(
        DesktopTuiParitySources(
            load_trust_store=lambda: SkillTrustStore.load(path=trust_path),
            workspace_root=lambda: tmp_path,
        )
    )
    bridge = CommandBridge(
        tui_parity_commands=service,
        token_factory=lambda: "sandbox-confirmation-token",
    )
    listed = bridge.execute("/sandbox list")
    challenge = bridge.execute(f"/sandbox revoke {full_sha}")

    assert listed.status == 200
    row = listed.payload["payload"]["trusted_skills"][0]
    assert row["sha256_prefix"] == "a" * 12
    assert row["name"] == "pdf-reader"
    assert "[redacted]" in row["allowed_tools"][1]
    rendered = repr(listed.payload)
    assert full_sha not in rendered
    assert "file:///private" not in rendered
    assert "tool-secret" not in rendered
    assert challenge.status == 409
    revoked = bridge.execute(
        f"/sandbox revoke {full_sha}",
        confirmation_token="sandbox-confirmation-token",
    )
    assert revoked.status == 200
    assert revoked.payload["payload"]["revoked"] is True
    assert not SkillTrustStore.load(path=trust_path).is_trusted(full_sha)


def test_voice_adapter_uses_confirmed_local_workspace_transcription(
    tmp_path: Path,
) -> None:
    audio = tmp_path / "sample.wav"
    audio.write_bytes(b"RIFF\x08\x00\x00\x00WAVEaudio")
    backend = LocalVoiceBackend(
        name="faster-whisper-local",
        transcribe=lambda snapshot: f"heard {snapshot.display_name}",
        state_sha256=lambda: "b" * 64,
    )
    service = DesktopTuiParityCommands(
        DesktopTuiParitySources(
            workspace_root=lambda: tmp_path,
            load_voice_backend=lambda: backend,
        )
    )
    bridge = CommandBridge(
        tui_parity_commands=service,
        token_factory=lambda: "voice-confirmation-token",
    )
    status = bridge.execute("/voice status")
    challenge = bridge.execute("/voice transcribe sample.wav")

    assert status.status == 200
    assert status.payload["payload"]["available"] is True
    assert status.payload["payload"]["backend"] == "faster-whisper-local"
    assert status.payload["payload"]["local_only"] is True
    assert challenge.status == 409
    transcribed = bridge.execute(
        "/voice transcribe sample.wav",
        confirmation_token="voice-confirmation-token",
    )
    assert transcribed.status == 200
    assert transcribed.payload["payload"]["transcript"] == "heard sample.wav"


def test_desktop_adapter_exception_is_stable_and_does_not_leak_source_details() -> None:
    def fail() -> list[Any]:
        raise RuntimeError("/private/config.toml api_key=super-secret")

    result = CommandBridge(
        desktop_sources=DesktopCommandSources(list_profiles=fail)
    ).execute("/profile")

    assert result.status == 200
    assert result.payload["error"]["code"] == "command_exception"
    rendered = repr(result.payload)
    assert "/private/config.toml" not in rendered
    assert "super-secret" not in rendered
    assert "Traceback" not in rendered
