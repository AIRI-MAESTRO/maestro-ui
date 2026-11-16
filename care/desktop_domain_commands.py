"""Exact, UI-neutral adapters for Maestro's domain slash commands.

The native desktop command bridge must not route a slash command to a CLI
command merely because both happen to share a name.  This module instead
reuses the same Memory, long-term-memory, and agent-hub services as the TUI.
It is deliberately independent from HTTP and from :mod:`care.desktop_commands`
so parsing, previewing, and mutation effects can be tested hermetically.

Mutating plans carry a stable confirmation scope derived from the exact
resolved targets.  The desktop bridge combines that scope with the command
digest, issues a short-lived one-use token, and only then calls ``execute``.
No function in this module invokes a model or executes an agent tool.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from care.redaction import redact_sensitive_text


MAX_REFERENCE_CHARS = 256
MAX_VERSION_ROWS = 200
MAX_DATASET_ROWS = 500
MAX_DEPLOYMENT_ROWS = 200
MAX_CONTEXT_CHARS = 12_000
MAX_DIFF_CHARS = 8_000
MAX_STDOUT_CHARS = 64 * 1024
MAX_SAFE_VALUE_CHARS = 2_000

_CHANNEL_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
_VERSION_RE = re.compile(r"^[A-Za-z0-9._:-]{1,256}$")
_ENTITY_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$")
_DEPLOYMENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
_ABSOLUTE_PATH_RE = re.compile(
    r"(?<![A-Za-z0-9:/])(?:~/(?:[^\s`'\"<>]+)|"
    r"/(?:Users|home|private|tmp|var|etc|opt|Volumes)/(?:[^\s`'\"<>]+)|"
    r"[A-Za-z]:\\(?:[^\s`'\"<>]+))"
)
_CONTEXT_PATH_LABEL_RE = re.compile(
    r"^(##\s+(?:Global user context|Project context))\s+\([^\n]*\)$",
    re.MULTILINE,
)


class DomainRisk(str, Enum):
    """Side-effect class used by the server-side confirmation gate."""

    READ_ONLY = "read_only"
    FILESYSTEM_MUTATION = "filesystem_write_or_mutation"
    EXTERNAL_SIDE_EFFECT = "external_side_effect"


class DomainCommandError(ValueError):
    """Stable, already-sanitised command failure safe for the desktop API."""

    def __init__(self, code: str, message: str, *, status: int = 400) -> None:
        super().__init__(_safe_text(message, max_chars=1_000))
        self.code = code
        self.status = status


@dataclass(frozen=True)
class DomainCommandResult:
    """HTTP-neutral output from a domain command."""

    stdout: str
    payload: dict[str, Any]
    ok: bool = True
    exit_code: int = 0
    stderr: str = ""
    ephemeral_secret: str | None = None


@dataclass(frozen=True)
class DomainCommandPlan:
    """Parsed command plus its exact side effects and execution closure."""

    name: str
    risk: DomainRisk
    reason: str
    effects: tuple[str, ...]
    confirmation_scope: str
    preview: dict[str, Any]
    execute: Callable[[], DomainCommandResult]


def _default_load_config() -> Any:
    from care.config import CareConfig

    return CareConfig.load()


def _default_build_memory(config: Any) -> Any:
    from care.memory import CareMemory

    return CareMemory.from_config(config)


def _default_build_ltm(config: Any) -> Any | None:
    from care.memory_ltm import build_ltm

    return build_ltm(config)


def _default_load_context(config: Any) -> str:
    from care.context_md import load_user_context

    return load_user_context(config)


def _default_recall_ltm(
    ltm: Any,
    session_id: str,
    *,
    max_chars: int,
) -> str:
    from care.memory_ltm import recall_digest

    return recall_digest(ltm, session_id, max_chars=max_chars)


def _default_ltm_session_id(config: Any) -> str:
    from care.memory_ltm import ltm_session_id

    return ltm_session_id(config)


def _default_build_hub(config: Any) -> Any:
    from care.runtime.agent_hub import HubClient

    hub = config.hub
    return HubClient(hub.base_url, timeout=hub.timeout)


@dataclass(frozen=True)
class DesktopDomainSources:
    """Dependency seams; tests provide fakes and therefore never use network."""

    load_config: Callable[[], Any] = _default_load_config
    build_memory: Callable[[Any], Any] = _default_build_memory
    build_ltm: Callable[[Any], Any | None] = _default_build_ltm
    load_context: Callable[[Any], str] = _default_load_context
    recall_ltm: Callable[..., str] = _default_recall_ltm
    ltm_session_id: Callable[[Any], str] = _default_ltm_session_id
    build_hub: Callable[[Any], Any] = _default_build_hub


class DesktopDomainCommands:
    """Plan and execute reviewed TUI-domain commands for the native app."""

    NAMES = frozenset({"versions", "memory", "rollback", "forget", "deployments"})

    def __init__(self, sources: DesktopDomainSources | None = None) -> None:
        self._sources = sources or DesktopDomainSources()

    def plan(self, name: str, args: Sequence[str]) -> DomainCommandPlan:
        """Parse ``args`` strictly and return an executable, auditable plan."""

        command = str(name or "").lower()
        tokens = [str(item) for item in args]
        if command not in self.NAMES:
            raise DomainCommandError(
                "slash_command_unavailable",
                f"Desktop /{command or '?'} has no reviewed domain adapter.",
                status=501,
            )
        try:
            if command == "versions":
                return self._plan_versions(tokens)
            if command == "memory":
                return self._plan_memory(tokens)
            if command == "rollback":
                return self._plan_rollback(tokens)
            if command == "forget":
                return self._plan_forget(tokens)
            return self._plan_deployments(tokens)
        except DomainCommandError:
            raise
        except Exception as exc:  # noqa: BLE001 - never reflect source details
            raise DomainCommandError(
                "domain_unavailable",
                f"Desktop /{command} data is temporarily unavailable.",
                status=503,
            ) from exc

    # ------------------------------------------------------------------
    # /versions
    # ------------------------------------------------------------------
    def _plan_versions(self, args: list[str]) -> DomainCommandPlan:
        if not args:
            raise DomainCommandError(
                "slash_arguments_invalid",
                "Usage: /versions <chain-id|unique-name> [diff <vA> <vB>].",
            )
        diff_at = [index for index, token in enumerate(args) if token == "diff"]
        if diff_at:
            if len(diff_at) != 1 or diff_at[0] != len(args) - 3:
                raise DomainCommandError(
                    "slash_arguments_invalid",
                    "Use `/versions <chain-id|unique-name> diff <vA> <vB>`.",
                )
            ref_tokens = args[: diff_at[0]]
            from_version = _version_id(args[-2])
            to_version = _version_id(args[-1])
        else:
            ref_tokens = args
            from_version = to_version = None
        ref = _reference(" ".join(ref_tokens))
        memory, client = self._memory_handles()
        entity_id, record = _resolve_chain(client, ref, channel="latest")

        if from_version is not None and to_version is not None:

            def run_diff() -> DomainCommandResult:
                try:
                    differ = getattr(client, "diff_versions", None)
                    if not callable(differ):
                        raise DomainCommandError(
                            "domain_capability_missing",
                            "This Memory build does not expose version diff.",
                            status=501,
                        )
                    response = differ(entity_id, from_version, to_version)
                except DomainCommandError:
                    raise
                except Exception as exc:  # noqa: BLE001
                    raise _domain_failure("Version diff failed.", exc) from exc
                patch = _field(response, "patch", {}) or {}
                safe_patch, truncated = _safe_json_projection(
                    patch,
                    max_chars=MAX_DIFF_CHARS,
                )
                payload = {
                    "entity_id": _safe_identifier(entity_id),
                    "from_version": from_version,
                    "to_version": to_version,
                    "patch": safe_patch,
                    "truncated": truncated,
                }
                if not patch:
                    stdout = (
                        f"No differences between {from_version} and {to_version}.\n"
                    )
                else:
                    rendered = json.dumps(
                        safe_patch,
                        ensure_ascii=False,
                        indent=2,
                        sort_keys=True,
                    )
                    stdout = (
                        f"Diff {from_version[:12]} -> {to_version[:12]}:\n{rendered}\n"
                    )
                return _result(stdout, payload)

            return _read_plan(
                "versions",
                {
                    "action": "diff",
                    "entity_id": _safe_identifier(entity_id),
                    "from_version": from_version,
                    "to_version": to_version,
                },
                run_diff,
            )

        def run_list() -> DomainCommandResult:
            try:
                versions = _list_versions(client, entity_id)
                channel_of = _channel_versions(client, entity_id)
            except Exception as exc:  # noqa: BLE001
                raise _domain_failure("Could not list chain versions.", exc) from exc
            rows = [
                _version_payload(version, channel_of)
                for version in sorted(
                    versions,
                    key=lambda item: _safe_int(_field(item, "version_number", 0)),
                    reverse=True,
                )[:MAX_VERSION_ROWS]
            ]
            display_name = _display_name(record, entity_id)
            lines = [f"Versions of {display_name} ({_safe_identifier(entity_id)}):"]
            lines.extend(_format_version_line(row) for row in rows)
            if not rows:
                lines = [f"No versions found for {_safe_identifier(entity_id)}."]
            payload = {
                "entity_id": _safe_identifier(entity_id),
                "display_name": display_name,
                "versions": rows,
                "count": len(rows),
                "truncated": len(versions) > MAX_VERSION_ROWS,
            }
            return _result("\n".join(lines) + "\n", payload)

        del memory  # resolution intentionally happens through the raw TUI client
        return _read_plan(
            "versions",
            {
                "action": "list",
                "entity_id": _safe_identifier(entity_id),
            },
            run_list,
        )

    # ------------------------------------------------------------------
    # /memory
    # ------------------------------------------------------------------
    def _plan_memory(self, args: list[str]) -> DomainCommandPlan:
        action = args[0].lower() if args else "show"
        if action == "show":
            if len(args) > 1:
                raise DomainCommandError(
                    "slash_arguments_invalid",
                    "Desktop /memory show accepts no additional arguments.",
                )

            def run_show() -> DomainCommandResult:
                config = self._load_config()
                try:
                    context = self._sources.load_context(config)
                    ltm = self._sources.build_ltm(config)
                    session_id = self._sources.ltm_session_id(config)
                    digest = (
                        self._sources.recall_ltm(
                            ltm,
                            session_id,
                            max_chars=MAX_CONTEXT_CHARS,
                        )
                        if ltm is not None
                        else ""
                    )
                except Exception as exc:  # noqa: BLE001
                    raise _domain_failure(
                        "Could not read Maestro memory.", exc
                    ) from exc
                safe_context = _safe_content(
                    context or "_(no persistent context)_",
                    max_chars=MAX_CONTEXT_CHARS,
                )
                safe_digest = _safe_content(
                    digest or "_(long-term memory empty)_",
                    max_chars=MAX_CONTEXT_CHARS,
                )
                payload = {
                    "context": safe_context,
                    "long_term_memory": safe_digest,
                    "ltm_enabled": ltm is not None,
                    "editable": False,
                }
                return _result(
                    "## Memory\n\n" + safe_context + "\n\n" + safe_digest + "\n",
                    payload,
                )

            return _read_plan("memory", {"action": "show"}, run_show)

        if action == "edit":
            raise DomainCommandError(
                "slash_action_unsupported",
                "Desktop /memory edit is unavailable until a safe native content editor exists.",
                status=501,
            )
        if action != "forget":
            raise DomainCommandError(
                "slash_action_unsupported",
                "Desktop /memory supports `show` and `forget <key>`; edit is unavailable.",
            )
        if len(args) < 2:
            raise DomainCommandError(
                "slash_arguments_invalid",
                "Usage: /memory forget <key>.",
            )
        key = _memory_key(" ".join(args[1:]))
        config = self._load_config()
        try:
            ltm = self._sources.build_ltm(config)
            if ltm is None:
                raise DomainCommandError(
                    "memory_disabled",
                    "Long-term memory is disabled.",
                    status=409,
                )
            session_id = self._sources.ltm_session_id(config)
            current = ltm.retrieve(key, session_id=session_id)
        except DomainCommandError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise _domain_failure("Could not inspect the memory key.", exc) from exc
        present = current not in (None, "")
        value_digest = hashlib.sha256(
            str(current if present else "<missing>").encode(
                "utf-8", errors="surrogatepass"
            )
        ).hexdigest()
        safe_key = _safe_text(key, max_chars=MAX_REFERENCE_CHARS)
        preview = {
            "action": "forget",
            "key": safe_key,
            "present": present,
        }

        def run_forget() -> DomainCommandResult:
            try:
                removed = bool(ltm.delete(key, session_id=session_id))
            except Exception as exc:  # noqa: BLE001
                raise _domain_failure("Could not delete the memory key.", exc) from exc
            payload = {**preview, "removed": removed}
            stdout = (
                f"Forgot long-term-memory key {safe_key!r}.\n"
                if removed
                else f"No long-term-memory key {safe_key!r} was present.\n"
            )
            return _result(stdout, payload)

        return _mutation_plan(
            "memory",
            DomainRisk.FILESYSTEM_MUTATION,
            "This deletes one key from Maestro's persistent long-term memory.",
            (f"Delete long-term-memory key {safe_key!r}.",),
            {
                "action": "forget",
                "session_id": _safe_identifier(session_id),
                "key": key,
                "value_digest": value_digest,
            },
            preview,
            run_forget,
        )

    # ------------------------------------------------------------------
    # /rollback
    # ------------------------------------------------------------------
    def _plan_rollback(self, args: list[str]) -> DomainCommandPlan:
        ref, channel, explicit_target = _parse_rollback(args)
        _memory, client = self._memory_handles()
        entity_id, record = _resolve_chain(client, ref, channel=channel)
        current_number = _optional_int(_field(record, "version_number", None))
        current_version = _safe_identifier(_field(record, "version_id", ""))
        current_label = (
            f"v{current_number}" if current_number is not None else "current"
        )

        if explicit_target is None:
            try:
                versions = _list_versions(client, entity_id)
            except Exception as exc:  # noqa: BLE001
                raise _domain_failure(
                    "Could not resolve the previous version.", exc
                ) from exc
            earlier = [
                version
                for version in versions
                if current_number is None
                or _safe_int(_field(version, "version_number", 0)) < current_number
            ]
            if not earlier:
                raise DomainCommandError(
                    "rollback_not_available",
                    f"{_display_name(record, entity_id)} is already at its earliest version on {channel!r}.",
                    status=409,
                )
            previous = max(
                earlier,
                key=lambda item: _safe_int(_field(item, "version_number", 0)),
            )
            target_id = _version_id(_field(previous, "version_id", ""))
            target_number = _optional_int(_field(previous, "version_number", None))
            target_label = (
                f"v{target_number}" if target_number is not None else target_id[:12]
            )
        else:
            target_id = explicit_target
            target_label = target_id[:12]

        display_name = _display_name(record, entity_id)
        preview = {
            "action": "rollback",
            "entity_id": _safe_identifier(entity_id),
            "display_name": display_name,
            "channel": channel,
            "from_version": current_version or None,
            "from_label": current_label,
            "to_version": target_id,
            "to_label": target_label,
        }

        def run_rollback() -> DomainCommandResult:
            try:
                pin = getattr(client, "pin_channel", None)
                if not callable(pin):
                    raise DomainCommandError(
                        "domain_capability_missing",
                        "This Memory build does not expose channel pinning.",
                        status=501,
                    )
                pin(entity_id, channel, target_id)
            except DomainCommandError:
                raise
            except Exception as exc:  # noqa: BLE001
                raise _domain_failure("Rollback failed.", exc) from exc
            return _result(
                f"Rolled back {display_name} on {channel}: {current_label} -> {target_label}.\n",
                {**preview, "pinned": True},
            )

        return _mutation_plan(
            "rollback",
            DomainRisk.FILESYSTEM_MUTATION,
            "This repoints a saved chain channel to an earlier version.",
            (
                f"Move channel {channel!r} for {_safe_identifier(entity_id)} "
                f"from {current_label} to {target_label}.",
            ),
            {
                "entity_id": entity_id,
                "channel": channel,
                "current_version": current_version,
                "target_version": target_id,
            },
            preview,
            run_rollback,
        )

    # ------------------------------------------------------------------
    # /forget
    # ------------------------------------------------------------------
    def _plan_forget(self, args: list[str]) -> DomainCommandPlan:
        chain_id, force = _parse_forget(args)
        memory, _client = self._memory_handles()
        chain_exists = _chain_exists(memory, chain_id)
        dataset = _dataset_entries(memory, chain_id)
        card_ids = tuple(
            _safe_identifier(_field(item, "entity_id", ""))
            for item in dataset
            if _safe_identifier(_field(item, "entity_id", ""))
        )
        preview = {
            "action": "forget",
            "chain_id": chain_id,
            "chain_exists": chain_exists,
            "dataset_cards": list(card_ids),
            "dataset_count": len(card_ids),
            "soft_delete": True,
            "force": force,
        }

        def render_preview() -> DomainCommandResult:
            if not chain_exists and not card_ids:
                stdout = (
                    f"Nothing saved for chain {chain_id}; no dataset cards found.\n"
                )
            else:
                chain_text = f"chain {chain_id}" if chain_exists else "no chain row"
                stdout = (
                    f"Would soft-delete {chain_text} and {len(card_ids)} linked "
                    "dataset card(s). Re-run with --force to delete.\n"
                )
            return _result(stdout, preview)

        if not force:
            return _read_plan("forget", preview, render_preview)

        def run_forget() -> DomainCommandResult:
            chain_ok = True
            deleted_cards = 0
            failed_cards = 0
            if chain_exists:
                try:
                    chain_ok = _delete_entity(memory, "chain", chain_id)
                except Exception:  # noqa: BLE001 - report bounded aggregate only
                    chain_ok = False
            for card_id in card_ids:
                try:
                    if _delete_entity(memory, "memory_card", card_id):
                        deleted_cards += 1
                    else:
                        failed_cards += 1
                except Exception:  # noqa: BLE001
                    failed_cards += 1
            ok = chain_ok and failed_cards == 0
            result_payload = {
                **preview,
                "chain_deleted": chain_ok if chain_exists else False,
                "dataset_deleted": deleted_cards,
                "dataset_failed": failed_cards,
                "partial": not ok,
            }
            if ok:
                stdout = (
                    f"Forgot chain {chain_id}; {deleted_cards} linked dataset "
                    "card(s) also soft-deleted.\n"
                )
                return _result(stdout, result_payload)
            message = (
                f"Forget was partial for {chain_id}: chain "
                f"{'ok' if chain_ok else 'failed'}, dataset "
                f"{deleted_cards} ok / {failed_cards} failed."
            )
            return _result(
                "",
                result_payload,
                ok=False,
                exit_code=2,
                stderr=message + "\n",
            )

        effects = []
        if chain_exists:
            effects.append(f"Soft-delete chain {chain_id}.")
        effects.append(f"Soft-delete {len(card_ids)} linked dataset card(s).")
        return _mutation_plan(
            "forget",
            DomainRisk.FILESYSTEM_MUTATION,
            "This soft-deletes a saved chain and every linked dataset card.",
            tuple(effects),
            {
                "chain_id": chain_id,
                "chain_exists": chain_exists,
                "dataset_cards": card_ids,
            },
            preview,
            run_forget,
        )

    # ------------------------------------------------------------------
    # /deployments
    # ------------------------------------------------------------------
    def _plan_deployments(self, args: list[str]) -> DomainCommandPlan:
        action, name = _parse_deployments(args)
        config = self._load_config()
        try:
            hub = self._sources.build_hub(config)
        except Exception as exc:  # noqa: BLE001
            raise _domain_failure("Could not configure the agent hub.", exc) from exc

        if action == "list":

            def run_list() -> DomainCommandResult:
                _require_hub_health(hub)
                try:
                    source_rows = _run_async(hub.list_deployments())
                except Exception as exc:  # noqa: BLE001
                    raise _domain_failure("Could not list deployments.", exc) from exc
                deployments = list(source_rows or [])
                rows = [
                    _deployment_payload(hub, row)
                    for row in deployments[:MAX_DEPLOYMENT_ROWS]
                ]
                lines = ["Deployments:"]
                lines.extend(_format_deployment_line(row) for row in rows)
                if not rows:
                    lines = ["No deployments yet."]
                payload = {
                    "action": "list",
                    "deployments": rows,
                    "count": len(rows),
                    "truncated": len(deployments) > MAX_DEPLOYMENT_ROWS,
                }
                return _result("\n".join(lines) + "\n", payload)

            return _read_plan("deployments", {"action": "list"}, run_list)

        assert name is not None
        if action == "docs":

            def run_docs() -> DomainCommandResult:
                _require_hub_health(hub)
                url = _safe_url(hub.docs_url(name))
                if not url:
                    raise DomainCommandError(
                        "hub_url_invalid",
                        "The hub returned an invalid documentation URL.",
                        status=502,
                    )
                payload = {
                    "action": "docs",
                    "name": name,
                    "url": url,
                    "opened": False,
                }
                return _result(f"docs: {url}\n", payload)

            return _read_plan(
                "deployments",
                {"action": "docs", "name": name, "opened": False},
                run_docs,
            )

        _require_hub_health(hub)
        try:
            current = _run_async(hub.get_deployment(name))
        except Exception as exc:  # noqa: BLE001
            raise _domain_failure("Could not resolve the deployment.", exc) from exc
        current_payload = _deployment_payload(hub, current)
        preview = {
            "action": action,
            "name": name,
            "current": current_payload,
        }

        if action == "undeploy":

            def run_undeploy() -> DomainCommandResult:
                try:
                    _run_async(hub.undeploy(name))
                except Exception as exc:  # noqa: BLE001
                    raise _domain_failure("Undeploy failed.", exc) from exc
                return _result(
                    f"Undeployed {name}.\n",
                    {**preview, "undeployed": True},
                )

            return _mutation_plan(
                "deployments",
                DomainRisk.EXTERNAL_SIDE_EFFECT,
                "This removes an agent from the running hub.",
                (f"Undeploy agent {name!r} from the configured hub.",),
                {
                    "action": action,
                    "name": name,
                    "entity_id": current_payload.get("entity_id"),
                    "version": current_payload.get("version"),
                },
                preview,
                run_undeploy,
            )

        def run_reload() -> DomainCommandResult:
            try:
                reloaded, deployment = _run_async(hub.reload(name))
            except Exception as exc:  # noqa: BLE001
                raise _domain_failure("Deployment reload failed.", exc) from exc
            row = _deployment_payload(hub, deployment)
            payload = {
                **preview,
                "reloaded": bool(reloaded),
                "deployment": row,
            }
            if reloaded:
                return _result(f"Reloaded {name} -> {row['version']}.\n", payload)
            return _result(
                f"Reload kept the previous version for {name}; inspect the hub log.\n",
                payload,
            )

        return _mutation_plan(
            "deployments",
            DomainRisk.EXTERNAL_SIDE_EFFECT,
            "This asks the running hub to reload an agent deployment.",
            (f"Reload agent {name!r} from its configured source and preflight it.",),
            {
                "action": action,
                "name": name,
                "entity_id": current_payload.get("entity_id"),
                "version": current_payload.get("version"),
            },
            preview,
            run_reload,
        )

    def _load_config(self) -> Any:
        try:
            return self._sources.load_config()
        except Exception as exc:  # noqa: BLE001
            raise _domain_failure("Could not load Maestro configuration.", exc) from exc

    def _memory_handles(self) -> tuple[Any, Any]:
        config = self._load_config()
        try:
            memory = self._sources.build_memory(config)
        except Exception as exc:  # noqa: BLE001
            raise _domain_failure("Could not configure Maestro memory.", exc) from exc
        client = getattr(memory, "client", None) or memory
        return memory, client


def _read_plan(
    name: str,
    preview: dict[str, Any],
    execute: Callable[[], DomainCommandResult],
) -> DomainCommandPlan:
    return DomainCommandPlan(
        name=name,
        risk=DomainRisk.READ_ONLY,
        reason="",
        effects=(),
        confirmation_scope="",
        preview=_safe_mapping(preview),
        execute=execute,
    )


def _mutation_plan(
    name: str,
    risk: DomainRisk,
    reason: str,
    effects: tuple[str, ...],
    scope: Mapping[str, Any],
    preview: dict[str, Any],
    execute: Callable[[], DomainCommandResult],
) -> DomainCommandPlan:
    serialised = json.dumps(
        _safe_mapping(scope),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return DomainCommandPlan(
        name=name,
        risk=risk,
        reason=_safe_text(reason, max_chars=500),
        effects=tuple(_safe_text(effect, max_chars=500) for effect in effects),
        confirmation_scope=hashlib.sha256(
            serialised.encode("utf-8", errors="surrogatepass")
        ).hexdigest(),
        preview=_safe_mapping(preview),
        execute=execute,
    )


def _result(
    stdout: str,
    payload: Mapping[str, Any],
    *,
    ok: bool = True,
    exit_code: int = 0,
    stderr: str = "",
) -> DomainCommandResult:
    return DomainCommandResult(
        stdout=_safe_content(stdout, max_chars=MAX_STDOUT_CHARS),
        payload=_safe_mapping(payload),
        ok=bool(ok),
        exit_code=int(exit_code),
        stderr=_safe_content(stderr, max_chars=MAX_STDOUT_CHARS),
    )


def _memory_key(value: Any) -> str:
    key = str(value or "").strip()
    if not key or len(key) > MAX_REFERENCE_CHARS or _CONTROL_RE.search(key):
        raise DomainCommandError(
            "slash_arguments_invalid",
            f"Memory key must be 1-{MAX_REFERENCE_CHARS} printable characters.",
        )
    return key


def _reference(value: Any) -> str:
    ref = str(value or "").strip()
    if not ref or len(ref) > MAX_REFERENCE_CHARS or _CONTROL_RE.search(ref):
        raise DomainCommandError(
            "slash_arguments_invalid",
            f"Chain reference must be 1-{MAX_REFERENCE_CHARS} printable characters.",
        )
    if ref.startswith("-"):
        raise DomainCommandError(
            "slash_arguments_invalid",
            "Chain reference must not start with an option prefix.",
        )
    return ref


def _version_id(value: Any) -> str:
    version = str(value or "").strip()
    if not _VERSION_RE.fullmatch(version):
        raise DomainCommandError(
            "slash_arguments_invalid",
            "Version ids may use only letters, digits, '.', '_', ':', and '-'.",
        )
    return version


def _channel(value: Any) -> str:
    channel = str(value or "").strip()
    if not _CHANNEL_RE.fullmatch(channel):
        raise DomainCommandError(
            "slash_arguments_invalid",
            "Channel names may use only letters, digits, '.', '_', and '-'.",
        )
    return channel


def _parse_rollback(args: list[str]) -> tuple[str, str, str | None]:
    if not args:
        raise DomainCommandError(
            "slash_arguments_invalid",
            "Usage: /rollback <chain-id|unique-name> [--channel NAME] [--to VERSION].",
        )
    channel = "stable"
    target: str | None = None
    ref_parts: list[str] = []
    seen: set[str] = set()
    index = 0
    while index < len(args):
        token = args[index]
        option = next(
            (
                item
                for item in ("--channel", "--to")
                if token == item or token.startswith(item + "=")
            ),
            None,
        )
        if option is None:
            if token.startswith("-"):
                raise DomainCommandError(
                    "slash_arguments_invalid",
                    "Desktop /rollback accepts only --channel and --to.",
                )
            ref_parts.append(token)
            index += 1
            continue
        if option in seen:
            raise DomainCommandError(
                "slash_arguments_invalid",
                f"{option} may be supplied only once.",
            )
        seen.add(option)
        raw, index = _option_value(args, index, option)
        if option == "--channel":
            channel = _channel(raw)
        else:
            target = _version_id(raw)
    return _reference(" ".join(ref_parts)), channel, target


def _parse_forget(args: list[str]) -> tuple[str, bool]:
    if not args:
        raise DomainCommandError(
            "slash_arguments_invalid",
            "Usage: /forget <chain-id> [--force].",
        )
    force = False
    chain_id: str | None = None
    for token in args:
        if token == "--force":
            if force:
                raise DomainCommandError(
                    "slash_arguments_invalid",
                    "--force may be supplied only once.",
                )
            force = True
        elif token.startswith("-"):
            raise DomainCommandError(
                "slash_arguments_invalid",
                "Desktop /forget accepts only the optional --force flag.",
            )
        elif chain_id is None:
            chain_id = _safe_identifier(token)
        else:
            raise DomainCommandError(
                "slash_arguments_invalid",
                "Desktop /forget requires one exact chain id, not a name query.",
            )
    if not chain_id:
        raise DomainCommandError(
            "slash_arguments_invalid",
            "Desktop /forget requires one exact chain id.",
        )
    return chain_id, force


def _parse_deployments(args: list[str]) -> tuple[str, str | None]:
    if not args:
        return "list", None
    action = args[0].lower()
    if action == "list":
        if len(args) != 1:
            raise DomainCommandError(
                "slash_arguments_invalid",
                "Desktop /deployments list accepts no target.",
            )
        return action, None
    if action not in {"undeploy", "reload", "docs"} or len(args) != 2:
        raise DomainCommandError(
            "slash_arguments_invalid",
            "Usage: /deployments [list|undeploy <name>|reload <name>|docs <name>].",
        )
    name = str(args[1]).strip()
    if not _DEPLOYMENT_RE.fullmatch(name):
        raise DomainCommandError(
            "slash_arguments_invalid",
            "Deployment names may use only letters, digits, '.', '_', and '-'.",
        )
    return action, name


def _option_value(
    args: list[str],
    index: int,
    option: str,
) -> tuple[str, int]:
    token = args[index]
    if token.startswith(option + "="):
        value = token[len(option) + 1 :]
        if not value:
            raise DomainCommandError(
                "slash_arguments_invalid",
                f"{option} requires a value.",
            )
        return value, index + 1
    if index + 1 >= len(args):
        raise DomainCommandError(
            "slash_arguments_invalid",
            f"{option} requires a value.",
        )
    return args[index + 1], index + 2


def _resolve_chain(client: Any, ref: str, *, channel: str) -> tuple[str, Any]:
    getter = getattr(client, "get_chain_record", None)
    if not callable(getter):
        raise DomainCommandError(
            "domain_capability_missing",
            "This Memory build does not expose chain-version lookup.",
            status=501,
        )
    try:
        direct = getter(ref, channel=channel)
    except Exception:  # unknown id and transport both fall through to name lookup
        direct = None
    if direct is not None:
        entity_id = _safe_identifier(_field(direct, "entity_id", None) or ref)
        if entity_id:
            return entity_id, direct

    lister = getattr(client, "list_chains", None)
    if not callable(lister):
        raise DomainCommandError(
            "chain_not_found",
            f"Could not resolve {ref!r} to a saved chain.",
            status=404,
        )
    try:
        hits = list(lister(limit=5, q=ref) or [])
    except Exception as exc:  # noqa: BLE001
        raise _domain_failure("Could not resolve the chain reference.", exc) from exc
    if not hits:
        raise DomainCommandError(
            "chain_not_found",
            f"Could not resolve {ref!r} to a saved chain.",
            status=404,
        )
    if len(hits) != 1:
        names = [
            _safe_text(
                _field(item, "display_name", None)
                or _field(item, "name", None)
                or "unnamed",
                max_chars=80,
            )
            for item in hits[:5]
        ]
        raise DomainCommandError(
            "chain_reference_ambiguous",
            f"{ref!r} matches several chains ({', '.join(names)}); use an exact id.",
            status=409,
        )
    entity_id = _safe_identifier(_field(hits[0], "entity_id", ""))
    if not entity_id:
        raise DomainCommandError(
            "chain_reference_invalid",
            "The unique name match did not contain a valid chain id.",
            status=502,
        )
    try:
        record = getter(entity_id, channel=channel)
    except Exception as exc:  # noqa: BLE001
        raise _domain_failure(
            "Could not load the resolved chain version.", exc
        ) from exc
    if record is None:
        raise DomainCommandError(
            "chain_channel_missing",
            f"Chain {entity_id} has no version on channel {channel!r}.",
            status=404,
        )
    return entity_id, record


def _list_versions(client: Any, entity_id: str) -> list[Any]:
    lister = getattr(client, "list_versions", None)
    if not callable(lister):
        raise DomainCommandError(
            "domain_capability_missing",
            "This Memory build does not expose version history.",
            status=501,
        )
    return list(lister(entity_id, entity_type="chain", limit=MAX_VERSION_ROWS) or [])


def _channel_versions(client: Any, entity_id: str) -> dict[str, list[str]]:
    output: dict[str, list[str]] = {}
    for channel in ("latest", "stable"):
        try:
            record = client.get_chain_record(entity_id, channel=channel)
        except Exception:  # channel markers are best-effort, matching the TUI
            continue
        version_id = _safe_identifier(_field(record, "version_id", ""))
        if version_id:
            output.setdefault(version_id, []).append(channel)
    return output


def _version_payload(version: Any, channels: dict[str, list[str]]) -> dict[str, Any]:
    version_id = _safe_identifier(_field(version, "version_id", ""))
    evolution = _field(version, "evolution_meta", {}) or {}
    score = evolution.get("fitness_score") if isinstance(evolution, Mapping) else None
    return {
        "version_number": _optional_int(_field(version, "version_number", None)),
        "version_id": version_id,
        "created_at": _safe_text(_field(version, "created_at", ""), max_chars=64)
        or None,
        "fitness_score": _safe_number(score),
        "change_summary": _safe_text(
            _field(version, "change_summary", ""), max_chars=256
        )
        or None,
        "channels": list(channels.get(version_id, ())),
    }


def _format_version_line(row: Mapping[str, Any]) -> str:
    number = row.get("version_number")
    parts = [
        f"v{number if number is not None else '?'}",
        str(row.get("version_id") or "")[:12],
    ]
    if row.get("created_at"):
        parts.append(str(row["created_at"])[:10])
    if row.get("fitness_score") is not None:
        parts.append(f"fitness {row['fitness_score']}")
    if row.get("change_summary"):
        parts.append(str(row["change_summary"])[:50])
    line = "- " + " | ".join(parts)
    if row.get("channels"):
        line += " <- " + ", ".join(str(item) for item in row["channels"])
    return line


def _display_name(record: Any, entity_id: str) -> str:
    meta = _field(record, "meta", {}) or {}
    content = _field(record, "content", {}) or {}
    value = (
        (meta.get("display_name") if isinstance(meta, Mapping) else None)
        or (content.get("name") if isinstance(content, Mapping) else None)
        or entity_id
    )
    return _safe_text(value, max_chars=256)


def _chain_exists(memory: Any, chain_id: str) -> bool:
    getter = getattr(memory, "get_chain", None)
    if not callable(getter):
        client = getattr(memory, "client", None) or memory
        record_getter = getattr(client, "get_chain_record", None)
        if not callable(record_getter):
            raise DomainCommandError(
                "domain_capability_missing",
                "This Memory build cannot verify saved chains.",
                status=501,
            )
        try:
            return record_getter(chain_id, channel="latest") is not None
        except (KeyError, LookupError):
            return False
        except Exception as exc:  # noqa: BLE001
            raise _domain_failure("Could not verify the saved chain.", exc) from exc
    try:
        return getter(chain_id) is not None
    except (KeyError, LookupError):
        return False
    except Exception as exc:  # noqa: BLE001
        raise _domain_failure("Could not verify the saved chain.", exc) from exc


def _dataset_entries(memory: Any, chain_id: str) -> list[dict[str, Any]]:
    lister = getattr(memory, "list_entities", None)
    if not callable(lister):
        raise DomainCommandError(
            "domain_capability_missing",
            "This Memory build cannot list linked dataset cards.",
            status=501,
        )
    try:
        rows = list(lister(entity_type="memory_card", limit=MAX_DATASET_ROWS) or [])
    except Exception as exc:  # noqa: BLE001
        raise _domain_failure("Could not inspect linked dataset cards.", exc) from exc
    tag = f"dataset-entry:{chain_id}"
    output: list[dict[str, Any]] = []
    for row in rows[:MAX_DATASET_ROWS]:
        tags = _field(row, "tags", None)
        if not tags:
            meta = _field(row, "meta", {}) or {}
            tags = meta.get("tags", ()) if isinstance(meta, Mapping) else ()
        if tag not in (tags or ()):
            continue
        entity_id = _safe_identifier(
            _field(row, "entity_id", None) or _field(row, "id", "")
        )
        if entity_id:
            output.append({"entity_id": entity_id})
    return output


def _delete_entity(memory: Any, entity_type: str, entity_id: str) -> bool:
    client = getattr(memory, "client", None) or memory
    private = getattr(client, "_delete_entity", None)
    if callable(private):
        result = private(entity_type, entity_id)
        return True if result is None else bool(result)
    public = getattr(memory, "delete_entity", None)
    if callable(public):
        return bool(public(entity_id, entity_type=entity_type))
    raise DomainCommandError(
        "domain_capability_missing",
        "This Memory build does not expose soft-delete.",
        status=501,
    )


def _require_hub_health(hub: Any) -> None:
    try:
        health = _run_async(hub.health())
    except Exception as exc:  # noqa: BLE001
        raise _domain_failure("Could not reach the configured agent hub.", exc) from exc
    if health is None:
        raise DomainCommandError(
            "hub_unavailable",
            "The configured agent hub is not running.",
            status=503,
        )


def _deployment_payload(hub: Any, deployment: Any) -> dict[str, Any]:
    name = _safe_text(_field(deployment, "name", ""), max_chars=128)
    return {
        "name": name,
        "display_name": _safe_text(
            _field(deployment, "display_name", ""), max_chars=256
        ),
        "version": _safe_text(_field(deployment, "version", ""), max_chars=128),
        "ready": bool(_field(deployment, "ready", False)),
        "ready_reason": _safe_text(
            _field(deployment, "ready_reason", ""), max_chars=500
        ),
        "entity_id": _safe_identifier(_field(deployment, "entity_id", "")) or None,
        "channel": _safe_text(_field(deployment, "channel", ""), max_chars=64) or None,
        "source": _safe_text(_field(deployment, "source", ""), max_chars=64),
        "deployed_at": _safe_text(_field(deployment, "deployed_at", ""), max_chars=64),
        "runs": _safe_int(_field(deployment, "runs", 0)),
        "agent_url": _safe_url(hub.agent_url(name)) if name else "",
        "docs_url": _safe_url(hub.docs_url(name)) if name else "",
    }


def _format_deployment_line(row: Mapping[str, Any]) -> str:
    status = (
        "ready"
        if row.get("ready")
        else f"not ready: {row.get('ready_reason') or 'unknown'}"
    )
    return (
        f"- {row.get('name')} | {row.get('display_name')} | "
        f"{row.get('version')} | {status} | runs {row.get('runs', 0)} | "
        f"docs {row.get('docs_url')}"
    )


def _safe_url(value: Any) -> str:
    """Return an http(s) URL without user-info, query, or fragment."""

    try:
        parsed = urlsplit(str(value or ""))
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            return ""
        host = parsed.hostname
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        port = f":{parsed.port}" if parsed.port is not None else ""
        return urlunsplit((parsed.scheme, host + port, parsed.path, "", ""))
    except (TypeError, ValueError):
        return ""


def _run_async(awaitable: Any) -> Any:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(awaitable)
    close = getattr(awaitable, "close", None)
    if callable(close):
        close()
    raise DomainCommandError(
        "async_context_unsupported",
        "This desktop domain command must run outside an active event loop.",
        status=500,
    )


def _safe_json_projection(value: Any, *, max_chars: int) -> tuple[Any, bool]:
    projected = _safe_value(value, depth=0)
    rendered = json.dumps(
        projected,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    if len(rendered) <= max_chars:
        return projected, False
    return {
        "summary": _safe_content(rendered, max_chars=max_chars),
        "truncated": True,
    }, True


def _safe_mapping(value: Mapping[str, Any]) -> dict[str, Any]:
    projected = _safe_value(dict(value), depth=0)
    return projected if isinstance(projected, dict) else {}


def _safe_value(value: Any, *, depth: int) -> Any:
    if depth >= 6:
        return "[depth limit]"
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return _safe_number(value)
    if isinstance(value, str):
        return _safe_content(value, max_chars=MAX_SAFE_VALUE_CHARS)
    if isinstance(value, Mapping):
        output: dict[str, Any] = {}
        for index, (key, item) in enumerate(value.items()):
            if index >= 200:
                output["__truncated__"] = True
                break
            safe_key = _safe_text(key, max_chars=128)
            if _looks_secret_key(safe_key):
                output[safe_key] = "[redacted]"
            else:
                output[safe_key] = _safe_value(item, depth=depth + 1)
        return output
    if isinstance(value, (list, tuple, set, frozenset)):
        rows = list(value)
        return [_safe_value(item, depth=depth + 1) for item in rows[:500]]
    if hasattr(value, "model_dump"):
        try:
            return _safe_value(value.model_dump(mode="json"), depth=depth + 1)
        except Exception:  # noqa: BLE001
            pass
    return _safe_text(value, max_chars=MAX_SAFE_VALUE_CHARS)


def _looks_secret_key(value: str) -> bool:
    normalised = value.lower().replace("-", "_")
    return any(
        token in normalised
        for token in (
            "api_key",
            "token",
            "password",
            "passwd",
            "secret",
            "authorization",
        )
    )


def _safe_content(value: Any, *, max_chars: int) -> str:
    text = redact_sensitive_text(value, max_chars=max_chars)
    text = _CONTEXT_PATH_LABEL_RE.sub(r"\1", text)
    text = _ABSOLUTE_PATH_RE.sub("[path]", text)
    return text[:max_chars]


def _safe_text(value: Any, *, max_chars: int) -> str:
    text = _safe_content(value, max_chars=max_chars)
    return _CONTROL_RE.sub(" ", text).strip()[:max_chars]


def _safe_identifier(value: Any) -> str:
    text = _safe_text(value, max_chars=MAX_REFERENCE_CHARS)
    return text if _ENTITY_ID_RE.fullmatch(text) else ""


def _field(value: Any, name: str, default: Any = None) -> Any:
    if isinstance(value, Mapping):
        return value.get(name, default)
    return getattr(value, name, default)


def _safe_int(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError, OverflowError):
        return 0


def _optional_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return None


def _safe_number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if number == number and abs(number) != float("inf") else None


def _domain_failure(message: str, _exc: BaseException) -> DomainCommandError:
    return DomainCommandError("domain_operation_failed", message, status=502)


__all__ = [
    "DesktopDomainCommands",
    "DesktopDomainSources",
    "DomainCommandError",
    "DomainCommandPlan",
    "DomainCommandResult",
    "DomainRisk",
]
