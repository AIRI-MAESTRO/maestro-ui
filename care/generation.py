"""MAGE generation wiring (TODO §4 P0).

The future ``GenerationScreen`` needs to drive
``MAGEGenerator(config=MAGEConfig.from_toml(...),
progress=...)`` from CARE's :class:`CareConfig` + a
:class:`care.runtime.MagePoster`. The data-layer glue is
bounded:

* Translate ``CareConfig.mage`` (CARE's narrow Pydantic
  surface) into a fully-populated ``mmar_mage.MAGEConfig``.
* Lazy-import ``mmar_mage.MAGEGenerator`` so a CARE install
  without the ``mage`` extra still imports this module
  cleanly. Missing dep raises a friendly
  :class:`GenerationError` rather than a raw ``ImportError``.
* Wrap the async ``generate(query, ...)`` call so screen
  code (or the future ``care generate`` CLI) doesn't have to
  juggle kwarg names. Forwards ``context_files`` / ``cancel``
  / ``capabilities`` verbatim.

The actual screen work — composing the worker, wiring
``MagePoster`` into the app, rendering the streamed DAG — is
gated on §1 P0 UI. This module is the data-layer floor the
screen builds on.
"""

from __future__ import annotations

import asyncio
import inspect
import re
from pathlib import Path
from typing import Any, Literal

from mmar_mage.generator import MAGEGenerator
from mmar_mage.schemas import MAGEConfig

from care.config import CareConfig, MageConfig


class GenerationError(RuntimeError):
    """Raised when the generation wiring can't proceed —
    missing API key, malformed config, or downstream
    ``generate()`` raised."""


_EMBEDDED_BLOCK_RE = re.compile(
    r"<(file|image)\b[^>]*>.*?</\1>",
    re.IGNORECASE | re.DOTALL,
)


def _supports_typed_generation_request(generate: Any) -> bool:
    """Detect the post-0.1.3 trust-boundary API without version guessing."""
    try:
        annotation = inspect.signature(generate).parameters["query"].annotation
    except (KeyError, TypeError, ValueError):
        return False
    return "GenerationRequest" in str(annotation)


def build_generation_request(
    query: str,
    *,
    safe_task: str,
    user_context: str = "",
    recalled_context: str = "",
    context_files: list[dict[str, Any]] | None = None,
) -> Any:
    """Build MAGE's typed request using CARE's canonical file loader.

    File bodies remain private/untrusted planning context. Only structural
    profiles (schema, rows, missingness, section outline) enter the manifest;
    profiles never contain cell values. ``safe_task`` is the sole
    research/persistence projection.
    """
    try:
        from mmar_mage.schemas import (
            DataEgressPolicy,
            GenerationInputManifestEntry,
            GenerationRequest,
            PersistencePolicy,
            RetrievedUntrustedContext,
        )
    except ImportError as exc:
        raise GenerationError(
            "installed mmar-mage lacks GenerationRequest; install release 0.1.4+"
        ) from exc

    from care.runtime.file_loading import load_file, profile_loaded_file

    constraints: list[str] = []
    planning_remainder = query
    if query.startswith("(Today is "):
        date_line, separator, remainder = query.partition("\n\n")
        if separator:
            constraints.append(date_line)
            planning_remainder = remainder

    manifest: list[Any] = []
    contexts: list[Any] = []
    if recalled_context.strip():
        contexts.append(
            RetrievedUntrustedContext(
                context_id="care-long-term-memory",
                source="care_ltm",
                content=recalled_context.strip(),
                private=True,
            )
        )
    for index, item in enumerate(context_files or [], start=1):
        raw_path = str(item.get("path") or "")
        name = Path(raw_path).name or f"attachment-{index}"
        loaded = load_file(raw_path)
        input_id = f"input-{index}"
        media_type = item.get("media_type") or item.get("mime_type")
        kind = "image" if loaded.is_image else "dataset" if Path(name).suffix.lower() in {".csv", ".tsv", ".json", ".xlsx"} else "file"
        manifest.append(
            GenerationInputManifestEntry(
                input_id=input_id,
                name=name,
                kind=kind,
                media_type=media_type,
                size_bytes=item.get("size_bytes", loaded.size_bytes),
                sha256=item.get("sha256"),
                local_path=raw_path or None,
                profile=profile_loaded_file(loaded, name=name).as_dict(),
                available=loaded.ok,
                error=loaded.error,
            )
        )
        if loaded.ok:
            contexts.append(
                RetrievedUntrustedContext(
                    context_id=input_id,
                    source=f"attachment:{name}",
                    content=loaded.memory_value,
                    media_type=media_type,
                    private=True,
                )
            )

    # Preserve conversational/follow-up context that is richer than the safe
    # task, but label it untrusted/private and strip inline attachment bodies;
    # canonical file loading above supplies those bodies exactly once.
    turn_context = _EMBEDDED_BLOCK_RE.sub("[attached user data]", planning_remainder).strip()
    if turn_context and turn_context != safe_task.strip():
        contexts.append(
            RetrievedUntrustedContext(
                context_id="turn-context",
                source="current-turn-private-context",
                content=turn_context,
                private=True,
            )
        )

    return GenerationRequest(
        current_task=safe_task,
        current_turn_constraints=constraints,
        project_instructions=user_context,
        retrieved_untrusted_context=contexts,
        input_manifest=manifest,
        data_egress_policy=DataEgressPolicy(external_research="safe_task_only"),
        persistence_policy=PersistencePolicy(mode="explicit_only"),
    )


