"""CARE configuration model (TODO §2 P0).

`CareConfig` is the single source of truth for every knob CARE reads at
startup: connection details for MAGE / Memory / Platform, sandbox
backend choice + resource limits, and UI defaults. The loader pulls
values from three places in priority order:

    1. ``$CARE_*`` environment variables (highest precedence)
    2. ``~/.config/care/config.toml``
    3. Pydantic field defaults

The TOML file follows the nested-section layout described in
``TODO.md §2``::

    [mage]
    mode = "deep"
    provider = "openai"
    api_key = "sk-..."
    base_url = "https://api.openai.com/v1"
    enable_memory_research = true
    enable_memory = false
    enable_web_research = false

    [memory]
    base_url = "http://localhost:8000"
    api_key = "sk-..."

    [platform]
    base_url = "http://localhost:8000"
    api_key = "sk-..."

    [sandbox]
    kind = "docker"
    cpu_limit = 2.0
    mem_limit = "1g"
    network_policy = "skill_declared"
    image = "python:3.12-slim"

    [defaults]
    language = "en"
    max_history_entries = 50

Environment variables use double-underscore nesting:
``CARE_MAGE__MODE=fast``, ``CARE_MEMORY__BASE_URL=http://...``, etc.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import tomllib
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class StagePolicy(StrEnum):
    """How a configurable chat-pipeline stage behaves for a given mode.

    Canonical home is the config layer so both `care.config` (the
    override surface) and `care.screens.chat` (the presets + driver)
    share one definition without a screens→config import inversion.
    """

    AUTO = "auto"   # do it silently
    ASK = "ask"     # show a ConfirmModal gate first
    SKIP = "skip"   # never do it (and skip stages that depend on it)


DEFAULT_CONFIG_PATH = Path("~/.config/care/config.toml").expanduser()
"""Default user-global path searched by :meth:`CareConfig.load`."""

PROJECT_CONFIG_FILENAME = "care.toml"
"""Filename CARE looks for in the current working directory for
per-project overrides. Sits next to user-global TOML in the
precedence stack: env > project > user > defaults."""

ENV_PREFIX = "CARE_"
"""Prefix for environment-variable overrides. Nested fields use ``__``
as the separator (so ``CARE_MAGE__MODE=fast`` sets ``mage.mode``)."""


class MageConfig(BaseModel):
    """MAGE generator settings.

    Field set mirrors ``mmar_mage.MAGEConfig`` but is intentionally
    duck-typed: CARE doesn't import ``mmar_mage`` at config-load time
    so a misconfigured MAGE install can't break startup. The values
    are forwarded into ``MAGEConfig`` later via :meth:`to_dict`.
    """

    model_config = ConfigDict(extra="allow", validate_assignment=True)

    mode: Literal["fast", "deep"] = "deep"
    api_key: str | None = None
    base_url: str | None = None
    model: str | None = None

    enable_memory_research: bool = True
    """Stage 0a recall: before planning, MAGE expands the task into
    sub-queries, recalls relevant past chains/knowledge from Memory, and
    feeds a digest to the planner — so generation gets smarter as Memory
    fills (see C5's save-on-success). Costs ~one extra sub-query LLM call
    per generation (cheap on cold start; the Memory search itself is a
    fast local call). On by default since the point is to use Memory;
    set ``CARE_MAGE__ENABLE_MEMORY_RESEARCH=false`` to skip it for the
    lowest-latency path."""

    enable_memory: bool = False
    """Implicit MAGE write-back after generation.

    CARE keeps this off by default because Interactive generation is a preview,
    not consent to persist, and Production has its own explicit SAVE stage.
    Memory recall remains independently controlled by
    ``enable_memory_research``.
    """

    memory_search_mode: Literal["bm25", "vector", "hybrid"] = "bm25"
    """Strategy for Memory recall. Default ``bm25`` — the bundled Memory
    deployment ships with vector search OFF, so ``hybrid``/``vector`` just
    fall back to bm25 with a noisy per-search warning. Flip to ``hybrid``
    once Memory has vector search + sentence-transformers enabled."""

    memory_relevance_threshold: float = 0.0
    """Minimum relevance score for a recalled chain to be kept. Default 0.0
    because bm25 scores are small (~0.1) and a higher gate silently drops
    every real hit (perpetual cold-start). Quality is still enforced
    downstream by MAGE's applicability gate (an LLM judge)."""

    enable_web_research: bool = False
    web_search_provider: Literal[
        "tavily", "serper", "exa", "serpapi", "brave", "duckduckgo"
    ] = "tavily"
    """Backend MAGE's web-research agent uses.

    Supported values are Tavily, Serper, Exa, SerpAPI, Brave and the
    unauthenticated DuckDuckGo fallback.
    Mirrors :attr:`ToolsConfig.web_search_provider` so generation-time
    research and the runtime ``web_search`` tool share one engine."""
    web_search_api_key: str | None = None

    enable_needs_analysis: bool = False
    """Run the opt-in planning pass that identifies required tools, skills,
    non-secret human inputs and credential *names*. The pass only receives
    MAGE's redacted request projection; file paths, file contents and secret
    values are excluded."""

    # Hard per-generation limits. ``None`` preserves provider defaults; once
    # set, MAGE fails closed before the next call would exceed the cap.
    max_llm_calls: int | None = Field(default=None, gt=0)
    max_input_tokens: int | None = Field(default=None, gt=0)
    max_output_tokens: int | None = Field(default=None, gt=0)
    max_total_tokens: int | None = Field(default=None, gt=0)
    max_wall_time_seconds: float | None = Field(default=None, gt=0.0)
    max_cost_usd: float | None = Field(default=None, gt=0.0)
    stage_max_output_tokens: dict[str, int] = Field(default_factory=dict)
    """Optional output-token ceilings keyed by MAGE stage name."""

    # MAGE planning-stage discovery (forwarded to mmar_mage.MAGEConfig).
    # CARE normally INJECTS capabilities, which bypasses MAGE's internal
    # CapabilityLookupAgent — so these default off. Flip them for
    # deployments that want MAGE to discover tools/skills itself.
    enable_capability_lookup: bool = False
    """Run MAGE's CapabilityLookupAgent at plan time (ignored when CARE
    injects capabilities, which is the normal path)."""
    enable_memory_skill_lookup: bool = False
    """Let MAGE query Memory for ``agent_skill`` entities during
    capability lookup. Needs MAGE's memory client working (Phase C)."""
    enable_skill_discovery: bool = False
    """Surface packaged AgentSkills via MAGE's built-in SkillRegistry."""

    # Chain topology + depth (forwarded to mmar_mage.MAGEConfig). MAGE's own
    # defaults bias HARD toward short linear chains — ``simplicity_bias=0.5``
    # halves the analyzer's suggested step count (floored at 2) and
    # ``enable_topology_selection=False`` means every plan is the default
    # linear ``pipeline``. Together that collapses most generations to a
    # 2-step tool→llm chain. CARE overrides both so generation can pick richer
    # shapes (diamond / map_reduce / tree_of_thought / hierarchical / …) with
    # appropriate depth. Non-linear shapes whose step types the *installed*
    # CARL can't execute are downgraded to ``llm`` at run time (the DAG shape
    # survives) by :func:`care.tool_planning.downgrade_unsupported_step_types`.
    enable_topology_selection: bool = True
    """Run MAGE's TopologySelectorAgent so the planner is hinted toward a
    task-appropriate topology (pipeline / diamond / funnel / map_reduce /
    tree_of_thought / mixture_of_experts / hierarchical / …) instead of always
    a linear pipeline. One extra LLM call per generation."""
    topology_max_candidates: int = Field(default=3, ge=1, le=8)
    """How many ranked topology candidates the selector keeps. >1 enables
    parallel topology sampling only when MAGE's own flag is set; here it just
    widens the hint pool."""
    simplicity_bias: float = Field(default=0.2, ge=0.0, le=1.0)
    """Scales the analyzer's suggested step count by ``(1 - bias)`` (floored
    at 2). MAGE defaults to 0.5 (aggressive shortening); CARE lowers it to
    0.2 so non-trivial tasks keep enough steps to form a real topology. Set
    0.0 to fully trust the analyzer, 0.5+ to force short chains."""
    simplicity_max_steps: int = Field(default=7, ge=2, le=30)
    """Hard cap on plan length — a plan exceeding this *without* a filled
    rationale is truncated. Raise for deliberately long pipelines."""


class MemoryConfig(BaseModel):
    """GigaEvo Memory connection."""

    model_config = ConfigDict(validate_assignment=True)

    base_url: str = "http://localhost:8000"
    api_key: str | None = None
    timeout: float = Field(default=30.0, gt=0)


class PlatformConfig(BaseModel):
    """GigaEvo Platform connection.

    The Platform itself runs two LLMs CARE doesn't speak to directly:

    * **Mutation** — the GA engine that proposes new chain variants
      each generation. Higher quality here = better evolution candidates.
    * **Validation** — the judge that scores each candidate against the
      reference dataset. Determines the GA's fitness signal.

    CARE doesn't call these models — the Platform does. We carry the
    URL / model id / api key so the wizard can stamp them into the
    Platform's ``llm_models.yml`` and CARE's
    :meth:`care.platform.CarePlatform.start_evolution` can pass the
    right model ids in the experiment spec.
    """

    model_config = ConfigDict(validate_assignment=True)

    base_url: str = "http://localhost:8000"
    """Primary Platform URL. Point at **master-api** (default port 8000).

    Legacy setups sometimes used runner-api on port 8001; CARE
    auto-redirects control-plane calls to :attr:`master_base_url`
    when ``base_url`` ends in ``:8001``."""
    master_base_url: str = "http://localhost:8000"
    """Control-plane URL (dataset upload, experiment create).

    Used automatically when :attr:`base_url` still points at
    runner-api on port 8001."""
    api_key: str | None = None
    timeout: float = Field(default=30.0, gt=0)

    # Platform-side LLMs the Platform runs (not CARE). CARE passes
    # the model ids ("care-validation", "care-mutation") in each
    # experiment spec; the Platform looks them up against its own
    # ``llm_models.yml`` (seeded by the wizard from these fields).
    validation_base_url: str = "https://openrouter.ai/api/v1"
    validation_model: str = "tngtech/deepseek-r1t-chimera:free"
    validation_api_key: str | None = None
    mutation_base_url: str = "https://openrouter.ai/api/v1"
    mutation_model: str = "tngtech/deepseek-r1t-chimera:free"
    mutation_api_key: str | None = None
    mutation_max_tokens: int = Field(default=8192, gt=0)
    """Max completion tokens for the mutation LLM (chain proposals).

    Written into Platform ``llm_models.yml`` as ``care-mutation``'s
    ``max_tokens``. Evolution launch can override per run."""
    validation_max_tokens: int = Field(default=2048, gt=0)
    """Max completion tokens for the validation / judge LLM."""


class HubConfig(BaseModel):
    """carl-agent-server hub — where chains get deployed as HTTP agents.

    The hub is one lightweight process hosting N deployed chains, each
    mounted at ``/agents/<name>`` with its own Swagger
    (``/agents/<name>/docs``). CARE's ``/deploy`` talks to its control
    API (``POST /deployments`` …); when the hub is down and
    ``autostart`` is on, CARE spawns ``agent_server_cmd`` as a detached
    subprocess and waits for ``/healthz``.

    ``base_url`` and ``port`` must agree — autostart passes ``--port``
    from here and the client talks to ``base_url``.
    """

    model_config = ConfigDict(validate_assignment=True)

    base_url: str = "http://127.0.0.1:8080"
    autostart: bool = True
    port: int = Field(default=8080, gt=0, lt=65536)

    state_file: str = "~/.care/agent-hub.json"
    """Where the hub persists deployments between restarts."""

    agent_server_cmd: list[str] = Field(
        default_factory=lambda: ["carl-agent-hub", "serve"]
    )
    """Command CARE spawns when autostarting the hub. ``--port`` and
    ``--state-file`` are appended from this config."""

    start_timeout: float = Field(default=15.0, gt=0)
    """How long to wait for /healthz after an autostart."""

    timeout: float = Field(default=30.0, gt=0)
    """Per-request timeout for control-API calls."""


class UploadConfig(BaseModel):
    """External chain-upload target (Phase 3 P2).

    Wired by the chat surface's `/upload <chain_id>` slash
    command: fetches the saved chain from Memory and POSTs the
    JSON body to ``url`` so a downstream deployment service
    (CARE-companion bot, internal "agent shelf", etc.) can
    pick it up. Empty ``url`` disables the command — `/upload`
    surfaces an actionable "config missing" line so the user
    knows what to set.
    """

    model_config = ConfigDict(validate_assignment=True)

    url: str = ""
    """POST endpoint that receives the chain JSON. Empty
    disables `/upload`."""

    api_key: str | None = None
    """Optional bearer token sent in the ``auth_header``."""

    auth_header: str = "Authorization"
    """Header name carrying the credential. Default fits the
    canonical OAuth-style ``Authorization: Bearer <key>``; some
    services want ``X-API-Key`` instead — that's a single env
    flip away."""

    timeout: float = Field(default=30.0, gt=0)


class SandboxConfig(BaseModel):
    """AgentSkill sandbox backend.

    ``kind`` selects the backend used by CARE's runtime executor when
    a chain hits an ``agent_skill`` step. ``"local"`` is unsafe — only
    appropriate for CARE-internal testing — and the loader warns when
    it's picked in a production-looking config.
    """

    model_config = ConfigDict(validate_assignment=True)

    kind: Literal["local", "docker", "e2b", "firejail"] = "docker"
    cpu_limit: float = Field(default=2.0, gt=0)
    mem_limit: str = "1g"
    network_policy: Literal["none", "skill_declared", "open"] = "skill_declared"
    image: str = "python:3.12-slim"
    pids_limit: int = Field(default=256, gt=0)

    @field_validator("mem_limit")
    @classmethod
    def _validate_mem_limit(cls, v: str) -> str:
        """Accept Docker-style memory strings (e.g. ``512m``, ``1g``,
        ``2048k``)."""
        s = v.strip().lower()
        if not s or not s[:-1].isdigit() or s[-1] not in {"k", "m", "g"}:
            raise ValueError(
                f"mem_limit must look like '512m', '1g', or '2048k'; got {v!r}"
            )
        return s


class ToolsConfig(BaseModel):
    """User-supplied ``@carl_tool`` directory.

    CARL's :meth:`ReasoningContext.register_tools_from_path` discovers
    every ``@carl_tool``-decorated callable under a glob, so CARE only
    needs to remember where the user keeps theirs. ``path`` points at
    the directory; the loader globs ``*.py`` underneath at startup.
    Empty/missing dirs are normal (first-run users have no tools yet)
    so the loader treats "no files" as a silent no-op rather than an
    error.
    """

    model_config = ConfigDict(validate_assignment=True)

    path: Path = Field(default=Path("~/.config/care/tools"))
    tag_filter: list[str] | None = None
    """Optional whitelist — only tools whose ``@carl_tool(tags=[...])``
    intersect this set get registered. ``None`` registers all."""
    name_prefix: str = ""
    """Optional prefix prepended to every registered tool name.
    Handy when sourcing third-party tools so they don't collide with
    built-ins."""

    enable_builtins: bool = True
    """Register CARE's bundled standard tools (``web_search``,
    ``fetch_url``, ``calculator``, ``current_datetime``) into every
    execution context so MAGE-generated chains that reference them can
    actually run. Set ``False`` to ship a fully bring-your-own-tools
    deployment. See :mod:`care.builtin_tools`."""

    web_search_provider: Literal[
        "tavily", "serper", "exa", "serpapi", "brave", "duckduckgo"
    ] = "tavily"
    """Backend for the bundled ``web_search`` tool. Mirrors MAGE's
    ``web_search_provider`` so the runtime tool and generation-time
    research can share a key."""

    web_search_api_key: str | None = None
    """API key for the primary :attr:`web_search_provider`. ``None`` is
    fine — ``web_search`` falls back to keyless DuckDuckGo, so it works
    out of the box. Set ``CARE_TOOLS__WEB_SEARCH_API_KEY`` to use the
    chosen provider; see :attr:`search_provider_keys` for cross-provider
    fallback keys."""

    search_provider_keys: dict[str, str] = Field(default_factory=dict)
    """Optional per-provider API keys (e.g. ``{"serper": "…", "exa":
    "…"}``) so ``web_search`` can fall back between engines when the
    primary fails. The primary's key still comes from
    :attr:`web_search_api_key`; DuckDuckGo needs no key and is always the
    final fallback. Configure under ``[tools.search_provider_keys]``."""

    @field_validator("search_provider_keys", mode="before")
    @classmethod
    def _coerce_search_provider_keys(cls, v: Any) -> Any:
        """Tolerate configs written by the old serializer bug that
        stringified an empty dict to ``"{}"`` (and env vars, which always
        arrive as strings). A JSON-object string parses into a dict; a
        blank or non-object string falls back to an empty dict so a
        stale ``config.toml`` boots instead of crashing at load."""
        if isinstance(v, str):
            s = v.strip()
            if not s:
                return {}
            try:
                parsed = json.loads(s)
            except (ValueError, TypeError):
                return {}
            return parsed if isinstance(parsed, dict) else {}
        return v

    @field_validator("tag_filter", mode="before")
    @classmethod
    def _coerce_tag_filter(cls, v: Any) -> Any:
        """Env vars always arrive as strings, and ``.env.example`` documents
        ``CARE_TOOLS__TAG_FILTER`` as a tag list — accept a bare or
        comma-joined string (``"foo"`` -> ``["foo"]``, ``"a,b"`` ->
        ``["a", "b"]``) so a documented setting doesn't crash
        ``CareConfig.load()`` with a list_type ValidationError. Blank ->
        ``None`` (register all tools)."""
        if isinstance(v, str):
            tokens = [t.strip() for t in v.split(",") if t.strip()]
            return tokens or None
        return v

    web_search_max_results: int = Field(default=5, ge=1, le=20)
    """How many results the bundled ``web_search`` returns per call."""

    fetch_url_max_chars: int = Field(default=4000, ge=200, le=200_000)
    """Character cap on the text ``fetch_url`` returns — keeps a single
    page from blowing the downstream LLM step's context budget. Also caps
    ``http_request`` / ``run_python`` output."""

    enable_code_exec: bool = True
    """Register the ``run_python`` builtin — lets a chain synthesise and
    run arbitrary Python on the fly ("generate a tool"). Executed inside
    the :class:`SandboxConfig` Docker sandbox, **not** in CARE's process.
    Set ``False`` to drop the code-exec surface while keeping
    ``web_search`` / ``fetch_url`` / ``http_request``."""

    code_exec_timeout: int = Field(default=60, ge=5, le=600)
    """Wall-clock seconds a single ``run_python`` sandbox call may run
    before the container is force-killed."""

    auto_synthesize_tools: bool = True
    """Self-healing tool synthesis: before a generated chain runs, scan
    it for ``tool`` steps whose ``tool_name`` isn't registered, ask the
    LLM to write an implementation, and register it as a sandboxed
    callable — so a planner that invents ``get_current_weather`` gets a
    working tool instead of a "not registered" failure. Requires
    :attr:`enable_code_exec` (synthesised tools run in the Docker
    sandbox). See :mod:`care.tool_synthesis`."""

    synthesized_tools_path: Path = Field(default=Path("~/.config/care/synthesized_tools"))
    """Disk cache for synthesised tools. Each tool is stored as
    ``<name>.json`` (source + metadata), re-registered on every run, and
    advertised to MAGE — so a tool is generated once and reused
    thereafter instead of regenerated each run."""

    save_synthesized_to_memory: bool = True
    """Also persist each newly-synthesised tool to GigaEvo Memory (as an
    ``agent_skill`` entity) when Memory is configured, and search Memory
    for an existing implementation before generating a new one. Lets
    synthesised tools be shared/reused across machines + sessions.
    Best-effort — a Memory outage never blocks a run."""

    recall_tools_from_memory: bool = True
    """At generation time, search GigaEvo Memory for previously-saved
    tools (``agent_skill`` entities tagged ``care:synthesized-tool``)
    relevant to the task and advertise them to MAGE — so a tool
    synthesised in an earlier session is reused by name instead of
    re-invented. Best-effort: a Memory outage is swallowed. See
    :func:`care.capability_priming.build_capabilities_for_generation`."""

    route_live_data_to_tools: bool = True
    """Before running an all-LLM chain, ask the model whether any step
    actually needs live/external data (current date, weather, news,
    prices, web facts) that a language model would hallucinate. If so,
    rewrite that step into a ``tool`` call — reusing a registered tool
    when one fits (e.g. ``current_datetime``), else letting tool
    synthesis create one. Stops "what's today's date" from being
    answered from the model's stale memory. See :mod:`care.tool_planning`."""

    ground_synthesis_with_web_search: bool = True
    """Before synthesising a missing tool from scratch, ``web_search`` the
    internet for a real keyless public API for the capability and feed the
    findings into the codegen prompt — so the generated tool calls a REAL,
    current endpoint instead of one the LLM guessed from stale training
    (which is how synthesis silently produced dead URLs for anything outside
    its hardcoded weather/geocode examples). Needs :attr:`web_search_api_key`;
    degrades gracefully to ungrounded synthesis when search is unavailable.
    See :func:`care.tool_synthesis.synthesize_missing_tools`."""

    self_test_synthesized_tools: bool = True
    """After synthesising a tool, run it once in the sandbox with the model's
    own sample inputs; if it returns a runtime ``error:`` (e.g. a wrong
    endpoint path), regenerate ONCE with the failure fed back. Turns "found
    the right API provider but mis-built the URL" into a working tool. Costs
    one extra sandbox run (+ at most one extra codegen) per newly-synthesised
    tool; reused/cached tools are re-tested per :attr:`verify_cached_tools`."""

    verify_cached_tools: bool = True
    """Health-check a cached / Memory-recalled synthesised tool BEFORE
    reusing it (and only advertise verified-good ones to MAGE) — re-run its
    self-test in the sandbox and, if it now errors (a stale endpoint, or a
    tool that only ever worked for one hardcoded input), drop it and
    re-synthesise instead of silently running a broken tool. Closes the gap
    left by :attr:`self_test_synthesized_tools`, which only tests *fresh*
    synthesis. A verdict that can't be reached (no sample inputs / sandbox
    hiccup) never marks a tool bad — we only drop tools that actually
    errored. Verdicts are cached on disk for :attr:`cached_tool_health_ttl_s`
    so the sandbox runs at most once per tool per window."""

    cached_tool_health_ttl_s: int = Field(default=86400, ge=0)
    """How long (seconds) a cached tool's health verdict stays trusted
    before it's re-verified on next use. ``0`` re-verifies on every use."""


class TelemetryConfig(BaseModel):
    """Opt-in event-stream recording (TODO §9 P3).

    CARE emits structured events for each MAGE generation, CARL
    run, and Platform evolution. With telemetry **off** (the
    default), the recording layer is a no-op — zero network /
    file I/O. Flip ``enabled=True`` and pick a ``backend`` to
    stream events to an external dashboard.

    The only built-in backend so far is ``langfuse`` (the
    library the TODO calls out). Custom backends register via
    :func:`care.runtime.register_telemetry_backend`.
    """

    model_config = ConfigDict(validate_assignment=True)

    enabled: bool = False
    backend: str = "null"
    """``null`` is the no-op default. ``langfuse`` enables the
    lazy-imported Langfuse SDK backend. Custom backends register
    by name via the telemetry factory registry."""
    host: str = "https://cloud.langfuse.com"
    public_key: str | None = None
    secret_key: str | None = None


class DefaultsConfig(BaseModel):
    """UI defaults."""

    model_config = ConfigDict(validate_assignment=True)

    language: Literal["en", "ru"] = "en"
    """Language CARL (the agent) is told to answer in — forwarded to the
    reasoning engine in ``runtime/executor.py``. Independent of the TUI's
    own language (see :attr:`ui_language`)."""

    ui_language: Literal["ru", "en"] = "ru"
    """Language of the TUI itself (labels, system messages, settings,
    help). Defaults to Russian. Read at render time via
    ``care.runtime.i18n``; changing it does NOT change the agent's answer
    language (that's :attr:`language`). Env: ``CARE_DEFAULTS__UI_LANGUAGE``."""

    dag_ascii: bool = False
    """Draw chain DAGs with plain ASCII glyphs (``+ - | v <``) instead of
    Unicode box-drawing. For terminals/fonts that can't render the box
    glyphs, or for clean copy-paste. Read by every DAG surface (chat
    trail, inspect pane, run overlay, DAG modal). Env:
    ``CARE_DEFAULTS__DAG_ASCII``."""

    dag_bus_lanes: bool = False
    """Draw multi-layer "skip" dependencies as routed left-margin channels
    instead of terse ``◀ N`` annotations on the dependent box. Honoured by
    every DAG surface. Env: ``CARE_DEFAULTS__DAG_BUS_LANES``."""

    dag_layout: Literal["tb", "lr"] = "tb"
    """Initial orientation of the DAG modal's graph: ``tb`` (top-down) or
    ``lr`` (left-to-right — a deep linear chain becomes a wide strip
    instead of a tall column). Toggle live in the modal with ``l``. Env:
    ``CARE_DEFAULTS__DAG_LAYOUT``."""

    max_history_entries: int = Field(default=50, ge=1, le=10_000)

    reduced_motion: bool = False
    """Disable UI animations (chat-line fade-in, modal/toast reveals, smooth
    scroll, stage-trail transitions). When ``True`` the app forces Textual's
    animation level to ``"none"`` so every ``styles.animate()`` call + CSS
    ``transition`` resolves instantly to its final value — useful over slow
    SSH links or for motion-sensitivity. The ``TEXTUAL_ANIMATIONS=none`` env
    var has the same effect at the Textual layer. Env:
    ``CARE_DEFAULTS__REDUCED_MOTION``."""


class ChatStageConfig(BaseModel):
    """Per-mode override of the four configurable pipeline stages.

    ``None`` means "defer to the mode preset"; a set value overrides it.
    Precedence: preset default ← config override (single level).
    """

    model_config = ConfigDict(validate_assignment=True)

    run: StagePolicy | None = None
    save: StagePolicy | None = None
    baseline: StagePolicy | None = None
    evolve: StagePolicy | None = None
    # Overridden via e.g. ``CARE_CHAT__MODE__INTERACTIVE__RUN=auto``.


class ChatModeConfig(BaseModel):
    """Stage-policy overrides per chat mode. Env vars nest as
    ``CARE_CHAT__INTERACTIVE_RUN`` / ``CARE_CHAT__PRODUCTION_SAVE`` …"""

    model_config = ConfigDict(validate_assignment=True)

    interactive: ChatStageConfig = Field(default_factory=ChatStageConfig)
    production: ChatStageConfig = Field(default_factory=ChatStageConfig)
    # Env vars nest with the ``__`` delimiter through every level, e.g.
    # ``CARE_CHAT__MODE__INTERACTIVE__RUN=auto`` /
    # ``CARE_CHAT__MODE__PRODUCTION__SAVE=ask``.


class ChatConfig(BaseModel):
    """ChatScreen surface (Phase 1).

    Holds knobs the chat surface reads at construction time —
    the default mode plus per-mode pipeline-stage overrides. Other
    chat-specific env vars (loop budget, collapse thresholds, session
    log dir, tutorial sidecar) are resolved by helper functions on
    `ChatScreen` directly because they're either UI-tuning knobs or
    sidecar paths, not data the rest of the stack needs to read.
    """

    model_config = ConfigDict(validate_assignment=True)

    default_mode: Literal["interactive", "production"] = "interactive"
    """Boot-time mode for the chat surface. Aliases like
    ``ad-hoc`` / ``adhoc`` / ``prod`` are still accepted by the
    runtime resolver (``_resolve_default_mode``); only canonical
    ids land in the typed config. The legacy ``ad_hoc`` value is
    normalised to ``interactive`` on read (see the validator below)."""

    @field_validator("default_mode", mode="before")
    @classmethod
    def _normalise_default_mode(cls, value: object) -> object:
        """Accept the legacy ``ad_hoc`` spellings from persisted config /
        env and map them onto the canonical ``interactive`` literal."""
        if isinstance(value, str):
            legacy = {"ad_hoc": "interactive", "ad-hoc": "interactive",
                      "adhoc": "interactive"}
            return legacy.get(value.strip().lower(), value)
        return value

    mode: ChatModeConfig = Field(default_factory=ChatModeConfig)
    """Per-mode pipeline-stage policy overrides. ``None`` fields defer to
    the mode preset (`MODE_SPECS`); see `resolve_mode_spec`. Env vars nest
    as ``CARE_CHAT__MODE__INTERACTIVE__RUN``, ``CARE_CHAT__MODE__PRODUCTION__SAVE``, …"""

    interactive_save_trim: bool = False
    """Decision 1 — when ``True``, an Interactive save persists a trimmed
    chain definition rather than the full one. Off by default; the binary
    Save affordance never forks into a "full vs trimmed" dialog (trimming is
    an optimisation, not an inline decision). Env:
    ``CARE_CHAT__INTERACTIVE_SAVE_TRIM``. The trim transform itself is
    deferred — this is the opt-in knob."""


class ContextConfig(BaseModel):
    """Long-term user/project context — the CARE.md file (P1.1) + CARL LTM.

    CARE reads a global ``~/.config/care/CARE.md`` plus a per-project
    ``./CARE.md`` and injects them as a standing context block into
    generation — the chain-building analog of Claude Code's CLAUDE.md (user
    preferences, recurring domain, constraints). Missing/empty files are a
    silent no-op. See :mod:`care.context_md`. CARL's native long-term memory
    (``JsonFileLTM``) is attached to every run + injected into the answer and
    the planner prompt, and updated post-turn by the save-decision pass.
    """

    model_config = ConfigDict(validate_assignment=True)

    enabled: bool = True
    """Load CARE.md context at all. ``False`` never reads the files."""
    global_path: Path = Field(default=Path("~/.config/care/CARE.md"))
    """User-global context file, applied to every project."""
    project_filename: str = "CARE.md"
    """Per-project context file looked up in the working directory; it
    augments/overrides the global file."""
    max_chars: int = Field(default=8000, ge=200, le=200_000)
    """Cap on the merged context so a large CARE.md can't dominate the
    generation prompt."""

    auto_learn_facts: bool = False
    """P5.6 — after a turn, extract durable user facts (role, preferences,
    recurring constraints) and write them into the global CARE.md's
    "## Auto-learned facts" section (deduped / superseded). Default OFF — it
    costs one extra LLM call per turn and writes to your CARE.md."""

    # CARL's native LTM (JsonFileLTM). CARE attaches it to every execution +
    # generation context, ALWAYS injects a recalled digest into the answer and
    # the planner prompt, and runs a post-turn save-decision to persist durable
    # facts under named keys (recall via ``$ltm.<key>`` / a memory step too).
    ltm_enabled: bool = True
    """Attach a long-term-memory store + inject/update it. ``False`` skips LTM."""
    ltm_dir: Path = Field(default=Path("~/.config/care/ltm"))
    """Directory for the ``JsonFileLTM`` store (one ``<session_id>.json`` per
    session). Persists across runs."""
    ltm_session_id: str = "default"
    """LTM scope key — contexts sharing it share the same long-term memory."""

    ltm_inject_max_chars: int = Field(default=2000, ge=0, le=20_000)
    """Cap on the recalled-LTM digest injected into answer/planner prompts."""
    ltm_autosave: bool = True
    """Run the post-turn save-decision pass that persists durable facts to LTM.
    ``False`` keeps LTM read-only (still injected, never auto-written)."""


class ArtifactsConfig(BaseModel):
    """P6.5 — where chain/skill output files land.

    A chain or AgentSkill that produces files (.pptx/.xlsx/.docx/…) writes
    them to a sandbox ``/workspace/out`` and surfaces them on each step's
    ``output_files``. CARE copies them OUT of that throwaway dir into a
    stable, cross-platform directory the user can open, then shows a
    ``📄 saved: <path>`` line. See :mod:`care.runtime.artifacts`.
    """

    model_config = ConfigDict(validate_assignment=True)

    dir: Path | None = None
    """Root directory for saved artifacts (``CARE_ARTIFACTS__DIR``). When
    unset, defaults to ``Path.home() / ".care" / "artifacts"`` — resolved at
    save time so it follows the running user's home on any OS (Windows /
    macOS / Linux). ``~`` is expanded."""


def _looks_like_jwt(value: str) -> bool:
    """Heuristic: a JWT is three dot-separated base64url segments and
    almost always starts with ``eyJ`` (base64 of ``{"``). Catches the
    common "pasted the API key into a *_model slot" mistake."""
    if not isinstance(value, str):
        return False
    return value.startswith("eyJ") and value.count(".") == 2


def _is_probable_url(value: str) -> bool:
    """True when ``value`` parses as an http(s) URL with a host."""
    if not isinstance(value, str) or not value.strip():
        return False
    from urllib.parse import urlparse

    try:
        parsed = urlparse(value)
    except (ValueError, TypeError):
        return False
    return parsed.scheme in ("http", "https") and bool(parsed.netloc)


class CareConfig(BaseModel):
    """Top-level CARE configuration."""

    model_config = ConfigDict(validate_assignment=True, extra="forbid")

    mage: MageConfig = Field(default_factory=MageConfig)
    memory: MemoryConfig = Field(default_factory=MemoryConfig)
    platform: PlatformConfig = Field(default_factory=PlatformConfig)
    hub: HubConfig = Field(default_factory=HubConfig)
    upload: UploadConfig = Field(default_factory=UploadConfig)
    sandbox: SandboxConfig = Field(default_factory=SandboxConfig)
    tools: ToolsConfig = Field(default_factory=ToolsConfig)
    telemetry: TelemetryConfig = Field(default_factory=TelemetryConfig)
    defaults: DefaultsConfig = Field(default_factory=DefaultsConfig)
    chat: ChatConfig = Field(default_factory=ChatConfig)
    context: ContextConfig = Field(default_factory=ContextConfig)
    artifacts: ArtifactsConfig = Field(default_factory=ArtifactsConfig)

    def audit_fields(self) -> list[str]:
        """Return human-readable warnings for obviously-misconfigured
        fields — e.g. a ``*_base_url`` that doesn't parse as an http(s)
        URL, or a ``*_model`` that looks like a JWT (the API key was
        pasted into the wrong slot). Returns an empty list when nothing
        looks wrong. Surfaced by ``care doctor`` so silent mistakes get
        flagged instead of happily printed."""
        warnings: list[str] = []

        url_fields = [
            ("mage.base_url", self.mage.base_url),
            ("memory.base_url", self.memory.base_url),
            ("platform.base_url", self.platform.base_url),
            ("platform.master_base_url", self.platform.master_base_url),
            ("platform.validation_base_url", self.platform.validation_base_url),
            ("platform.mutation_base_url", self.platform.mutation_base_url),
            ("hub.base_url", self.hub.base_url),
        ]
        for name, value in url_fields:
            if not value:
                continue
            if _looks_like_jwt(value):
                warnings.append(
                    f"{name} looks like a JWT/API key, not a URL — did you "
                    f"paste the API key into the URL slot?"
                )
            elif not _is_probable_url(value):
                warnings.append(
                    f"{name} = {value!r} doesn't parse as an http(s) URL"
                )

        model_fields = [
            ("mage.model", self.mage.model),
            ("platform.validation_model", self.platform.validation_model),
            ("platform.mutation_model", self.platform.mutation_model),
        ]
        for name, value in model_fields:
            if value and _looks_like_jwt(value):
                warnings.append(
                    f"{name} looks like a JWT/API key, not a model name — "
                    f"did you paste the API key into the model slot?"
                )
        return warnings

    @classmethod
    def load(
        cls,
        *,
        path: Path | None = None,
        env: dict[str, str] | None = None,
        cwd: Path | None = None,
        resolve_secrets: bool = True,
    ) -> "CareConfig":
        """Load a config with layered precedence.

        Layers, lowest to highest precedence:

        1. Pydantic field defaults.
        2. User-global TOML at :data:`DEFAULT_CONFIG_PATH`
           (``~/.config/care/config.toml``).
        3. Per-project TOML at ``<cwd>/care.toml`` —
           per-project knobs win over user-global. Empty when
           the file doesn't exist.
        4. ``CARE_*`` environment variables.

        When ``path`` is supplied explicitly the project-file
        lookup is skipped — the caller's path is treated as the
        single source of truth. (Tests + CLI flags like
        ``care --config foo.toml`` use this to avoid surprise
        merging.)

        Args:
            path: Override the config-file location. ``None``
                reads :data:`DEFAULT_CONFIG_PATH` and then layers
                ``<cwd>/care.toml`` on top.
            env: Override ``os.environ`` for testing. ``None``
                uses the real environment.
            cwd: Directory to search for the per-project
                ``care.toml``. ``None`` uses :func:`Path.cwd`.
                Ignored when ``path`` is supplied.
            resolve_secrets: Dereference ``keystore://`` credential fields.
                The native resident Harness sets this to ``False`` in both
                processes and resolves the fixed credential allowlist through
                its private in-memory broker instead.

        Returns:
            A validated ``CareConfig`` instance. Raises
            ``pydantic.ValidationError`` if any field fails its
            constraint (e.g. ``sandbox.mem_limit="1xb"``).
        """
        if path is not None:
            data = _read_toml(path)
        else:
            user_data = _read_toml(DEFAULT_CONFIG_PATH)
            if cwd is None:
                from care.runtime.workspace_context import current_workspace_root

                project_root = current_workspace_root()
            else:
                project_root = cwd
            project_path = project_root / PROJECT_CONFIG_FILENAME
            project_data = _read_toml(project_path)
            data = _deep_merge(user_data, project_data)
        if env is None:
            env = dict(os.environ)
        env_overrides = _parse_env(env)
        merged = _deep_merge(data, env_overrides)
        _sanitize_chat_default_mode(merged)
        config = cls.model_validate(merged)
        # §1 P0 — dereference any `keystore://service/key` URLs
        # in the api_key fields. Literals + None pass through
        # unchanged; failures (malformed URL, missing entry)
        # leave the field empty + log a warning so the user
        # sees a "service not configured" toast rather than a
        # crash. Write-side migration ships separately.
        if resolve_secrets:
            _resolve_api_key_secrets(config)
        return config

    def save_to_disk(
        self,
        path: Path | None = None,
        *,
        store_secrets: bool = True,
    ) -> Path:
        return self.save_to_disk_with_report(
            path, store_secrets=store_secrets,
        ).path

    def save_to_disk_with_report(
        self,
        path: Path | None = None,
        *,
        store_secrets: bool = True,
    ) -> "ConfigSaveReport":
        """Serialise this config to TOML and write it to disk.

        Used by the SettingsScreen Save action so first-run users
        actually persist their edits — without this method
        ``CareConfig.load()`` keeps re-reading the missing file
        and the user's edits never survive a session.

        Args:
            path: Override the destination. ``None`` writes to
                :data:`DEFAULT_CONFIG_PATH`
                (``~/.config/care/config.toml``); the parent dir
                is created if missing.
            store_secrets: When ``True`` (default), each
                non-empty `*_api_key` field is offloaded to the
                detected keystore + the TOML carries a
                ``keystore://service/key`` URL instead of the
                literal. Idempotent: values that already look
                like URLs pass through unchanged. Pass
                ``False`` for tests / migration scripts that
                want the literal-on-disk shape.

        Returns:
            The resolved path the bytes landed at — useful for
            the caller's success toast / log line.

        Raises:
            OSError: When the parent dir can't be created or the
                file can't be written. CARE bubbles this so the
                caller's toast surfaces the real reason rather
                than a silent no-op.
            KeystoreError: When secure storage or restoration
                fails. Any stable-slot updates completed during
                this save are rolled back before the error is
                surfaced.
        """
        target = path or DEFAULT_CONFIG_PATH
        target.parent.mkdir(parents=True, exist_ok=True)
        payload = self.model_dump(mode="json")
        backend_name = ""
        stored_slots = 0
        secret_transaction = (
            _ApiKeySecretStoreTransaction()
            if store_secrets
            else None
        )
        try:
            if secret_transaction is not None:
                backend_name, stored_slots = _store_api_key_secrets(
                    payload,
                    transaction=secret_transaction,
                )
            text = _format_toml(payload)
            _write_toml_atomically(target, text)
        except Exception:
            if secret_transaction is not None:
                secret_transaction.rollback()
            raise
        except BaseException:
            if secret_transaction is not None:
                try:
                    secret_transaction.rollback()
                except BaseException as rollback_exc:  # noqa: BLE001
                    logging.getLogger("care.config").error(
                        "keystore rollback after interruption failed (%s)",
                        type(rollback_exc).__name__,
                        exc_info=False,
                    )
            raise
        else:
            if secret_transaction is not None:
                secret_transaction.commit()
        return ConfigSaveReport(
            path=target,
            keystore_backend=backend_name,
            stored_slots=stored_slots,
        )


def _read_toml(path: Path) -> dict[str, Any]:
    """Parse a TOML file or return ``{}`` if missing.

    Permission / parse errors propagate — CARE prefers a loud failure
    at startup over silently running with defaults the user didn't
    intend. Missing files are normal (first-run, ephemeral CI).
    """
    if not path.exists():
        return {}
    with path.open("rb") as fp:
        return tomllib.load(fp)


def _write_toml_atomically(target: Path, text: str) -> None:
    """Replace ``target`` from a unique same-directory staging file."""
    tmp_path: Path | None = None
    tmp_fd: int | None = None
    try:
        tmp_fd, tmp_name = tempfile.mkstemp(
            prefix=f".{target.name}.",
            suffix=".tmp",
            dir=target.parent,
        )
        tmp_path = Path(tmp_name)
        with os.fdopen(tmp_fd, "w", encoding="utf-8") as fp:
            tmp_fd = None
            fp.write(text)
        os.replace(tmp_path, target)
    finally:
        if tmp_fd is not None:
            try:
                os.close(tmp_fd)
            except OSError:
                pass
        if tmp_path is not None:
            try:
                tmp_path.unlink()
            except OSError:
                pass


def _format_toml(data: dict[str, Any]) -> str:
    """Hand-rolled TOML serialiser for :class:`CareConfig`.

    Avoids a runtime dependency on ``tomli_w`` for the narrow
    write path the SettingsScreen needs. Handles the shapes the
    config actually carries: top-level scalars (none today, but
    safe), nested dict sections (``[memory]`` / ``[platform]`` /
    ``[mage]`` / ``[sandbox]`` / ``[tools]`` / ``[telemetry]`` /
    ``[defaults]``), and scalar values (str / bool / int / float /
    None — None becomes a section-level comment-out, not a key).

    Two-pass: emit top-level scalars first (none today, so the
    pass is a no-op but kept defensive), then one ``[section]``
    block per nested dict. Sections render in
    ``CareConfig.model_dump()`` insertion order — Pydantic
    guarantees field-declaration order, which matches what the
    user reads in ``care.config``.
    """
    lines: list[str] = []
    scalars: list[tuple[str, Any]] = []
    sections: list[tuple[str, dict[str, Any]]] = []
    for key, value in data.items():
        if isinstance(value, dict):
            sections.append((key, value))
        else:
            scalars.append((key, value))
    for key, value in scalars:
        rendered = _toml_value(value)
        if rendered is None:
            continue
        lines.append(f"{key} = {rendered}")
    for name, section in sections:
        if lines:
            lines.append("")
        lines.append(f"[{name}]")
        for key, value in section.items():
            rendered = _toml_value(value)
            if rendered is None:
                # `None` → write as commented-out key so the user
                # can see what's available without polluting the
                # parsed config.
                lines.append(f"# {key} =")
                continue
            lines.append(f"{key} = {rendered}")
    return "\n".join(lines) + "\n"


def _toml_value(value: Any) -> str | None:
    """Render a single TOML value or return ``None`` to skip."""
    if value is None:
        return None
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, str):
        # Escape backslashes + quotes; TOML basic strings cover
        # the shapes CARE uses (URLs, identifiers, paths). Newlines
        # are unlikely but encoded explicitly for safety.
        escaped = (
            value.replace("\\", "\\\\")
            .replace('"', '\\"')
            .replace("\n", "\\n")
        )
        return f'"{escaped}"'
    if isinstance(value, list):
        rendered_items: list[str] = []
        for item in value:
            rendered = _toml_value(item)
            if rendered is not None:
                rendered_items.append(rendered)
        return "[" + ", ".join(rendered_items) + "]"
    if isinstance(value, dict):
        # Inline table — covers the only dict field today
        # (``tools.search_provider_keys``). An empty dict renders as
        # ``{}`` (valid TOML that round-trips back to ``{}``); without
        # this branch it fell through to ``str({})`` and was written as
        # the literal string ``"{}"``, which then failed to re-load.
        pairs: list[str] = []
        for sub_key, sub_value in value.items():
            rendered = _toml_value(sub_value)
            if rendered is None:
                continue
            key = str(sub_key)
            if not key or not all(c.isalnum() or c in "-_" for c in key):
                key = '"' + key.replace("\\", "\\\\").replace('"', '\\"') + '"'
            pairs.append(f"{key} = {rendered}")
        return "{" + ", ".join(pairs) + "}"
    # Fall through: stringify whatever it is (paths / enum values).
    escaped = str(value).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


