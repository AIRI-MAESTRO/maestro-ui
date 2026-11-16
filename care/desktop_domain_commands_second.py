"""UI-neutral native adapters for the second TUI domain tranche.

The webview command bridge cannot treat equal CLI/TUI spellings as semantic
parity.  These adapters reuse the data paths behind the TUI for library,
marketplace, long-term memory, run inspection/execution, deploy, and promote.

Every mutating/provider plan is bound to a digest of the exact resolved state.
The bridge recomputes that digest when a confirmation token is submitted, so a
changed chain version, target channel, marketplace listing, or LTM snapshot
invalidates the old token.  Planning never calls a model, runtime, hub, or
promotion gate; those effects live exclusively in the confirmed closure.
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import ipaddress
import json
import os
import re
import secrets
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from care.desktop_domain_commands import (
    DomainCommandError,
    DomainCommandPlan,
    DomainCommandResult,
)
from care.redaction import redact_sensitive_text
from care.runtime.workspace_context import workspace_path


MAX_ROWS = 50
MAX_QUERY_CHARS = 512
MAX_NOTE_CHARS = 8_000
MAX_TASK_CHARS = 16_000
MAX_INPUT_VALUE_CHARS = 32_000
MAX_OUTPUT_CHARS = 64 * 1024
MAX_SAFE_VALUE_CHARS = 4_000
MAX_LTM_FILE_CHARS = 1_000_000

_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$")
_CHANNEL_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
_DEPLOYMENT_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,62}$")
_INPUT_KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_CREDENTIAL_RE = re.compile(r"^[A-Za-z0-9_-]{20,128}$")
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


class SecondDomainRisk(str, Enum):
    """Risk values intentionally match :class:`CommandRisk` values."""

    READ_ONLY = "read_only"
    FILESYSTEM_MUTATION = "filesystem_write_or_mutation"
    EXTERNAL_SIDE_EFFECT = "external_side_effect"
    PROVIDER_EXECUTION = "provider_or_tool_execution"


def _default_load_config() -> Any:
    from care.config import CareConfig

    return CareConfig.load()


def _default_build_memory(config: Any) -> Any:
    from care.memory import CareMemory

    return CareMemory.from_config(config)


def _default_build_ltm(config: Any) -> Any | None:
    from care.memory_ltm import build_ltm

    return build_ltm(config)


def _default_ltm_session_id(config: Any) -> str:
    from care.memory_ltm import ltm_session_id

    return ltm_session_id(config)


def _default_recall_ltm(ltm: Any, session_id: str, *, max_chars: int) -> str:
    from care.memory_ltm import recall_digest

    return recall_digest(ltm, session_id, max_chars=max_chars)


def _default_inspect_ltm(
    config: Any,
    session_id: str,
    *,
    max_chars: int,
) -> str | None:
    """Read the configured JSON LTM without constructing its mkdir-on-init store.

    ``JsonFileLTM`` creates its directory in ``__init__``.  A confirmation plan
    must be observational, so inspect the current session file directly and
    defer store construction until the confirmed closure runs.
    """

    context = getattr(config, "context", None)
    if context is None or not getattr(context, "ltm_enabled", False):
        return None
    try:
        import mmar_carl  # noqa: F401 - availability check without construction
    except Exception:
        return None
    directory_raw = os.environ.get("CARE_CONTEXT__LTM_DIR") or str(
        getattr(context, "ltm_dir", "~/.config/care/ltm")
    )
    safe_session = session_id.replace("/", "_").replace("\\", "_") or "_global"
    path = workspace_path(directory_raw).resolve() / f"{safe_session}.json"
    if not path.is_file():
        return ""
    try:
        with path.open("r", encoding="utf-8") as stream:
            raw = stream.read(MAX_LTM_FILE_CHARS + 1)
    except OSError:
        return ""
    if len(raw) > MAX_LTM_FILE_CHARS:
        raise ValueError("long-term memory snapshot exceeds its safety limit")
    try:
        values = json.loads(raw)
    except json.JSONDecodeError:
        return ""
    if not isinstance(values, Mapping):
        return ""
    lines = [
        f"- {key}: {value}" for key, value in values.items() if value not in (None, "")
    ]
    if not lines:
        return ""
    block = "## What I remember about you (long-term memory)\n" + "\n".join(lines)
    return block[:max_chars].rstrip() + (" …" if len(block) > max_chars else "")


def _default_build_ltm_complete(config: Any) -> Callable[[str, str], str] | None:
    """Build the same JSON-mode completion callable as the TUI.

    Client construction is local and does not send a request.  The returned
    callable is invoked only by a confirmed ``/remember`` execution.
    """

    mage = getattr(config, "mage", None)
    if mage is None or not (
        getattr(mage, "api_key", None) or getattr(mage, "base_url", None)
    ):
        return None
    try:
        from care.runtime.llm_client import build_llm_client

        client = build_llm_client(mage)
    except Exception:  # noqa: BLE001 - raw fallback is the TUI contract
        return None
    model = getattr(mage, "model", "") or "gpt-4o-mini"

    def complete(system: str, user: str) -> str:
        response = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            response_format={"type": "json_object"},
            temperature=0,
        )
        return response.choices[0].message.content or ""

    return complete


def _default_marketplace_search(client: Any, query: str) -> Any:
    from care.marketplace import search_marketplace

    return search_marketplace(client, query, top_k=MAX_ROWS)


def _default_validate_chain(chain: Mapping[str, Any]) -> Any:
    from care.preflight import validate_chain

    return validate_chain(json.dumps(dict(chain), ensure_ascii=False))


def _default_run_chain(
    config: Any,
    memory: Any,
    chain_id: str,
    chain: dict[str, Any],
    *,
    task: str | None,
    inputs: dict[str, str],
    save_result: str | None,
) -> dict[str, Any]:
    """Execute through the same CARL and run-recording helpers as CLI run."""

    from care import cli as care_cli

    override = getattr(care_cli, "_BUILD_CARL_EXECUTOR_OVERRIDE", None)
    executor = (
        care_cli._build_carl_executor()
        if override is not None
        else _build_carl_executor_from_config(config)
    )
    history_chain = _history_chain_projection(chain)
    history_task = _text(task or "", 200)
    started_at = time.time()
    try:
        result = _run_async(
            executor(copy.deepcopy(chain), task=task, inputs=dict(inputs))
        )
    except Exception as exc:
        care_cli._record_cli_run(
            chain_dict=history_chain,
            chain_id=chain_id,
            task=history_task,
            result=None,
            started_at=started_at,
            duration=time.time() - started_at,
            status="failure",
            error=_text(exc, 2_000),
        )
        raise

    success = bool(getattr(result, "success", False))
    care_cli._record_cli_run(
        chain_dict=history_chain,
        chain_id=chain_id,
        task=history_task,
        result=_history_result_projection(result),
        started_at=started_at,
        duration=time.time() - started_at,
        status="success" if success else "failure",
        error="" if success else "chain reported failure",
    )
    saved_result_id: str | None = None
    if save_result and success:
        saved_result_id = care_cli._persist_run_result(
            memory=memory,
            chain_id=chain_id,
            chain_dict=chain,
            result=result,
            name=save_result,
            task=task,
            inputs=inputs,
        )
    return {
        "success": success,
        "steps_completed": len(getattr(result, "step_results", None) or []),
        "final_answer": (str(getattr(result, "final_answer", "") or "") or None),
        "saved_result_id": saved_result_id,
    }


def _build_carl_executor_from_config(config: Any) -> Callable[..., Any]:
    """Build the CLI-compatible executor from the confirmed config snapshot."""

    from care import cli as care_cli

    try:
        from mmar_carl import (
            OpenAIClientConfig,
            OpenAICompatibleClient,
            ReasoningChain,
            ReasoningContext,
        )
    except Exception as exc:
        raise care_cli.CliCarlError(
            f"mmar_carl isn't installed: {exc}. Install with `pip install 'care[carl]'`."
        ) from exc
    mage = getattr(config, "mage", None)
    api_key = getattr(mage, "api_key", None)
    if not api_key:
        raise care_cli.CliCarlError(
            "mage.api_key isn't set — `care run --execute` needs an LLM API key."
        )
    try:
        api = OpenAICompatibleClient(
            OpenAIClientConfig(
                api_key=api_key,
                model=getattr(mage, "model", None) or "openai/gpt-4o",
                base_url=(
                    getattr(mage, "base_url", None) or "https://openrouter.ai/api/v1"
                ),
            )
        )
    except Exception as exc:
        raise care_cli.CliCarlError(
            "failed to construct the confirmed CARL LLM client"
        ) from exc

    async def execute(
        chain_dict: dict[str, Any],
        *,
        task: str | None = None,
        inputs: dict[str, str] | None = None,
    ) -> Any:
        chain = ReasoningChain.from_dict(chain_dict, use_typed_steps=True)
        kwargs: dict[str, Any] = {"api": api}
        if task is not None:
            kwargs["outer_context"] = task
        if inputs:
            kwargs["files"] = dict(inputs)
        context = ReasoningContext.from_chain_inputs(chain, **kwargs)
        return await chain.execute_async(context)

    return execute


@dataclass(frozen=True)
class _HistoryResult:
    success: bool
    final_answer: str
    step_count: int
    token_usage: dict[str, int]

    @property
    def step_results(self) -> tuple[None, ...]:
        return (None,) * min(self.step_count, 10_000)

    def to_dict(self) -> dict[str, Any]:
        return {
            "success": self.success,
            "final_answer": self.final_answer,
            "steps_completed": self.step_count,
            "token_usage": dict(self.token_usage),
        }


def _history_result_projection(result: Any) -> _HistoryResult:
    raw_steps = getattr(result, "step_results", None)
    try:
        step_count = min(max(0, len(raw_steps or ())), 10_000)
    except (TypeError, OverflowError):
        step_count = 0
    raw_usage = (
        getattr(result, "usage", None) or getattr(result, "token_usage", None) or {}
    )
    usage: dict[str, int] = {}
    if isinstance(raw_usage, Mapping):
        for source, target in (
            ("prompt", "prompt"),
            ("completion", "completion"),
            ("prompt_tokens", "prompt"),
            ("completion_tokens", "completion"),
        ):
            if target in usage or source not in raw_usage:
                continue
            try:
                usage[target] = max(0, int(raw_usage[source]))
            except (TypeError, ValueError, OverflowError):
                continue
    return _HistoryResult(
        success=bool(getattr(result, "success", False)),
        final_answer=_text(getattr(result, "final_answer", "") or "", 2_000),
        step_count=step_count,
        token_usage=usage,
    )


def _history_chain_projection(chain: Mapping[str, Any]) -> dict[str, Any]:
    metadata = chain.get("metadata") if isinstance(chain, Mapping) else None
    metadata = metadata if isinstance(metadata, Mapping) else {}
    care_metadata = metadata.get("care")
    care_metadata = care_metadata if isinstance(care_metadata, Mapping) else {}
    display_name = (
        care_metadata.get("display_name")
        or metadata.get("display_name")
        or chain.get("name")
        or ""
    )
    return {
        "metadata": {
            "care": {"display_name": _text(display_name, 200)},
        }
    }


def _default_bundle_tools(chain: dict[str, Any], config: Any) -> list[dict[str, Any]]:
    from care.tool_synthesis import bundled_tools_for_chain

    return list(bundled_tools_for_chain(chain, config) or [])


def _default_deploy_gate(
    chain: dict[str, Any], bundled_names: frozenset[str]
) -> list[str]:
    from care.runtime.deploy_gate import gate_chain_for_deploy

    return list(gate_chain_for_deploy(chain, bundled_names) or [])


def _default_ensure_hub(config: Any) -> Any:
    from care.runtime.agent_hub import ensure_hub_running, hub_env

    return _run_async(ensure_hub_running(config.hub, env=hub_env(config)))


def _default_promotion_gate(
    memory: Any,
    config: Any,
    entity_id: str,
    *,
    from_channel: str,
    to_channel: str,
) -> Any:
    from care.runtime.promote_gate import gate_promotion

    return _run_async(
        gate_promotion(
            memory,
            config,
            entity_id,
            from_channel=from_channel,
            to_channel=to_channel,
        )
    )


@dataclass(frozen=True)
class DesktopSecondDomainSources:
    """All I/O seams; tests replace them with hermetic fakes."""

    load_config: Callable[[], Any] = _default_load_config
    build_memory: Callable[[Any], Any] = _default_build_memory
    build_ltm: Callable[[Any], Any | None] = _default_build_ltm
    ltm_session_id: Callable[[Any], str] = _default_ltm_session_id
    recall_ltm: Callable[..., str] = _default_recall_ltm
    inspect_ltm: Callable[..., str | None] = _default_inspect_ltm
    build_ltm_complete: Callable[[Any], Callable[[str, str], str] | None] = (
        _default_build_ltm_complete
    )
    marketplace_search: Callable[[Any, str], Any] = _default_marketplace_search
    validate_chain: Callable[[Mapping[str, Any]], Any] = _default_validate_chain
    run_chain: Callable[..., Mapping[str, Any]] = _default_run_chain
    bundle_tools: Callable[[dict[str, Any], Any], list[dict[str, Any]]] = (
        _default_bundle_tools
    )
    deploy_gate: Callable[[dict[str, Any], frozenset[str]], list[str]] = (
        _default_deploy_gate
    )
    ensure_hub: Callable[[Any], Any] = _default_ensure_hub
    promotion_gate: Callable[..., Any] = _default_promotion_gate
    token_factory: Callable[[], str] = lambda: "maestro_" + secrets.token_urlsafe(24)


class DesktopSecondDomainCommands:
    """Plan the six reviewed second-tranche slash adapters."""

    NAMES = frozenset(
        {"library", "marketplace", "remember", "run", "deploy", "promote"}
    )

    def __init__(self, sources: DesktopSecondDomainSources | None = None) -> None:
        self._sources = sources or DesktopSecondDomainSources()

    def plan(self, name: str, args: Sequence[str]) -> DomainCommandPlan:
        command = str(name or "").lower()
        tokens = [str(value) for value in args]
        if command not in self.NAMES:
            raise DomainCommandError(
                "slash_command_unavailable",
                f"Desktop /{command or '?'} has no reviewed second-domain adapter.",
                status=501,
            )
        try:
            if command == "library":
                return self._plan_library(tokens)
            if command == "marketplace":
                return self._plan_marketplace(tokens)
            if command == "remember":
                return self._plan_remember(tokens)
            if command == "run":
                return self._plan_run(tokens)
            if command == "deploy":
                return self._plan_deploy(tokens)
            return self._plan_promote(tokens)
        except DomainCommandError:
            raise
        except Exception as exc:  # noqa: BLE001 - source errors stay opaque
            raise DomainCommandError(
                "domain_unavailable",
                f"Desktop /{command} data is temporarily unavailable.",
                status=503,
            ) from exc

    def _config_memory(self) -> tuple[Any, Any, Any]:
        config = self._sources.load_config()
        memory = self._sources.build_memory(config)
        client = getattr(memory, "client", None) or memory
        return config, memory, client

    def _require_runtime_unchanged(
        self,
        expected: str,
        *sections: str,
    ) -> None:
        try:
            current = self._sources.load_config()
            actual = _runtime_binding(current, *sections)
        except Exception as exc:  # noqa: BLE001 - never reflect config details
            raise _operation_failed(
                "Runtime configuration could not be rechecked.", exc
            ) from exc
        if not secrets.compare_digest(actual, expected):
            raise DomainCommandError(
                "runtime_state_changed",
                "Runtime settings changed after confirmation; review and confirm again.",
                status=409,
            )

    # ------------------------------------------------------------------
    # /library [query]
    # ------------------------------------------------------------------
    def _plan_library(self, args: list[str]) -> DomainCommandPlan:
        query = _free_text(" ".join(args), label="Library query", allow_empty=True)

        def execute() -> DomainCommandResult:
            _config, _memory, client = self._config_memory()
            lister = getattr(client, "list_chains", None)
            if not callable(lister):
                raise DomainCommandError(
                    "domain_capability_missing",
                    "This Memory build does not expose the chain library.",
                    status=501,
                )
            try:
                source_rows = list(
                    lister(
                        limit=MAX_ROWS,
                        offset=0,
                        channel="latest",
                        namespace=None,
                        tags=None,
                        q=query or None,
                        favourites_only=None,
                        sort_by="created_at",
                        sort_dir="desc",
                    )
                    or []
                )
            except Exception as exc:  # noqa: BLE001
                raise _operation_failed("Library lookup failed.", exc) from exc

            from care.runtime.library_view import parse_library_row

            rows: list[dict[str, Any]] = []
            for source in source_rows[:MAX_ROWS]:
                try:
                    row = parse_library_row(source)
                except Exception:  # malformed rows are skipped, not reflected
                    continue
                rows.append(
                    {
                        "entity_id": _identifier(row.entity_id),
                        "display_name": _text(row.label, 200),
                        "description": _text(row.description, 600),
                        "domain": _text(row.domain, 120),
                        "steps": row.step_count,
                        "favourite": bool(row.favourite),
                        "tags": [_text(tag, 120) for tag in row.tags[:30]],
                        "run_count": max(0, int(row.run_count)),
                        "fitness": row.fitness,
                        "status": row.status,
                        "channel": _channel(row.channel),
                    }
                )
            lines = [
                f"Library{f' matching {query!r}' if query else ''}: {len(rows)} chain(s)"
            ]
            lines.extend(
                f"- {row['display_name']} [{row['entity_id']}] · "
                f"{row['steps'] if row['steps'] is not None else '?'} step(s)"
                for row in rows
            )
            if not rows:
                lines.append("No saved chains matched.")
            return _result(
                "\n".join(lines) + "\n",
                {
                    "action": "search" if query else "browse",
                    "query": query,
                    "rows": rows,
                    "count": len(rows),
                    "truncated": len(source_rows) > MAX_ROWS,
                },
            )

        return _read_plan("library", {"query": query}, execute)

    # ------------------------------------------------------------------
    # /marketplace [query] | install <entity-id> [--name NAME]
    # ------------------------------------------------------------------
    def _plan_marketplace(self, args: list[str]) -> DomainCommandPlan:
        if args and args[0].lower() == "install":
            return self._plan_marketplace_install(args[1:])
        query = _free_text(" ".join(args), label="Marketplace query", allow_empty=True)

        def execute() -> DomainCommandResult:
            _config, _memory, client = self._config_memory()
            if query:
                try:
                    result = self._sources.marketplace_search(client, query)
                    source_rows = list(getattr(result, "listings", ()) or ())
                except Exception as exc:  # noqa: BLE001
                    raise _operation_failed("Marketplace search failed.", exc) from exc
                action = "search"
            else:
                lister = getattr(client, "list_agent_skills", None)
                if not callable(lister):
                    raise DomainCommandError(
                        "domain_capability_missing",
                        "This Memory build does not expose marketplace browsing.",
                        status=501,
                    )
                try:
                    source_rows = list(
                        lister(
                            limit=MAX_ROWS,
                            offset=0,
                            channel="latest",
                            namespace=None,
                            tags=None,
                            q=None,
                            favourites_only=None,
                            sort_by="created_at",
                            sort_dir="desc",
                        )
                        or []
                    )
                except Exception as exc:  # noqa: BLE001
                    raise _operation_failed("Marketplace browse failed.", exc) from exc
                action = "browse"
            rows = [_marketplace_row(item) for item in source_rows[:MAX_ROWS]]
            rows = [row for row in rows if row["entity_id"]]
            lines = [
                f"Marketplace{f' matching {query!r}' if query else ''}: {len(rows)} skill(s)"
            ]
            lines.extend(
                f"- {row['name']} [{row['entity_id']}]"
                + (f" · score {row['score']:.3f}" if row["score"] is not None else "")
                for row in rows
            )
            if not rows:
                lines.append("No shared skills matched.")
            lines.append("Install: /marketplace install <entity-id> [--name NAME]")
            return _result(
                "\n".join(lines) + "\n",
                {
                    "action": action,
                    "query": query,
                    "rows": rows,
                    "count": len(rows),
                    "truncated": len(source_rows) > MAX_ROWS,
                    "install_form": "install <entity-id> [--name NAME]",
                },
            )

        return _read_plan("marketplace", {"query": query}, execute)

    def _plan_marketplace_install(self, args: list[str]) -> DomainCommandPlan:
        entity_id, requested_name = _parse_marketplace_install(args)
        config, _memory, client = self._config_memory()
        runtime_binding = _runtime_binding(config, "memory")
        source = _marketplace_source(client, entity_id)
        source_row = _marketplace_row(source)
        name = requested_name or source_row["name"] or entity_id
        preview = {
            "action": "install",
            "entity_id": entity_id,
            "name": _text(name, 200),
            "source_version": _identifier(_field(source, "version_id", "")) or None,
        }

        def execute() -> DomainCommandResult:
            self._require_runtime_unchanged(runtime_binding, "memory")
            saver = getattr(client, "save_agent_skill", None)
            if not callable(saver):
                raise DomainCommandError(
                    "domain_capability_missing",
                    "This Memory build does not expose marketplace install.",
                    status=501,
                )
            try:
                response = saver(entity_id=entity_id, name=name)
            except Exception as exc:  # noqa: BLE001
                raise _operation_failed("Marketplace install failed.", exc) from exc
            saved_id = _identifier(
                _field(response, "entity_id", None)
                or _field(response, "id", None)
                or (response if isinstance(response, str) else None)
                or entity_id
            )
            return _result(
                f"Installed marketplace skill {name} [{saved_id or entity_id}].\n",
                {**preview, "saved_entity_id": saved_id or entity_id},
            )

        return _mutation_plan(
            "marketplace",
            SecondDomainRisk.EXTERNAL_SIDE_EFFECT,
            "This installs the selected shared skill into your Maestro library.",
            (f"Install marketplace skill {name!r} ({entity_id}).",),
            {
                "entity_id": entity_id,
                "name": name,
                "source": _state_digest(source),
                "runtime": runtime_binding,
            },
            preview,
            execute,
        )

    # ------------------------------------------------------------------
    # /remember <note>
    # ------------------------------------------------------------------
    def _plan_remember(self, args: list[str]) -> DomainCommandPlan:
        note = _free_text(
            " ".join(args), label="Note", allow_empty=False, cap=MAX_NOTE_CHARS
        )
        config = self._sources.load_config()
        session_id = _text(self._sources.ltm_session_id(config), 256) or "default"
        try:
            existing = self._sources.inspect_ltm(config, session_id, max_chars=12_000)
        except Exception as exc:  # noqa: BLE001
            raise _operation_failed("Could not inspect long-term memory.", exc) from exc
        if existing is None:
            raise DomainCommandError(
                "memory_disabled", "Long-term memory is disabled.", status=409
            )
        runtime_binding = _runtime_binding(config, "mage", "context")
        complete = self._sources.build_ltm_complete(config)
        uses_provider = callable(complete)
        mode = "llm_merge_or_raw_fallback" if uses_provider else "raw_fallback"
        risk = (
            SecondDomainRisk.PROVIDER_EXECUTION
            if uses_provider
            else SecondDomainRisk.FILESYSTEM_MUTATION
        )
        preview = {
            "action": "remember",
            "session_id": session_id,
            "mode": mode,
            "note_chars": len(note),
        }

        def execute() -> DomainCommandResult:
            from care.memory_ltm import remember_text

            self._require_runtime_unchanged(runtime_binding, "mage", "context")
            ltm = self._sources.build_ltm(config)
            if ltm is None:
                raise DomainCommandError(
                    "memory_disabled", "Long-term memory is disabled.", status=409
                )
            merger = complete if callable(complete) else (lambda _system, _user: "")
            try:
                saved = remember_text(
                    ltm,
                    session_id,
                    content=note,
                    complete=merger,
                    existing_digest=existing,
                )
            except Exception as exc:  # noqa: BLE001
                raise _operation_failed("Could not remember that note.", exc) from exc
            keys = [_text(_field(item, "key", ""), 128) for item in saved]
            keys = [key for key in keys if key]
            return _result(
                ("Remembered: " + ", ".join(keys) + ".\n")
                if keys
                else "The note was already represented in long-term memory.\n",
                {**preview, "saved_keys": keys, "saved_count": len(keys)},
            )

        effects = ["Write reconciled facts to persistent long-term memory."]
        if uses_provider:
            effects.insert(0, "Call the configured model once to merge the note.")
        return _mutation_plan(
            "remember",
            risk,
            (
                "This calls the configured model and writes persistent memory."
                if uses_provider
                else "No model is configured; this stores a bounded raw-note fallback."
            ),
            tuple(effects),
            {
                "session_id": session_id,
                "existing_digest": hashlib.sha256(existing.encode()).hexdigest(),
                "note_digest": hashlib.sha256(note.encode()).hexdigest(),
                "mode": mode,
                "runtime": runtime_binding,
            },
            preview,
            execute,
        )

    # ------------------------------------------------------------------
    # /run <chain-id> [--channel C] [--execute ...]
    # ------------------------------------------------------------------
    def _plan_run(self, args: list[str]) -> DomainCommandPlan:
        options = _parse_run(args)
        config, memory, client = self._config_memory()
        runtime_binding = _runtime_binding(
            config,
            "mage",
            "memory",
            "tools",
            "sandbox",
        )
        record = _exact_chain_record(client, options["chain_id"], options["channel"])
        chain = _record_content(record)
        try:
            preflight = self._sources.validate_chain(chain)
        except Exception as exc:  # noqa: BLE001
            raise _operation_failed("Chain preflight failed.", exc) from exc
        preview = _run_preview(record, chain, preflight, options)

        def inspect() -> DomainCommandResult:
            return _result(_format_run_preview(preview), preview)

        if not options["execute"]:
            return _read_plan("run", preview, inspect)
        if not bool(getattr(preflight, "parsed", False)):
            raise DomainCommandError(
                "run_preflight_failed",
                "The chain is not parseable; execution is refused.",
                status=409,
            )

        def execute() -> DomainCommandResult:
            self._require_runtime_unchanged(
                runtime_binding,
                "mage",
                "memory",
                "tools",
                "sandbox",
            )
            try:
                outcome = dict(
                    self._sources.run_chain(
                        config,
                        memory,
                        options["chain_id"],
                        copy.deepcopy(chain),
                        task=options["task"],
                        inputs=dict(options["inputs"]),
                        save_result=options["save_result"],
                    )
                    or {}
                )
            except Exception as exc:  # noqa: BLE001
                raise _operation_failed("Chain execution failed.", exc) from exc
            safe_outcome = _project(outcome)
            success = bool(outcome.get("success", False))
            answer = _text(outcome.get("final_answer", "") or "", 2_000)
            stdout = (
                f"Executed {options['chain_id']}: "
                f"{'success' if success else 'failed'}; "
                f"{max(0, int(outcome.get('steps_completed', 0) or 0))} step(s).\n"
            )
            if answer:
                stdout += f"Final: {answer}\n"
            return _result(
                stdout,
                {**preview, "executed": True, "outcome": safe_outcome},
                ok=success,
                exit_code=0 if success else 1,
                stderr="" if success else "The chain runtime reported failure.\n",
            )

        effects = [
            "Call the configured provider and execute every tool in the chain.",
            "Write a bounded local run-history record.",
        ]
        if options["save_result"]:
            effects.append("Persist the successful result to Memory.")
        return _mutation_plan(
            "run",
            SecondDomainRisk.PROVIDER_EXECUTION,
            "This executes the resolved chain with the configured provider and tools.",
            tuple(effects),
            {
                "chain_id": options["chain_id"],
                "channel": options["channel"],
                "version_id": _field(record, "version_id", ""),
                "chain_digest": _state_digest(chain),
                "task_digest": _state_digest(options["task"]),
                "inputs_digest": _state_digest(options["inputs"]),
                "save_result": options["save_result"],
                "runtime": runtime_binding,
            },
            preview,
            execute,
        )

    # ------------------------------------------------------------------
    # /deploy <id|unique-name> [--channel C] [--name N]
    # ------------------------------------------------------------------
    def _plan_deploy(self, args: list[str]) -> DomainCommandPlan:
        ref, channel, requested_name = _parse_deploy(args)
        config, _memory, client = self._config_memory()
        _validate_hub_endpoint(getattr(getattr(config, "hub", None), "base_url", ""))
        runtime_binding = _runtime_binding(
            config,
            "hub",
            "mage",
            "memory",
            "tools",
            "sandbox",
        )
        entity_id, record = _resolve_chain(client, ref, channel=channel)
        chain = _record_content(record)
        display_name = _display_name(record, entity_id)
        try:
            bundled = list(self._sources.bundle_tools(chain, config) or [])
        except Exception:  # TUI treats synthesis bundling as best-effort
            bundled = []
        bundled_names = frozenset(
            _text(_field(item, "name", ""), 128)
            for item in bundled
            if _text(_field(item, "name", ""), 128)
        )
        try:
            issues = list(self._sources.deploy_gate(chain, bundled_names) or [])
        except Exception as exc:  # noqa: BLE001
            raise _operation_failed("Deploy gate failed to run.", exc) from exc
        if issues:
            safe_issues = [_text(issue, 500) for issue in issues[:20]]
            raise DomainCommandError(
                "deploy_gate_failed",
                "Deploy gate failed: " + "; ".join(safe_issues),
                status=409,
            )
        agent_name = requested_name or _slugify(display_name)
        preview = {
            "action": "deploy",
            "entity_id": entity_id,
            "display_name": display_name,
            "version_id": _identifier(_field(record, "version_id", "")) or None,
            "channel": channel,
            "agent_name": agent_name,
            "bundled_tools": sorted(bundled_names),
            "gate_passed": True,
        }

        def execute() -> DomainCommandResult:
            self._require_runtime_unchanged(
                runtime_binding,
                "hub",
                "mage",
                "memory",
                "tools",
                "sandbox",
            )
            credential = self._sources.token_factory()
            if (
                not isinstance(credential, str)
                or not _CREDENTIAL_RE.fullmatch(credential)
                or redact_sensitive_text(credential) != credential
            ):
                raise DomainCommandError(
                    "credential_generation_failed",
                    "Could not generate a deployment credential.",
                    status=500,
                )
            try:
                hub = self._sources.ensure_hub(config)
                deployment = _run_maybe_async(
                    hub.deploy(
                        {
                            "name": agent_name,
                            "entity_id": entity_id,
                            "channel": channel,
                            "api_key": credential,
                            "extra_tools": bundled,
                        }
                    )
                )
            except DomainCommandError:
                raise
            except Exception as exc:  # noqa: BLE001
                raise _operation_failed("Deployment failed.", exc) from exc
            deployed_name = _text(_field(deployment, "name", agent_name), 128)
            agent_url = _safe_url(
                hub.agent_url(deployed_name)
                if callable(getattr(hub, "agent_url", None))
                else _field(deployment, "url", "")
            )
            docs_url = _safe_url(
                hub.docs_url(deployed_name)
                if callable(getattr(hub, "docs_url", None))
                else (agent_url.rstrip("/") + "/docs" if agent_url else "")
            )
            deployment_payload = {
                **preview,
                "deployed_name": deployed_name,
                "agent_url": agent_url,
                "docs_url": docs_url,
                "ready": bool(_field(deployment, "ready", False)),
                "ready_reason": _text(_field(deployment, "ready_reason", ""), 500),
            }
            stdout = (
                f"Deployed {display_name} as {deployed_name}.\n"
                f"Agent: {agent_url}\nDocs: {docs_url}\n"
            )
            return _result(
                stdout,
                deployment_payload,
                ephemeral_secret=credential,
            )

        return _mutation_plan(
            "deploy",
            SecondDomainRisk.EXTERNAL_SIDE_EFFECT,
            "This may start the local agent hub and creates a network deployment.",
            (
                "Start the configured local agent hub if autostart is enabled.",
                f"Deploy {entity_id} on channel {channel!r} as {agent_name!r}.",
                "Generate and display a new per-agent credential once.",
            ),
            {
                "entity_id": entity_id,
                "channel": channel,
                "version_id": _field(record, "version_id", ""),
                "chain_digest": _state_digest(chain),
                "agent_name": agent_name,
                "bundled_digest": _state_digest(bundled),
                "runtime": runtime_binding,
            },
            preview,
            execute,
        )

    # ------------------------------------------------------------------
    # /promote <id|unique-name> [--from C] [--to C] [--force]
    # ------------------------------------------------------------------
    def _plan_promote(self, args: list[str]) -> DomainCommandPlan:
        ref, from_channel, to_channel, force = _parse_promote(args)
        config, memory, client = self._config_memory()
        runtime_binding = _runtime_binding(
            config,
            "memory",
            "mage",
            "tools",
            "sandbox",
        )
        entity_id, record = _resolve_chain(client, ref, channel=from_channel)
        candidate = _record_content(record)
        target_version = "<missing>"
        try:
            target = client.get_chain_record(entity_id, channel=to_channel)
        except Exception as exc:
            if _is_not_found_error(exc):
                target = None
            else:
                raise _operation_failed(
                    "Could not inspect the target promotion channel.", exc
                ) from exc
        if target is not None:
            target_version = str(_field(target, "version_id", "") or "<unknown>")
        display_name = _display_name(record, entity_id)
        preview = {
            "action": "promote",
            "entity_id": entity_id,
            "display_name": display_name,
            "from_channel": from_channel,
            "to_channel": to_channel,
            "candidate_version": _identifier(_field(record, "version_id", "")) or None,
            "target_version": _identifier(target_version) or None,
            "force": force,
            "gate": "skipped_by_force" if force else "artifact_baseline_eval",
        }

        def execute() -> DomainCommandResult:
            self._require_runtime_unchanged(
                runtime_binding,
                "memory",
                "mage",
                "tools",
                "sandbox",
            )
            checks: list[dict[str, Any]] = []
            if not force:
                try:
                    report = self._sources.promotion_gate(
                        memory,
                        config,
                        entity_id,
                        from_channel=from_channel,
                        to_channel=to_channel,
                    )
                    report = _run_maybe_async(report)
                except Exception as exc:  # noqa: BLE001
                    raise _operation_failed(
                        "Promotion gate failed to run.", exc
                    ) from exc
                for check in list(getattr(report, "checks", ()) or ())[:20]:
                    checks.append(
                        {
                            "name": _text(_field(check, "name", ""), 120),
                            "passed": bool(_field(check, "passed", False)),
                            "skipped": bool(_field(check, "skipped", False)),
                            "detail": _text(_field(check, "detail", ""), 800),
                        }
                    )
                if not bool(getattr(report, "ok", False)):
                    return _result(
                        "Promotion refused: the release gate failed.\n",
                        {**preview, "checks": checks, "promoted": False},
                        ok=False,
                        exit_code=1,
                        stderr="Promotion refused by the release gate.\n",
                    )
            promoter = getattr(client, "promote", None)
            if not callable(promoter):
                raise DomainCommandError(
                    "domain_capability_missing",
                    "This Memory build does not expose channel promotion.",
                    status=501,
                )
            try:
                promoter(entity_id, from_channel, to_channel)
            except Exception as exc:  # noqa: BLE001
                raise _operation_failed("Promotion failed.", exc) from exc
            return _result(
                f"Promoted {display_name}: {from_channel} -> {to_channel}.\n",
                {**preview, "checks": checks, "promoted": True},
            )

        risk = (
            SecondDomainRisk.EXTERNAL_SIDE_EFFECT
            if force
            else SecondDomainRisk.PROVIDER_EXECUTION
        )
        effects = []
        if not force:
            effects.extend(
                [
                    "Run the artifact gate.",
                    "Execute and record a provider/tool baseline run.",
                    "Compare the candidate with the target eval baseline when available.",
                ]
            )
        else:
            effects.append("Skip every promotion gate because --force was supplied.")
        effects.append(
            f"Move channel {to_channel!r} to the candidate from {from_channel!r}."
        )
        return _mutation_plan(
            "promote",
            risk,
            (
                "This runs the release gate, including a billable baseline, then mutates the channel."
                if not force
                else "This bypasses the release gate and mutates the target channel."
            ),
            tuple(effects),
            {
                "entity_id": entity_id,
                "from_channel": from_channel,
                "to_channel": to_channel,
                "candidate_version": _field(record, "version_id", ""),
                "candidate_digest": _state_digest(candidate),
                "target_version": target_version,
                "force": force,
                "runtime": runtime_binding,
            },
            preview,
            execute,
        )


# ---------------------------------------------------------------------------
# Strict parsers
# ---------------------------------------------------------------------------


def _parse_marketplace_install(args: list[str]) -> tuple[str, str | None]:
    if not args:
        raise DomainCommandError(
            "slash_arguments_invalid",
            "Usage: /marketplace install <entity-id> [--name NAME].",
        )
    entity_id: str | None = None
    name: str | None = None
    index = 0
    while index < len(args):
        token = args[index]
        if token == "--name" or token.startswith("--name="):
            if name is not None:
                raise DomainCommandError(
                    "slash_arguments_invalid", "--name may appear once."
                )
            raw, index = _option_value(args, index, "--name")
            name = _free_text(raw, label="Skill name", allow_empty=False, cap=200)
        elif token.startswith("-") or entity_id is not None:
            raise DomainCommandError(
                "slash_arguments_invalid",
                "Usage: /marketplace install <entity-id> [--name NAME].",
            )
        else:
            entity_id = _identifier(token)
            index += 1
    if not entity_id:
        raise DomainCommandError(
            "slash_arguments_invalid", "A valid skill entity id is required."
        )
    return entity_id, name


def _parse_run(args: list[str]) -> dict[str, Any]:
    if not args:
        raise DomainCommandError(
            "slash_arguments_invalid",
            "Usage: /run <chain-id> [--channel NAME] [--execute] [--input K=V] [--task TEXT] [--save-result NAME].",
        )
    chain_id: str | None = None
    channel = "latest"
    execute = False
    task: str | None = None
    save_result: str | None = None
    inputs: dict[str, str] = {}
    seen: set[str] = set()
    index = 0
    while index < len(args):
        token = args[index]
        if token == "--execute":
            if "--execute" in seen:
                raise DomainCommandError(
                    "slash_arguments_invalid", "--execute may appear once."
                )
            seen.add("--execute")
            execute = True
            index += 1
            continue
        option = next(
            (
                name
                for name in ("--channel", "--input", "--task", "--save-result")
                if token == name or token.startswith(name + "=")
            ),
            None,
        )
        if option:
            raw, index = _option_value(args, index, option)
            if option != "--input" and option in seen:
                raise DomainCommandError(
                    "slash_arguments_invalid", f"{option} may appear once."
                )
            seen.add(option)
            if option == "--channel":
                channel = _channel(raw)
            elif option == "--task":
                task = _free_text(
                    raw, label="Task", allow_empty=False, cap=MAX_TASK_CHARS
                )
            elif option == "--save-result":
                save_result = _free_text(
                    raw, label="Result name", allow_empty=False, cap=200
                )
            else:
                key, sep, value = raw.partition("=")
                if (
                    not sep
                    or not _INPUT_KEY_RE.fullmatch(key)
                    or len(value) > MAX_INPUT_VALUE_CHARS
                ):
                    raise DomainCommandError(
                        "slash_arguments_invalid",
                        "--input must be KEY=VALUE with a 1-128 character key.",
                    )
                inputs[key] = value
            continue
        if token.startswith("-") or chain_id is not None:
            raise DomainCommandError(
                "slash_arguments_invalid",
                "Desktop /run accepts one exact chain id plus reviewed options.",
            )
        chain_id = _identifier(token)
        index += 1
    if not chain_id:
        raise DomainCommandError(
            "slash_arguments_invalid", "A valid exact chain id is required."
        )
    if (inputs or task is not None or save_result is not None) and not execute:
        raise DomainCommandError(
            "slash_arguments_invalid",
            "--input, --task, and --save-result require --execute.",
        )
    return {
        "chain_id": chain_id,
        "channel": channel,
        "execute": execute,
        "task": task,
        "save_result": save_result,
        "inputs": inputs,
    }


def _parse_deploy(args: list[str]) -> tuple[str, str, str | None]:
    if not args:
        raise DomainCommandError(
            "slash_arguments_invalid",
            "Usage: /deploy <chain-id|unique-name> [--channel NAME] [--name NAME].",
        )
    channel = "stable"
    name: str | None = None
    ref: list[str] = []
    seen: set[str] = set()
    index = 0
    while index < len(args):
        token = args[index]
        option = next(
            (
                item
                for item in ("--channel", "--name")
                if token == item or token.startswith(item + "=")
            ),
            None,
        )
        if option:
            if option in seen:
                raise DomainCommandError(
                    "slash_arguments_invalid", f"{option} may appear once."
                )
            seen.add(option)
            raw, index = _option_value(args, index, option)
            if option == "--channel":
                channel = _channel(raw)
            else:
                proposed = _free_text(
                    raw, label="Deployment name", allow_empty=False, cap=63
                ).lower()
                if not _DEPLOYMENT_RE.fullmatch(proposed):
                    raise DomainCommandError(
                        "slash_arguments_invalid",
                        "Deployment names may use lowercase letters, digits, '.', '_', and '-' (max 63).",
                    )
                name = proposed
            continue
        if token.startswith("-"):
            raise DomainCommandError(
                "slash_arguments_invalid", f"Unknown deploy option {token!r}."
            )
        ref.append(token)
        index += 1
    return _reference(" ".join(ref)), channel, name


def _parse_promote(args: list[str]) -> tuple[str, str, str, bool]:
    if not args:
        raise DomainCommandError(
            "slash_arguments_invalid",
            "Usage: /promote <chain-id|unique-name> [--from NAME] [--to NAME] [--force].",
        )
    from_channel, to_channel, force = "latest", "stable", False
    ref: list[str] = []
    seen: set[str] = set()
    index = 0
    while index < len(args):
        token = args[index]
        if token == "--force":
            if force:
                raise DomainCommandError(
                    "slash_arguments_invalid", "--force may appear once."
                )
            force = True
            index += 1
            continue
        option = next(
            (
                item
                for item in ("--from", "--to")
                if token == item or token.startswith(item + "=")
            ),
            None,
        )
        if option:
            if option in seen:
                raise DomainCommandError(
                    "slash_arguments_invalid", f"{option} may appear once."
                )
            seen.add(option)
            raw, index = _option_value(args, index, option)
            if option == "--from":
                from_channel = _channel(raw)
            else:
                to_channel = _channel(raw)
            continue
        if token.startswith("-"):
            raise DomainCommandError(
                "slash_arguments_invalid", f"Unknown promote option {token!r}."
            )
        ref.append(token)
        index += 1
    if from_channel == to_channel:
        raise DomainCommandError(
            "slash_arguments_invalid", "--from and --to must name different channels."
        )
    return _reference(" ".join(ref)), from_channel, to_channel, force


def _option_value(args: list[str], index: int, option: str) -> tuple[str, int]:
    token = args[index]
    if token.startswith(option + "="):
        value = token[len(option) + 1 :]
        if not value:
            raise DomainCommandError(
                "slash_arguments_invalid", f"{option} requires a value."
            )
        return value, index + 1
    if index + 1 >= len(args):
        raise DomainCommandError(
            "slash_arguments_invalid", f"{option} requires a value."
        )
    return args[index + 1], index + 2


# ---------------------------------------------------------------------------
# Domain helpers
# ---------------------------------------------------------------------------


def _exact_chain_record(client: Any, entity_id: str, channel: str) -> Any:
    getter = getattr(client, "get_chain_record", None)
    if not callable(getter):
        raise DomainCommandError(
            "domain_capability_missing",
            "This Memory build does not expose chain-version lookup.",
            status=501,
        )
    try:
        record = getter(entity_id, channel=channel)
    except Exception as exc:  # noqa: BLE001
        if _is_not_found_error(exc):
            raise DomainCommandError(
                "chain_not_found",
                f"Could not load chain {entity_id!r} on channel {channel!r}.",
                status=404,
            ) from exc
        raise _operation_failed("Could not load the exact chain id.", exc) from exc
    if record is None:
        raise DomainCommandError(
            "chain_not_found",
            f"Could not load chain {entity_id!r} on channel {channel!r}.",
            status=404,
        )
    return record


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
    except Exception as exc:
        if _is_not_found_error(exc):
            direct = None
        else:
            raise _operation_failed(
                "Could not resolve the exact chain id.", exc
            ) from exc
    if direct is not None:
        entity_id = _identifier(_field(direct, "entity_id", None) or ref)
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
        hits = list(lister(limit=MAX_ROWS + 1, q=ref) or [])
    except Exception as exc:  # noqa: BLE001
        raise _operation_failed("Could not resolve the chain reference.", exc) from exc
    if not hits:
        raise DomainCommandError(
            "chain_not_found",
            f"Could not resolve {ref!r} to a saved chain.",
            status=404,
        )
    if len(hits) > MAX_ROWS:
        raise DomainCommandError(
            "chain_reference_ambiguous",
            f"{ref!r} has more matches than can be reviewed safely; use an exact id.",
            status=409,
        )
    exact = [
        item
        for item in hits
        if str(
            _field(item, "display_name", "")
            or _field(_field(item, "meta", {}), "display_name", "")
            or _field(item, "name", "")
        ).casefold()
        == ref.casefold()
    ]
    if len(exact) > 1:
        raise DomainCommandError(
            "chain_reference_ambiguous",
            f"{ref!r} matches several chains; use an exact id.",
            status=409,
        )
    if not exact:
        raise DomainCommandError(
            "chain_not_found",
            f"Could not resolve {ref!r} to one exact saved-chain name; use an exact id.",
            status=404,
        )
    entity_id = _identifier(_field(exact[0], "entity_id", ""))
    if not entity_id:
        raise DomainCommandError(
            "chain_reference_invalid",
            "The name match has no valid chain id.",
            status=502,
        )
    return entity_id, _exact_chain_record(client, entity_id, channel)


def _marketplace_source(client: Any, entity_id: str) -> Any:
    getter = getattr(client, "get_agent_skill", None)
    if callable(getter):
        try:
            source = getter(entity_id, channel="latest")
        except Exception as exc:
            if _is_not_found_error(exc):
                source = None
            else:
                raise _operation_failed(
                    "Could not resolve the exact marketplace skill id.", exc
                ) from exc
        if source is not None:
            return source
    lister = getattr(client, "list_agent_skills", None)
    if callable(lister):
        try:
            hits = list(lister(limit=5, q=entity_id) or [])
        except Exception as exc:  # noqa: BLE001
            raise _operation_failed(
                "Could not resolve the marketplace skill.", exc
            ) from exc
        exact = [item for item in hits if _field(item, "entity_id", "") == entity_id]
        if len(exact) == 1:
            return exact[0]
    raise DomainCommandError(
        "marketplace_skill_not_found",
        f"Could not resolve marketplace skill {entity_id!r}.",
        status=404,
    )


def _marketplace_row(item: Any) -> dict[str, Any]:
    meta = _field(item, "meta", {}) or {}
    content = _field(item, "content", {}) or {}
    name = (
        _field(item, "name", "")
        or _field(item, "display_name", "")
        or _field(meta, "name", "")
        or _field(content, "name", "")
        or _field(item, "entity_id", "")
    )
    tags = _field(item, "tags", None) or _field(meta, "tags", ()) or ()
    score = _finite_number(_field(item, "score", None))
    return {
        "entity_id": _identifier(_field(item, "entity_id", "")),
        "name": _text(name, 200),
        "description": _text(
            _field(item, "description", "") or _field(content, "description", ""),
            800,
        ),
        "score": score,
        "tags": [_text(tag, 120) for tag in list(tags)[:30]],
        "matched_via": _text(_field(item, "matched_via", ""), 120) or None,
        "snippet": _text(_field(item, "snippet", ""), 800) or None,
    }


def _record_content(record: Any) -> dict[str, Any]:
    content = _field(record, "content", None)
    if not isinstance(content, Mapping):
        raise DomainCommandError(
            "chain_payload_invalid",
            "The resolved chain has no object payload.",
            status=502,
        )
    return copy.deepcopy(dict(content))


def _display_name(record: Any, entity_id: str) -> str:
    content = _field(record, "content", {}) or {}
    meta = _field(record, "meta", {}) or {}
    return _text(
        _field(meta, "display_name", "") or _field(content, "name", "") or entity_id,
        200,
    )


def _run_preview(
    record: Any,
    chain: dict[str, Any],
    preflight: Any,
    options: Mapping[str, Any],
) -> dict[str, Any]:
    return {
        "action": "execute" if options["execute"] else "inspect",
        "entity_id": options["chain_id"],
        "channel": options["channel"],
        "version_id": _identifier(_field(record, "version_id", "")) or None,
        "display_name": _display_name(record, options["chain_id"]),
        "steps": len(chain.get("steps") or []),
        "parsed": bool(getattr(preflight, "parsed", False)),
        "ok": bool(getattr(preflight, "ok", False)),
        "preflight_available": bool(getattr(preflight, "preflight_available", False)),
        "parse_errors": [
            _text(item, 800)
            for item in list(getattr(preflight, "parse_errors", ()) or ())[:30]
        ],
        "required_tools": [
            _text(item, 200)
            for item in list(getattr(preflight, "required_tools", ()) or ())[:100]
        ],
        "missing_tools": [
            _text(item, 200)
            for item in list(getattr(preflight, "missing_tools", ()) or ())[:100]
        ],
        "required_mcp_servers": [
            _text(item, 200)
            for item in list(getattr(preflight, "required_mcp_servers", ()) or ())[:100]
        ],
        "missing_mcp_servers": [
            _text(item, 200)
            for item in list(getattr(preflight, "missing_mcp_servers", ()) or ())[:100]
        ],
        "required_skills": [
            _text(item, 200)
            for item in list(getattr(preflight, "required_skills", ()) or ())[:100]
        ],
        "missing_skills": [
            _text(item, 200)
            for item in list(getattr(preflight, "missing_skills", ()) or ())[:100]
        ],
        "execute": bool(options["execute"]),
        "executed": False,
        "input_keys": sorted(options["inputs"]),
        "task_supplied": options["task"] is not None,
        "save_result": options["save_result"],
    }


def _format_run_preview(preview: Mapping[str, Any]) -> str:
    lines = [
        f"Chain {preview['display_name']} [{preview['entity_id']}]",
        f"channel={preview['channel']} version={preview.get('version_id') or '?'} steps={preview['steps']}",
        f"preflight: parsed={preview['parsed']} ok={preview['ok']}",
    ]
    for label in ("missing_tools", "missing_mcp_servers", "missing_skills"):
        values = preview.get(label) or []
        if values:
            lines.append(f"{label}: {', '.join(values)}")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Plan/result safety and async helpers
# ---------------------------------------------------------------------------


def _read_plan(
    name: str,
    preview: Mapping[str, Any],
    execute: Callable[[], DomainCommandResult],
) -> DomainCommandPlan:
    return DomainCommandPlan(
        name=name,
        risk=SecondDomainRisk.READ_ONLY,  # type: ignore[arg-type]
        reason="",
        effects=(),
        confirmation_scope="",
        preview=_project_mapping(preview),
        execute=execute,
    )


def _mutation_plan(
    name: str,
    risk: SecondDomainRisk,
    reason: str,
    effects: tuple[str, ...],
    state: Mapping[str, Any],
    preview: Mapping[str, Any],
    execute: Callable[[], DomainCommandResult],
) -> DomainCommandPlan:
    scope = hashlib.sha256(
        json.dumps(
            _project_mapping(state),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8", errors="surrogatepass")
    ).hexdigest()
    return DomainCommandPlan(
        name=name,
        risk=risk,  # type: ignore[arg-type]
        reason=_text(reason, 600),
        effects=tuple(_text(effect, 600) for effect in effects),
        confirmation_scope=scope,
        preview=_project_mapping(preview),
        execute=execute,
    )


def _result(
    stdout: str,
    payload: Mapping[str, Any],
    *,
    ok: bool = True,
    exit_code: int = 0,
    stderr: str = "",
    ephemeral_secret: str | None = None,
) -> DomainCommandResult:
    projected = _project_mapping(payload)
    return DomainCommandResult(
        stdout=_text(stdout, MAX_OUTPUT_CHARS, strip=False),
        payload=projected,
        ok=bool(ok),
        exit_code=int(exit_code),
        stderr=_text(stderr, MAX_OUTPUT_CHARS, strip=False),
        ephemeral_secret=ephemeral_secret,
    )


def _project_mapping(value: Mapping[str, Any]) -> dict[str, Any]:
    projected = _project(dict(value), depth=0)
    return projected if isinstance(projected, dict) else {}


def _project(value: Any, *, depth: int = 0) -> Any:
    if depth >= 7:
        return "[depth limit]"
    if value is None or isinstance(value, bool | int):
        return value
    if isinstance(value, float):
        return _finite_number(value)
    if isinstance(value, str):
        return _text(value, MAX_SAFE_VALUE_CHARS)
    if isinstance(value, Mapping):
        output: dict[str, Any] = {}
        for index, (key, item) in enumerate(value.items()):
            if index >= 200:
                output["__truncated__"] = True
                break
            safe_key = _text(key, 128)
            if _secret_key(safe_key):
                output[safe_key] = "[redacted]"
            else:
                output[safe_key] = _project(item, depth=depth + 1)
        return output
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_project(item, depth=depth + 1) for item in list(value)[:500]]
    if hasattr(value, "model_dump"):
        try:
            return _project(value.model_dump(mode="json"), depth=depth + 1)
        except Exception:  # noqa: BLE001
            pass
    return _text(value, MAX_SAFE_VALUE_CHARS)


def _secret_key(key: str) -> bool:
    normal = key.casefold().replace("-", "_")
    return any(
        word in normal
        for word in ("api_key", "password", "secret", "authorization", "token")
    )


def _text(value: Any, cap: int, *, strip: bool = True) -> str:
    text = redact_sensitive_text(value, max_chars=cap)
    text = _CONTROL_RE.sub(" ", text)
    return text.strip() if strip else text


def _free_text(
    value: Any,
    *,
    label: str,
    allow_empty: bool,
    cap: int = MAX_QUERY_CHARS,
) -> str:
    text = str(value or "").strip()
    if (not text and not allow_empty) or len(text) > cap or _CONTROL_RE.search(text):
        requirement = f"1-{cap}" if not allow_empty else f"0-{cap}"
        raise DomainCommandError(
            "slash_arguments_invalid",
            f"{label} must be {requirement} printable characters.",
        )
    return text


def _identifier(value: Any) -> str:
    text = str(value or "").strip()
    return text if _IDENTIFIER_RE.fullmatch(text) else ""


def _reference(value: Any) -> str:
    text = _free_text(value, label="Chain reference", allow_empty=False, cap=256)
    if text.startswith("-"):
        raise DomainCommandError(
            "slash_arguments_invalid", "Chain reference must not start with '-'."
        )
    return text


def _channel(value: Any) -> str:
    text = str(value or "").strip()
    if not _CHANNEL_RE.fullmatch(text):
        raise DomainCommandError(
            "slash_arguments_invalid",
            "Channel names may use letters, digits, '.', '_', and '-' (max 64).",
        )
    return text


def _slugify(value: str) -> str:
    slug = re.sub(r"[^a-z0-9._-]+", "-", value.casefold()).strip("-._")
    slug = re.sub(r"-{2,}", "-", slug)[:63]
    return slug or "agent"


def _field(value: Any, key: str, default: Any = None) -> Any:
    if isinstance(value, Mapping):
        return value.get(key, default)
    return getattr(value, key, default)


def _finite_number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if number == number and abs(number) != float("inf") else None


def _state_digest(value: Any) -> str:
    """Hash exact state without routing it through display redaction.

    Redaction is correct for previews, but using a redacted projection here
    would make two different secret-valued inputs share a confirmation scope.
    The canonical bytes never leave this process; only the digest does.
    """

    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=_state_json_default,
        ).encode("utf-8", errors="surrogatepass")
    ).hexdigest()


def _state_json_default(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        try:
            return value.model_dump(mode="json")
        except Exception:  # noqa: BLE001
            pass
    attributes = getattr(value, "__dict__", None)
    if isinstance(attributes, dict):
        return attributes
    return {"type": type(value).__name__, "value": str(value)}


def _runtime_binding(config: Any, *sections: str) -> str:
    """Digest non-secret destinations and runtime choices used by a plan."""

    state: dict[str, Any] = {}
    requested = set(sections)
    if "mage" in requested:
        mage = getattr(config, "mage", None)
        state["mage"] = {
            "provider": _text(getattr(mage, "provider", "") or "", 80),
            "base_url": _safe_url(getattr(mage, "base_url", "") or ""),
            "model": _text(getattr(mage, "model", "") or "", 300),
            "web_search_provider": _text(
                getattr(mage, "web_search_provider", "") or "", 80
            ),
            "api_key_present": bool(getattr(mage, "api_key", None)),
            "web_search_key_present": bool(getattr(mage, "web_search_api_key", None)),
        }
    if "memory" in requested:
        memory = getattr(config, "memory", None)
        state["memory"] = {
            "base_url": _safe_url(getattr(memory, "base_url", "") or ""),
            "timeout": _finite_number(getattr(memory, "timeout", None)),
            "api_key_present": bool(getattr(memory, "api_key", None)),
        }
    if "hub" in requested:
        hub = getattr(config, "hub", None)
        command = getattr(hub, "agent_server_cmd", ()) or ()
        state["hub"] = {
            "base_url": _safe_url(getattr(hub, "base_url", "") or ""),
            "autostart": bool(getattr(hub, "autostart", False)),
            "port": getattr(hub, "port", None),
            "agent_server_cmd": [_text(item, 300) for item in list(command)[:20]],
        }
    if "tools" in requested:
        tools = getattr(config, "tools", None)
        state["tools"] = {
            "auto_synthesize_tools": bool(
                getattr(tools, "auto_synthesize_tools", False)
            ),
            "enable_code_exec": bool(getattr(tools, "enable_code_exec", False)),
            "verify_cached_tools": bool(getattr(tools, "verify_cached_tools", False)),
        }
    if "sandbox" in requested:
        sandbox = getattr(config, "sandbox", None)
        state["sandbox"] = {
            "kind": _text(getattr(sandbox, "kind", "") or "", 80),
        }
    if "context" in requested:
        context = getattr(config, "context", None)
        state["context"] = {
            "ltm_enabled": bool(getattr(context, "ltm_enabled", False)),
            "ltm_session_id": _text(getattr(context, "ltm_session_id", "") or "", 256),
            # Bind the storage target without exposing a local path in previews.
            "ltm_dir_digest": _state_digest(
                os.environ.get("CARE_CONTEXT__LTM_DIR")
                or str(getattr(context, "ltm_dir", "") or "")
            ),
        }
    return _state_digest(state)


def _is_not_found_error(error: BaseException) -> bool:
    if isinstance(error, KeyError):
        return True
    try:
        from gigaevo_client.exceptions import NotFoundError

        if isinstance(error, NotFoundError):
            return True
    except Exception:  # pragma: no cover - dependency is optional at import time
        pass
    response = getattr(error, "response", None)
    return getattr(response, "status_code", None) == 404


def _validate_hub_endpoint(value: Any) -> str:
    try:
        parsed = urlsplit(str(value or ""))
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("invalid hub origin")
        # Accessing the port validates its syntax/range.
        _ = parsed.port
    except (TypeError, ValueError) as exc:
        raise DomainCommandError(
            "hub_endpoint_invalid",
            "The agent hub must use a valid HTTP(S) origin without credentials, query, or fragment.",
            status=409,
        ) from exc
    if parsed.scheme == "http" and not _loopback_hostname(parsed.hostname):
        raise DomainCommandError(
            "hub_endpoint_insecure",
            "A remote agent hub must use HTTPS; HTTP is allowed only on loopback.",
            status=409,
        )
    return _safe_url(value)


def _loopback_hostname(hostname: str) -> bool:
    if hostname.casefold() == "localhost":
        return True
    try:
        return ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        return False


def _safe_url(value: Any) -> str:
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


def _run_maybe_async(value: Any) -> Any:
    return _run_async(value) if hasattr(value, "__await__") else value


def _operation_failed(message: str, _error: BaseException) -> DomainCommandError:
    return DomainCommandError("domain_operation_failed", message, status=502)


__all__ = [
    "DesktopSecondDomainCommands",
    "DesktopSecondDomainSources",
    "SecondDomainRisk",
]