def build_mage_config(
    care_config: CareConfig | MageConfig,
    *,
    mode: Literal["fast", "deep"] | None = None,
    deployable: bool = False,
) -> Any:
    """Build a ``mmar_mage.MAGEConfig`` from CARE's config.

    Translates the fields ``CareConfig.mage`` carries (provider,
    api_key, base_url, model, research toggles, etc.) into the
    ``MAGEConfig`` shape MAGE's generator expects. Validation
    lives on the MAGE side — we forward and let MAGE's Pydantic
    model error out if a value is invalid (the resulting
    ``ValidationError`` wraps in :class:`GenerationError`).

    Args:
        care_config: Either a full :class:`CareConfig` or just
            the ``mage`` section. Library code grabbing this
            from outside the TUI typically hand-rolls a
            :class:`MageConfig`.
        mode: Override for ``CareConfig.mage.mode``. The
            future ``GenerationScreen``'s Fast/Deep checkbox
            uses this to override without mutating the user's
            saved config.

    Returns:
        A fully-populated ``MAGEConfig`` instance.

    Raises:
        GenerationError: The translated config fails
            ``MAGEConfig`` validation, or required fields are
            missing.
    """
    mage = (
        care_config.mage if isinstance(care_config, CareConfig) else care_config
    )

    if not mage.api_key:
        raise GenerationError(
            "CareConfig.mage.api_key must be set to run generation; "
            "set CARE_MAGE__API_KEY or fill the `[mage] api_key` "
            "field in your config.toml"
        )

    resolved_mode = mode or mage.mode

    # `provider="custom"` tells MAGE to use whatever endpoint
    # we hand it via base_url, without applying provider-
    # specific defaults. CARE's user surface is single-endpoint
    # now (base_url + api_key + model); the provider concept
    # only lives on the MAGE side for internal routing.
    # MageConfig intentionally accepts future MAGE fields via ``extra=allow``.
    # The previous hand-written subset silently dropped those values and fell
    # back to unsafe/expensive defaults (notably ``enable_memory=False`` became
    # True). Forward every field the installed MAGE explicitly declares and
    # reject typos/version drift instead of ignoring them.
    raw = mage.model_dump(exclude_none=True)
    allowed = set(MAGEConfig.model_fields)
    unknown = sorted(set(raw) - allowed)
    if unknown:
        raise GenerationError(
            "CARE mage config contains field(s) unsupported by the installed "
            f"MAGE: {', '.join(unknown)}"
        )
    kwargs: dict[str, Any] = {key: value for key, value in raw.items() if key in allowed}
    kwargs["mode"] = resolved_mode
    kwargs["provider"] = "custom"

    # Point MAGE's MemoryManager at the SAME Memory service CARE uses, so
    # recall/save hit one store instead of MAGEConfig's localhost:8002
    # default ignoring CARE's configured URL.
    if isinstance(care_config, CareConfig) and getattr(care_config.memory, "base_url", None):
        kwargs["memory_base_url"] = care_config.memory.base_url

    # Deployable (production) chains run headless — there's no human to answer a
    # `human_input` step, and it would stall the agent. Forbid that step type so MAGE
    # builds the agent to consume its input ($outer_context) directly. Ad-hoc leaves it
    # allowed (the user is right there to answer mid-run prompts).
    if deployable:
        try:
            from mmar_mage.schemas import VALID_STEP_TYPES
        except Exception as exc:  # noqa: BLE001 — version/import drift must fail closed
            raise GenerationError(
                "cannot build a deployable MAGE config: the authoritative "
                "VALID_STEP_TYPES registry is unavailable, so CARE cannot "
                "prove that human_input is excluded"
            ) from exc

        if not isinstance(VALID_STEP_TYPES, (set, frozenset, list, tuple)) or not all(
            isinstance(step_type, str) and step_type
            for step_type in VALID_STEP_TYPES
        ):
            raise GenerationError(
                "cannot build a deployable MAGE config: the authoritative "
                "VALID_STEP_TYPES registry is malformed"
            )
        deployable_step_types = set(VALID_STEP_TYPES) - {"human_input"}
        if not deployable_step_types:
            raise GenerationError(
                "cannot build a deployable MAGE config: excluding human_input "
                "left no executable step types"
            )
        kwargs["allowed_step_types"] = sorted(deployable_step_types)

    try:
        return MAGEConfig(**kwargs)
    except Exception as exc:  # noqa: BLE001 — pydantic ValidationError is the common one
        raise GenerationError(
            f"failed to build MAGEConfig: {exc}"
        ) from exc