_RESERVED_ENV_TAILS: frozenset[str] = frozenset({
    # Owned by `care.logging_setup` — drives the Python-side
    # debug log channel, not a config section. Excluded here so
    # `CareConfig`'s `extra="forbid"` doesn't reject them.
    "log_file",
    "log_level",
    # §8 P3 — owned by `care.runtime.library_view`'s
    # `resolve_default_view_state_path` test-isolation knob.
    # Excluded here so the autouse pytest fixture that
    # redirects view-state writes doesn't trip CareConfig's
    # extra-forbid validation.
    "view_state_path",
})


def _parse_env(env: dict[str, str]) -> dict[str, Any]:
    """Translate ``CARE_*`` env vars into a nested dict.

    ``CARE_MAGE__MODE=fast`` becomes ``{"mage": {"mode": "fast"}}``.
    Booleans and numbers stay as strings — Pydantic's validators coerce
    them on the way in. Reserved tails (see ``_RESERVED_ENV_TAILS``)
    are skipped — those env vars belong to runtime subsystems that
    aren't part of the user-facing config surface.
    """
    out: dict[str, Any] = {}
    for raw_key, value in env.items():
        if not raw_key.startswith(ENV_PREFIX):
            continue
        tail = raw_key[len(ENV_PREFIX):].lower()
        if tail in _RESERVED_ENV_TAILS:
            continue
        path_parts = tail.split("__")
        cursor = out
        for part in path_parts[:-1]:
            cursor = cursor.setdefault(part, {})
            if not isinstance(cursor, dict):
                # Conflicting env keys (e.g. CARE_FOO and CARE_FOO__BAR)
                # — drop the leaf instead of crashing.
                cursor = {}
        cursor[path_parts[-1]] = value
    return out


def _deep_merge(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    """Merge ``overlay`` into ``base`` recursively. Overlay wins."""
    result = dict(base)
    for key, value in overlay.items():
        if (
            key in result
            and isinstance(result[key], dict)
            and isinstance(value, dict)
        ):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


# Canonical chat modes + the friendly aliases we silently normalize.
# Mirrors `care.screens.chat.normalise_mode` (canonical ids are
# `interactive` / `production`; the legacy `ad_hoc` spellings map to
# `interactive`) so the TOML file and the `CARE_CHAT__DEFAULT_MODE` env var
# accept the same spellings the runtime resolver does.
_CHAT_MODE_ALIASES: dict[str, str] = {
    "interactive": "interactive",
    "ad_hoc": "interactive",
    "ad-hoc": "interactive",
    "adhoc": "interactive",
    "production": "production",
    "prod": "production",
}


def _sanitize_chat_default_mode(merged: dict[str, Any]) -> None:
    """Normalize or drop ``chat.default_mode`` in place before validation.

    A malformed value (legacy spelling like ``interactive``, a typo, a
    non-string) must not crash the whole config load — the chat surface
    has a sane default. We normalize known aliases to their canonical id
    and drop anything unrecognized so the Pydantic field default applies,
    logging a warning either way. Mutates ``merged`` directly.
    """
    chat = merged.get("chat")
    if not isinstance(chat, dict) or "default_mode" not in chat:
        return
    raw = chat["default_mode"]
    canonical = (
        _CHAT_MODE_ALIASES.get(raw.strip().lower())
        if isinstance(raw, str)
        else None
    )
    if canonical is None:
        logging.getLogger("care.config").warning(
            "chat.default_mode=%r is not a valid mode (expected one of "
            "%s) — falling back to the default.",
            raw,
            sorted(set(_CHAT_MODE_ALIASES.values())),
        )
        del chat["default_mode"]
    else:
        chat["default_mode"] = canonical


# Path segments whose value is a credential — diffs mask these so a
# settings-change trace never leaks an API key into the chat log.
_SECRET_HINTS: tuple[str, ...] = ("key", "secret", "token", "password")


def _is_secret_segment(segment: str) -> bool:
    return any(hint in segment.lower() for hint in _SECRET_HINTS)


def _fmt_value(value: Any) -> str:
    if value is None or value == "":
        return "∅"
    text = str(value)
    return text if len(text) <= 60 else text[:57] + "…"


def _describe_change(path: tuple[str, ...], old: Any, new: Any) -> str:
    label = ".".join(path)
    if _is_secret_segment(path[-1]):
        # Never surface the actual credential — just signal the edit.
        if old in (None, ""):
            return f"{label}: set"
        if new in (None, ""):
            return f"{label}: cleared"
        return f"{label}: updated"
    if old in (None, ""):
        return f"{label}: set to {_fmt_value(new)}"
    if new in (None, ""):
        return f"{label}: cleared (was {_fmt_value(old)})"
    return f"{label}: {_fmt_value(old)} → {_fmt_value(new)}"


def _walk_config_diff(
    old: Any, new: Any, path: tuple[str, ...], out: list[str],
) -> None:
    if isinstance(old, dict) and isinstance(new, dict):
        for key in dict.fromkeys((*old.keys(), *new.keys())):
            _walk_config_diff(old.get(key), new.get(key), (*path, key), out)
        return
    if old != new and path:
        out.append(_describe_change(path, old, new))


def summarize_config_changes(
    old: "CareConfig", new: "CareConfig",
) -> list[str]:
    """Human-readable list of what changed between two configs.

    Each row is a dotted field path with old → new values (e.g.
    ``mage.model: gpt-4 → claude-3.5``). Credential fields are masked
    to ``set`` / ``updated`` / ``cleared`` so a settings-change trace
    posted into the chat never leaks an API key. Returns ``[]`` when
    the two configs are equivalent.
    """
    out: list[str] = []
    _walk_config_diff(
        old.model_dump(mode="json"), new.model_dump(mode="json"), (), out,
    )
    return out


# Pairs of (parent-attr, field-name) describing every
# `*_api_key` slot in the validated `CareConfig`. The keystore
# resolver walks these so a future field rename can't silently
# drop the dereferencing — adding a new api_key field is the
# only place a contributor needs to extend the list.
_API_KEY_SLOTS: tuple[tuple[str, str], ...] = (
    ("mage", "api_key"),
    ("mage", "web_search_api_key"),
    ("memory", "api_key"),
    ("platform", "api_key"),
    ("platform", "validation_api_key"),
    ("platform", "mutation_api_key"),
    ("tools", "web_search_api_key"),
    ("upload", "api_key"),
)


def _resolve_api_key_secrets(config: "CareConfig") -> None:
    """Dereference every `keystore://service/key` URL stored
    in `CareConfig`'s `*_api_key` fields (§1 P0 wiring).

    Mutates ``config`` in place. Literals + `None` pass
    through unchanged. Failures (malformed URL, missing
    keystore entry, keystore-backend exception) leave the
    field as `None` + log a WARNING so the user sees an
    "X facade not configured" toast downstream rather than
    a load-time crash.

    Resolution is deliberately lazy: the keystore is only
    instantiated when at least one field is a URL. Common
    case (every field is a literal or None) costs one
    `is_keystore_url` check per slot.
    """
    # Lazy import — `care.runtime.keystore` pulls `stat` /
    # `subprocess`; keep the config-load path cheap when no
    # keystore URLs are present.
    from care.runtime.keystore import (
        KeystoreInteractionRequiredError,
        is_keystore_url,
        resolve_secret,
    )

    keystore = None
    interaction_blocked = False
    for parent_attr, field_name in _API_KEY_SLOTS:
        parent = getattr(config, parent_attr, None)
        if parent is None:
            continue
        raw = getattr(parent, field_name, None)
        if not is_keystore_url(raw):
            continue
        if interaction_blocked:
            # One denied/cancelled authorization is enough evidence that the
            # remaining Keychain references cannot be resolved in this load.
            # Do not turn one launch into a run of visually identical dialogs.
            setattr(parent, field_name, None)
            continue
        # First URL we hit triggers the lazy detect.
        if keystore is None:
            try:
                from care.runtime.keystore import detect_keystore

                keystore = detect_keystore()
            except Exception:  # noqa: BLE001
                _log = logging.getLogger("care.config")
                _log.warning(
                    "keystore detection failed; %s.%s left empty",
                    parent_attr, field_name,
                    exc_info=False,
                )
                setattr(parent, field_name, None)
                continue
        try:
            resolved = resolve_secret(raw, keystore=keystore)
        except KeystoreInteractionRequiredError as exc:
            logging.getLogger("care.config").warning(
                "keystore authorization unavailable while resolving %s.%s "
                "(%s); remaining referenced credentials left empty",
                parent_attr,
                field_name,
                type(exc).__name__,
            )
            setattr(parent, field_name, None)
            interaction_blocked = True
            continue
        except Exception as exc:  # noqa: BLE001
            logging.getLogger("care.config").warning(
                "failed to resolve %s.%s from keystore (%s); "
                "field left empty",
                parent_attr, field_name, type(exc).__name__,
            )
            setattr(parent, field_name, None)
            continue
        # `resolved` may be `None` when the entry was deleted
        # from the keystore — surface as `None` so downstream
        # presence-checks behave like an unconfigured slot.
        setattr(parent, field_name, resolved)


@dataclass(frozen=True)
class ConfigSaveReport:
    """Outcome of :meth:`CareConfig.save_to_disk_with_report`
    (§1 P2).

    ``path`` carries the on-disk destination — equivalent to
    the legacy `save_to_disk` return value. The new fields
    name the active keystore backend (`"macos-keychain"` /
    `"linux-secret-tool"` / `"file"` / `"memory"`) and how
    many `*_api_key` slots were actually offloaded so the
    SettingsScreen save toast can read like
    `Saved (secrets stored in Keychain — 2 slot(s))`.

    ``keystore_backend`` is the empty string when no slots
    were offloaded (`store_secrets=False`, or every slot
    was already a URL / empty).
    """

    path: Path
    keystore_backend: str = ""
    stored_slots: int = 0

    @property
    def display_backend(self) -> str:
        """Human-friendly backend name. Maps the internal
        identifiers to their canonical product names so the
        toast reads naturally."""
        mapping = {
            "macos-keychain": "macOS Keychain",
            "linux-secret-tool": "secret-tool",
            "file": "file (~/.config/care/secrets.json)",
            "memory": "in-memory",
        }
        return mapping.get(
            self.keystore_backend, self.keystore_backend,
        )


@dataclass(frozen=True)
class SecretMigrationReport:
    """Outcome of :func:`migrate_literal_secrets`.

    Frozen so the CLI / SettingsScreen can print a summary
    without worrying about mutation. Each ``migrated`` entry
    carries the ``(parent_attr, field_name, url)`` tuple
    naming the slot, the field, and the keystore URL the
    literal was replaced with. ``skipped`` carries
    ``(slot, reason)`` for slots that didn't migrate
    (already a URL, empty, keystore failure, etc.).
    """

    migrated: tuple[tuple[str, str, str], ...] = ()
    skipped: tuple[tuple[str, str], ...] = ()

    @property
    def did_migrate(self) -> bool:
        return bool(self.migrated)

    def format_text(self) -> str:
        """Human-readable summary for the CLI handler."""
        lines: list[str] = []
        if self.migrated:
            lines.append(
                f"migrated {len(self.migrated)} secret(s):"
            )
            for parent, field, url in self.migrated:
                lines.append(f"  {parent}.{field} → {url}")
        else:
            lines.append("no literal secrets to migrate.")
        if self.skipped:
            lines.append(
                f"skipped {len(self.skipped)} slot(s):"
            )
            for slot, reason in self.skipped:
                lines.append(f"  {slot} — {reason}")
        return "\n".join(lines)


def migrate_literal_secrets(
    config: "CareConfig",
    *,
    path: Path | None = None,
    keystore: Any = None,
    persist: bool = True,
) -> SecretMigrationReport:
    """Walk ``config`` for literal `*_api_key` values, offload
    each to the keystore, and rewrite ``path`` (default
    :data:`DEFAULT_CONFIG_PATH`) so the literals become
    ``keystore://service/key`` URLs (§1 P1 migration).

    Idempotent: slots that are already URLs / empty / None
    are skipped without touching the keystore.

    The keystore updates, TOML write, and in-memory mutation form
    one transaction: the original stable-slot values are restored
    if any later slot or the TOML write fails, and ``config`` is
    mutated only after the write succeeds. A no-op call on a
    fully-URL'd config doesn't change the file's mtime.

    Ordinary failures land in ``skipped`` without exposing backend
    exception text. Process-control exceptions derived from
    :class:`BaseException` are re-raised after best-effort rollback.
    ``persist=False`` runs the same keystore staging (normally against
    a :class:`MemoryKeystore`) but does not touch a TOML file.
    """
    from care.runtime.keystore import is_keystore_url

    log = logging.getLogger("care.config")
    target_path = path or DEFAULT_CONFIG_PATH

    candidates: list[tuple[str, str, str, str]] = []
    skipped: list[tuple[str, str]] = []
    for parent_attr, field_name in _API_KEY_SLOTS:
        slot = f"{parent_attr}.{field_name}"
        parent = getattr(config, parent_attr, None)
        if parent is None:
            skipped.append((slot, "parent missing"))
            continue
        raw = getattr(parent, field_name, None)
        if not raw:
            skipped.append((slot, "empty"))
            continue
        if not isinstance(raw, str):
            skipped.append((slot, f"unexpected type {type(raw).__name__}"))
            continue
        if is_keystore_url(raw):
            skipped.append((slot, "already a keystore URL"))
            continue
        candidates.append((parent_attr, field_name, slot, raw))

    if not candidates:
        return SecretMigrationReport(skipped=tuple(skipped))

    transaction = _ApiKeySecretStoreTransaction(keystore=keystore)
    staged_config = config.model_copy(deep=True)
    migrated: list[tuple[str, str, str]] = []
    phase = "keystore"
    current_slot = candidates[0][2]
    try:
        for parent_attr, field_name, slot, raw in candidates:
            current_slot = slot
            url = transaction.store(slot=slot, value=raw)
            setattr(getattr(staged_config, parent_attr), field_name, url)
            migrated.append((parent_attr, field_name, url))
        if persist:
            phase = "disk"
            # Values on the staged config are already keystore URLs.
            staged_config.save_to_disk(
                path=target_path,
                store_secrets=False,
            )
    except Exception as exc:  # noqa: BLE001
        rollback_failed = False
        try:
            transaction.rollback()
        except Exception as rollback_exc:  # noqa: BLE001
            rollback_failed = True
            log.error(
                "migrate_literal_secrets: rollback failed (%s)",
                type(rollback_exc).__name__,
                exc_info=False,
            )
        error_type = type(exc).__name__
        log.warning(
            "migrate_literal_secrets: %s phase failed (%s); "
            "no secrets migrated",
            phase,
            error_type,
            exc_info=False,
        )
        if phase == "disk":
            skipped.append(("__disk__", f"write failed ({error_type})"))
        elif transaction.keystore is None:
            skipped.append(
                (current_slot, f"keystore detect failed ({error_type})")
            )
        else:
            skipped.append(
                (current_slot, f"secure storage failed ({error_type})")
            )
        if rollback_failed:
            skipped.append(
                ("__rollback__", "secure storage rollback incomplete")
            )
        return SecretMigrationReport(skipped=tuple(skipped))
    except BaseException:
        try:
            transaction.rollback()
        except BaseException as rollback_exc:  # noqa: BLE001
            log.error(
                "migrate_literal_secrets: rollback after interruption "
                "failed (%s)",
                type(rollback_exc).__name__,
                exc_info=False,
            )
        raise
    else:
        transaction.commit()
        for parent_attr, field_name, url in migrated:
            setattr(getattr(config, parent_attr), field_name, url)
    return SecretMigrationReport(
        migrated=tuple(migrated), skipped=tuple(skipped),
    )


class _ApiKeySecretStoreTransaction:
    """Rollback boundary for stable-slot keystore updates.

    The legacy config format uses deterministic keys such as
    ``mage.api_key``. Updating more than one key therefore has to
    snapshot the previous values before the first mutation: if a
    later store or the file write fails, every attempted slot is
    restored (or deleted when it did not exist before).

    Secret values are deliberately absent from exception and log
    messages. Snapshots are released on :meth:`commit` or
    :meth:`rollback`; Python-owned strings cannot be wiped in place.
    """

    def __init__(self, *, keystore: Any = None) -> None:
        self.keystore: Any = keystore
        self.backend_name = (
            getattr(keystore, "name", "") or ""
            if keystore is not None
            else ""
        )
        self.stored_slots = 0
        self._previous: dict[str, str | None] = {}
        self._attempted: list[str] = []
        self._closed = False

    def store(self, *, slot: str, value: str) -> str:
        from care.runtime.keystore import (
            DEFAULT_SERVICE,
            KeystoreError,
            detect_keystore,
            store_secret,
        )

        if self._closed:
            raise KeystoreError("secure credential transaction is closed")
        if self.keystore is None:
            self.keystore = detect_keystore()
            self.backend_name = (
                getattr(self.keystore, "name", "") or ""
            )
        if slot not in self._previous:
            self._previous[slot] = self.keystore.fetch(
                DEFAULT_SERVICE,
                slot,
            )
        # Register before store: a backend may commit and then raise.
        self._attempted.append(slot)
        url = store_secret(
            value,
            key=slot,
            keystore=self.keystore,
        )
        self.stored_slots += 1
        return url

    def commit(self) -> None:
        self._previous.clear()
        self._attempted.clear()
        self._closed = True

    def rollback(self) -> None:
        from care.runtime.keystore import DEFAULT_SERVICE, KeystoreError

        if self._closed:
            return
        failures = 0
        restored: set[str] = set()
        for slot in reversed(self._attempted):
            if slot in restored:
                continue
            restored.add(slot)
            previous = self._previous.get(slot)
            try:
                if previous is None:
                    self.keystore.delete(DEFAULT_SERVICE, slot)
                else:
                    self.keystore.store(
                        DEFAULT_SERVICE,
                        slot,
                        previous,
                    )
            except Exception as exc:  # noqa: BLE001
                failures += 1
                logging.getLogger("care.config").error(
                    "keystore rollback failed for %s (%s)",
                    slot,
                    type(exc).__name__,
                    exc_info=False,
                )
        self._previous.clear()
        self._attempted.clear()
        self._closed = True
        if failures:
            raise KeystoreError(
                "secure credential rollback was incomplete; "
                "configuration was not written"
            ) from None


def _store_api_key_secrets(
    payload: dict[str, Any],
    *,
    transaction: _ApiKeySecretStoreTransaction | None = None,
) -> tuple[str, int]:
    """Symmetric write-side of :func:`_resolve_api_key_secrets`
    (§1 P1).

    Walks the same ``_API_KEY_SLOTS`` list against the dict
    projection produced by :meth:`CareConfig.model_dump`. For
    each slot whose value is a non-empty literal (i.e. not
    already a ``keystore://service/key`` URL), the helper
    calls :func:`store_secret` to offload the literal to the
    detected backend and rewrites the dict entry to the
    returned URL. The keystore is instantiated lazily on
    first non-URL literal — configs that already use URLs
    everywhere cost one ``is_keystore_url`` check per slot.

    Mutates ``payload`` in place. Secure-storage failures are
    fail-closed: :class:`~care.runtime.keystore.KeystoreError`
    is raised before the caller serialises or writes ``payload``.
    In particular, a literal is never retained as a fallback in
    the TOML merely because Keychain/libsecret rejected a write.
    When ``transaction`` is supplied, the caller commits it only
    after its TOML write succeeds; otherwise this helper commits
    after all slots are stored for backwards compatibility.
    """
    from care.runtime.keystore import (
        KeystoreError,
        is_keystore_url,
    )

    owns_transaction = transaction is None
    secret_transaction = (
        transaction or _ApiKeySecretStoreTransaction()
    )
    log = logging.getLogger("care.config")
    for parent_attr, field_name in _API_KEY_SLOTS:
        parent = payload.get(parent_attr)
        if not isinstance(parent, dict):
            continue
        raw = parent.get(field_name)
        if not raw or not isinstance(raw, str):
            continue
        if is_keystore_url(raw):
            continue
        try:
            url = secret_transaction.store(
                slot=f"{parent_attr}.{field_name}",
                value=raw,
            )
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "keystore transaction failed for %s.%s (%s); "
                "config write aborted",
                parent_attr, field_name, type(exc).__name__,
                exc_info=False,
            )
            try:
                secret_transaction.rollback()
            except KeystoreError:
                raise
            raise KeystoreError(
                "secure credential storage failed; "
                "configuration was not written"
            ) from None
        except BaseException:
            try:
                secret_transaction.rollback()
            except BaseException as rollback_exc:  # noqa: BLE001
                log.error(
                    "keystore rollback after interruption failed for "
                    "%s.%s (%s)",
                    parent_attr,
                    field_name,
                    type(rollback_exc).__name__,
                    exc_info=False,
                )
            raise
        parent[field_name] = url
    if owns_transaction:
        secret_transaction.commit()
    stored_slots = secret_transaction.stored_slots
    return (
        secret_transaction.backend_name if stored_slots else "",
        stored_slots,
    )


def resolve_platform_api_base_url(platform: PlatformConfig) -> str:
    """Return the Platform master-api base URL CARE should call.

    GigaEvo splits control plane (master-api, port 8000) from
    execution (runner-api, port 8001). Dataset upload and experiment
    creation exist on master only; master also proxies start/stop/status
    to runners. Legacy configs often pointed ``base_url`` at runner-api
    — detect port 8001 and redirect to :attr:`PlatformConfig.master_base_url`.
    """
    base = platform.base_url.rstrip("/")
    master = platform.master_base_url.rstrip("/")
    if base.endswith(":8001"):
        return master
    return base or master


__all__ = [
    "CareConfig",
    "ConfigSaveReport",
    "DefaultsConfig",
    "MageConfig",
    "MemoryConfig",
    "PlatformConfig",
    "SandboxConfig",
    "SecretMigrationReport",
    "TelemetryConfig",
    "ToolsConfig",
    "DEFAULT_CONFIG_PATH",
    "ENV_PREFIX",
    "PROJECT_CONFIG_FILENAME",
    "migrate_literal_secrets",
    "resolve_platform_api_base_url",
    "summarize_config_changes",
]