def build_mage_generator(
    care_config: CareConfig | MageConfig,
    *,
    progress: Any = None,
    mode: Literal["fast", "deep"] | None = None,
    deployable: bool = False,
) -> Any:
    """Construct a ``mmar_mage.MAGEGenerator`` ready for
    :func:`run_generation`.

    Args:
        care_config: Same as :func:`build_mage_config`.
        progress: A duck-typed ``MAGEProgressCallback`` —
            typically the :class:`care.runtime.MagePoster`
            wired to the running app. ``None`` keeps MAGE's
            default (no-op) progress reporter.
        mode: Mode override forwarded to
            :func:`build_mage_config`.

    Returns:
        A configured ``MAGEGenerator``.
    """
    config = build_mage_config(care_config, mode=mode, deployable=deployable)

    try:
        return MAGEGenerator(config=config, progress=progress)
    except Exception as exc:  # noqa: BLE001
        raise GenerationError(
            f"failed to construct MAGEGenerator: {exc}"
        ) from exc


async def run_generation(
    generator: Any,
    query: str,
    *,
    context_files: list[dict[str, Any]] | None = None,
    cancel: asyncio.Event | None = None,
    capabilities: Any = None,
    user_context: str = "",
    recalled_context: str = "",
    safe_task: str | None = None,
) -> Any:
    """Drive ``generator.generate(...)`` and return the result.

    Thin async wrapper so the future ``GenerationScreen``
    worker (and the future ``care generate`` CLI) don't have
    to remember MAGE's kwarg names. Forwards everything
    verbatim.

    Args:
        generator: From :func:`build_mage_generator` (or a
            duck-typed stub for tests).
        query: The user's text query.
        context_files: Optional list of context-file refs
            (``path`` / ``sha256`` / ``size_bytes`` dicts).
            CARE-side helpers in :mod:`care.runtime.executor`
            build this from the file picker.
        cancel: Optional ``asyncio.Event`` — wired to CARE's
            `Esc` key by the screen. Setting it aborts
            generation between stages.
        capabilities: Optional ``mmar_mage.CapabilityContext`` —
            typically built via
            :func:`care.build_capability_payload(...).to_mage_context()`.
        user_context: User-authored standing instructions for internal planning.
        recalled_context: Private model-derived/retrieved memory. It is passed
            as untrusted planning data, never as project instructions.
        safe_task: Current task without attachment bodies or private paths;
            MAGE uses it for research and persistence.

    Returns:
        The :class:`mmar_mage.MAGEResult` MAGE returned.
        Callers project it via :func:`care.summarise_mage_result`
        and :func:`care.project_intermediate_artifacts`.

    Raises:
        GenerationError: ``generator.generate(...)`` raised, or
            the supplied generator doesn't expose
            ``.generate(...)``.
    """
    if not isinstance(query, str) or not query.strip():
        raise GenerationError("query must be a non-empty string")

    generate = getattr(generator, "generate", None)
    if not callable(generate):
        raise GenerationError(
            "generator is missing `generate(...)` — pass a "
            "MAGEGenerator or a duck-typed equivalent"
        )

    try:
        parameters = inspect.signature(generate).parameters
        accepts_kwargs = any(
            item.kind is inspect.Parameter.VAR_KEYWORD
            for item in parameters.values()
        )
        missing_private_contract: list[str] = []
        if user_context and "user_context" not in parameters and not accepts_kwargs:
            missing_private_contract.append("user_context")
        if (
            safe_task is not None
            and safe_task != query
            and "safe_task" not in parameters
            and not accepts_kwargs
        ):
            missing_private_contract.append("safe_task")
        if missing_private_contract:
            raise GenerationError(
                "installed mmar-mage lacks the private planning contract "
                f"({', '.join(missing_private_contract)}); install the workspace "
                "carl-mage package or a release containing safe_task/user_context support"
            )

        use_typed_request = bool(
            safe_task
            and _supports_typed_generation_request(generate)
        )
        if recalled_context and not use_typed_request:
            raise GenerationError(
                "installed mmar-mage cannot represent recalled memory as untrusted context; "
                "install release 0.1.4+"
            )
        if use_typed_request:
            generation_input = build_generation_request(
                query,
                safe_task=safe_task or query,
                user_context=user_context,
                recalled_context=recalled_context,
                context_files=context_files,
            )
            kwargs: dict[str, Any] = {
                "cancel": cancel,
                "capabilities": capabilities,
            }
        else:
            generation_input = query
            kwargs = {
                "context_files": context_files,
                "cancel": cancel,
                "capabilities": capabilities,
            }
            if "user_context" in parameters or accepts_kwargs:
                kwargs["user_context"] = user_context
            if "safe_task" in parameters or accepts_kwargs:
                kwargs["safe_task"] = safe_task
        coro: Any = generate(generation_input, **kwargs)
        return await coro
    except GenerationError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise GenerationError(
            f"generate() raised: {exc}"
        ) from exc


async def run_edit(
    generator: Any,
    instruction: str,
    *,
    entity_id: str | None = None,
    chain: dict[str, Any] | None = None,
    channel: str = "latest",
    save: bool = False,
    cancel: asyncio.Event | None = None,
) -> Any:
    """Drive ``generator.edit(...)`` and return the ``MAGEEditResult``.

    Thin async wrapper mirroring :func:`run_generation` for the NL *edit* flow:
    ChatScreen's ``/revise`` worker and the library "Revise (AI)" action call
    this so they don't have to juggle MAGE's kwarg names.

    Args:
        generator: From :func:`build_mage_generator` (or a duck-typed stub).
        instruction: What to change, in natural language.
        entity_id: Id of the saved chain to edit. When omitted (and no
            ``chain`` is given) MAGE resolves the chain from ``instruction``
            via memory search (and may return a disambiguation result).
        chain: An in-memory chain dict to edit directly (skips resolution).
        channel: Version channel to load from / save to.
        save: Persist the edited chain as a new version when an id is known.
        cancel: Optional ``asyncio.Event`` — wired to CARE's `Esc` key.

    Returns:
        The :class:`mmar_mage.MAGEEditResult` MAGE returned (which may carry
        ``needs_disambiguation=True`` + ``candidates`` instead of an edit).

    Raises:
        GenerationError: ``generator.edit(...)`` raised, or the supplied
            generator doesn't expose ``.edit(...)``.
    """
    if not isinstance(instruction, str) or not instruction.strip():
        raise GenerationError("instruction must be a non-empty string")

    edit = getattr(generator, "edit", None)
    if not callable(edit):
        raise GenerationError(
            "generator is missing `edit(...)` — pass a MAGEGenerator or a "
            "duck-typed equivalent"
        )

    try:
        return await edit(
            instruction,
            chain=chain,
            entity_id=entity_id,
            channel=channel,
            save=save,
            cancel=cancel,
        )
    except GenerationError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise GenerationError(f"edit() raised: {exc}") from exc


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


__all__ = [
    "GenerationError",
    "build_mage_config",
    "build_mage_generator",
    "run_generation",
    "run_edit",
]
