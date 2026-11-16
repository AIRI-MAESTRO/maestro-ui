(() => {
  "use strict";

  const API_ROOT = "/api";
  const THEME_STORAGE_KEY = "maestro.theme";
  const MAX_STREAM_CHARS = 7000;
  const MAX_PREVIEW_CHARS = 5000;
  const MAX_COMMAND_OUTPUT_CHARS = 128 * 1024;
  const MAX_PALETTE_RESULTS = 80;
  const MAX_SLASH_RESULTS = 9;
  const MAX_LOCAL_LIST_ROWS = 50;
  const MAX_LOCAL_ARTIFACTS = 24;
  const MAX_LOCAL_BRANCHES = 50;
  const MAX_MEMORY_EDITOR_BYTES = 12_000;
  const LOCAL_BRANCH_NAME = /^[A-Za-z0-9_-]{1,64}$/;
  const TERMINAL_STATUSES = new Set(["completed", "failed", "cancelled"]);
  const ACTIVE_STATUSES = new Set([
    "created",
    "pending",
    "queued",
    "running",
    "generating",
    "executing",
    "waiting_for_input",
    "cancelling",
  ]);
  const MAESTRIC_FALLBACK_ASSET = "mascot/mascot.webp";
  const MAESTRIC_MOODS = Object.freeze({
    idle: { mood: "idle", asset: "mascot/evoc-coffee.webp" },
    loading: { mood: "idle", asset: "mascot/evoc-coffee.webp" },
    created: { mood: "generation", asset: "mascot/evoc-generate.webp" },
    pending: { mood: "generation", asset: "mascot/evoc-generate.webp" },
    queued: { mood: "generation", asset: "mascot/evoc-generate.webp" },
    running: { mood: "generation", asset: "mascot/evoc-generate.webp" },
    generating: { mood: "thinking", asset: "mascot/evoc-think.webp" },
    executing: { mood: "work", asset: "mascot/evoc-work.webp" },
    waiting_for_input: { mood: "waiting", asset: "mascot/evoc-surprised.webp" },
    cancelling: { mood: "cancelled", asset: "mascot/evoc-coffee.webp" },
    completed: { mood: "success", asset: "mascot/evoc-cheers.webp" },
    failed: { mood: "failed", asset: "mascot/evoc-surprised.webp" },
    cancelled: { mood: "cancelled", asset: "mascot/evoc-coffee.webp" },
    canceled: { mood: "cancelled", asset: "mascot/evoc-coffee.webp" },
  });
  const LOCAL_COMMANDS = Object.freeze([
    { invocation: "/artifacts", description: "Inspect artifacts attached to the current desktop run (adapted, process-local).", source: "desktop", risk: "local" },
    { invocation: "/blocks", description: "List, copy, or save fenced blocks from the visible run inside this workspace.", source: "desktop", risk: "local" },
    { invocation: "/branch", description: "Save, list, switch, or delete process-local run checkpoints.", source: "desktop", risk: "local" },
    { invocation: "/new", description: "Archive this conversation and start a persistent session.", source: "desktop", risk: "local" },
    { invocation: "/clear", description: "Clear the current persistent session transcript.", source: "desktop", risk: "local" },
    { invocation: "/edit", description: "List or prefill prompts from process-local desktop runs.", source: "desktop", risk: "local" },
    { invocation: "/theme", description: "Set Auto, Warm, or Crystal appearance.", source: "desktop", risk: "local" },
    { invocation: "/settings", description: "Configure providers, models, secure API keys, services, and defaults.", source: "desktop", risk: "local" },
    { invocation: "/mode", description: "Switch the CARE session between Chat (interactive) and Agent (production).", source: "desktop", risk: "local" },
    { invocation: "/multi", description: "Open the multi-line task composer.", source: "desktop", risk: "local" },
    { invocation: "/history", description: "Open recent native runs; optionally limit the list with /history N.", source: "desktop", risk: "local" },
    { invocation: "/runs", description: "Alias for /history, including the optional limit.", source: "desktop", risk: "local" },
    { invocation: "/resume", description: "Resume a persistent session by latest or exact session ID.", source: "desktop", risk: "local" },
    { invocation: "/sessions", description: "List persistent workspace sessions.", source: "desktop", risk: "local" },
    { invocation: "/subagents", description: "Inspect captured step and tool events for the current run.", source: "desktop", risk: "local" },
    { invocation: "/tour", description: "Open the five-step native desktop walkthrough.", source: "desktop", risk: "local" },
    { invocation: "/help", description: "Show local and CLI command help.", source: "desktop", risk: "local" },
    { invocation: "/quit", description: "Close the native Maestro window.", source: "desktop", risk: "local" },
    { invocation: "/exit", description: "Alias for /quit.", source: "desktop", risk: "local" },
  ]);

  const SSE_EVENT_TYPES = [
    "run.created",
    "run.started",
    "run.completed",
    "run.failed",
    "run.cancelled",
    "generation.stage.started",
    "generation.stage.completed",
    "generation.stage.error",
    "generation.chunk",
    "generation.progress",
    "generation.stage.progress",
    "generation.retry",
    "generation.stage.retry",
    "generation.cost",
    "generation.completed",
    "generation.event",
    "production.stage",
    "production.completed",
    "execution.started",
    "execution.step.started",
    "execution.step.completed",
    "execution.progress",
    "execution.chunk",
    "execution.step.event",
    "execution.chain.completed",
    "execution.event",
    "execution.routing",
    "execution.warning",
    "execution.tool.status",
    "execution.tool.reused",
    "execution.tool.created",
    "execution.tool.failed",
    "human_input.requested",
    "human_input.resolved",
    "human_input.expired",
  ];

  const state = {
    runs: [],
    session: null,
    sessions: [],
    activeRun: null,
    chain: null,
    source: null,
    sourceRunId: null,
    pollTimer: null,
    seenEventKeys: new Set(),
    cards: new Map(),
    currentExecutionStep: null,
    submitting: false,
    cancelling: false,
    loadToken: 0,
    transportNoticeShown: false,
    commandEntries: [...LOCAL_COMMANDS],
    commandManifestLoaded: false,
    commandManifestError: "",
    commandSubmitting: false,
    activeCommandJob: null,
    settingsSubmitting: false,
    settingsLoaded: false,
    settingsHydration: null,
    pendingConfirmation: null,
    slashMatches: [],
    slashIndex: -1,
    multiLineMode: false,
    branches: new Map(),
    nextBranchNumber: 1,
    historyDisplayLimit: null,
    ephemeralSecretClear: null,
    memoryEditor: null,
    productionPointer: null,
  };

  const dom = {};

  class ApiError extends Error {
    constructor(message, status = 0, payload = null) {
      super(message);
      this.name = "ApiError";
      this.status = status;
      this.payload = payload;
    }
  }

  function init() {
    cacheDom();
    initMaestric();
    initTheme();
    bindEvents();
    resizePrompt();
    syncComposerState();
    renderCommandPalette();
    state.settingsHydration = hydrateNativeSettings();
    void loadCommandManifest();
    void initializeSession();
    void loadRuns({ showLoading: true });
  }

  function cacheDom() {
    const ids = [
      "app-shell",
      "sidebar",
      "sidebar-scrim",
      "sidebar-open",
      "sidebar-close",
      "new-run-button",
      "refresh-history",
      "history-region",
      "history-state",
      "history-list",
      "service-state",
      "service-state-label",
      "run-title",
      "run-status",
      "run-status-label",
      "settings-button",
      "command-palette-button",
      "maestric-companion",
      "maestric-avatar",
      "maestric-status-label",
      "chain-button",
      "cancel-button",
      "conversation",
      "conversation-inner",
      "empty-state",
      "run-view",
      "session-transcript",
      "current-user-message",
      "user-prompt",
      "event-timeline",
      "composer-alert",
      "composer-alert-text",
      "composer-alert-close",
      "slash-menu",
      "slash-command-list",
      "composer-form",
      "prompt-input",
      "execute-input",
      "send-button",
      "keyboard-hint",
      "composer-footnote",
      "chain-dialog",
      "chain-dialog-close",
      "chain-dialog-body",
      "command-dialog",
      "command-dialog-close",
      "command-search",
      "command-manifest-state",
      "command-list",
      "settings-dialog",
      "settings-form",
      "settings-dialog-close",
      "settings-cancel",
      "settings-save",
      "settings-status",
      "settings-fields",
      "settings-provider",
      "settings-generation-mode",
      "settings-base-url",
      "settings-model",
      "settings-api-key",
      "settings-api-key-state",
      "settings-api-key-clear",
      "settings-default-mode",
      "settings-interactive-run",
      "settings-memory-research",
      "settings-web-research",
      "settings-reduced-motion",
      "settings-ui-language",
      "settings-memory-url",
      "settings-memory-key",
      "settings-memory-key-state",
      "settings-memory-key-clear",
      "settings-platform-url",
      "settings-platform-key",
      "settings-platform-key-state",
      "settings-platform-key-clear",
      "settings-mutation-url",
      "settings-mutation-model",
      "settings-mutation-key",
      "settings-mutation-key-state",
      "settings-mutation-key-clear",
      "settings-validation-url",
      "settings-validation-model",
      "settings-validation-key",
      "settings-validation-key-state",
      "settings-validation-key-clear",
      "settings-search-provider",
      "settings-search-key",
      "settings-search-key-state",
      "settings-search-key-clear",
      "settings-upload-key",
      "settings-upload-key-state",
      "settings-upload-key-clear",
      "memory-editor-dialog",
      "memory-editor-form",
      "memory-editor-close",
      "memory-editor-cancel",
      "memory-editor-save",
      "memory-editor-content",
      "memory-editor-meta",
      "memory-editor-status",
      "memory-editor-warning",
      "command-confirmation-dialog",
      "command-confirmation-title",
      "command-confirmation-risk",
      "command-confirmation-reason",
      "command-confirmation-command",
      "command-confirmation-effects",
      "command-confirmation-effects-list",
      "command-confirmation-cancel",
      "command-confirmation-confirm",
      "announcer",
    ];

    for (const id of ids) {
      const node = document.getElementById(id);
      if (!node) {
        throw new Error(`Missing required UI element: ${id}`);
      }
      dom[toCamel(id)] = node;
    }
  }

  function bindEvents() {
    dom.composerForm.addEventListener("submit", startRun);
    dom.promptInput.addEventListener("input", onPromptInput);
    dom.promptInput.addEventListener("keydown", onPromptKeyDown);
    dom.executeInput.addEventListener("change", updateComposerFootnote);
    for (const radio of document.querySelectorAll('input[name="mode"], input[name="session-mode"]')) {
      radio.addEventListener("change", () => void persistSelectedModes());
    }
    dom.newRunButton.addEventListener("click", newChat);
    dom.refreshHistory.addEventListener("click", () => void loadRuns({ showLoading: true }));
    dom.cancelButton.addEventListener("click", cancelRun);
    dom.chainButton.addEventListener("click", openChainDialog);
    dom.chainDialogClose.addEventListener("click", closeChainDialog);
    dom.chainDialog.addEventListener("click", onDialogBackdropClick);
    dom.composerAlertClose.addEventListener("click", hideComposerAlert);
    dom.sidebarOpen.addEventListener("click", openSidebar);
    dom.sidebarClose.addEventListener("click", closeSidebar);
    dom.sidebarScrim.addEventListener("click", closeSidebar);
    dom.commandPaletteButton.addEventListener("click", openCommandDialog);
    dom.settingsButton.addEventListener("click", () => void openSettingsDialog());
    dom.commandDialogClose.addEventListener("click", closeCommandDialog);
    dom.commandDialog.addEventListener("click", onCommandDialogBackdropClick);
    dom.commandDialog.addEventListener("cancel", (event) => {
      event.preventDefault();
      closeCommandDialog();
    });
    dom.commandSearch.addEventListener("input", renderCommandPalette);
    dom.commandSearch.addEventListener("keydown", onCommandSearchKeyDown);
    dom.commandList.addEventListener("keydown", onCommandListKeyDown);
    dom.settingsForm.addEventListener("submit", onSettingsSubmit);
    dom.settingsDialogClose.addEventListener("click", closeSettingsDialog);
    dom.settingsCancel.addEventListener("click", closeSettingsDialog);
    dom.settingsDialog.addEventListener("cancel", (event) => {
      event.preventDefault();
      closeSettingsDialog();
    });
    dom.settingsDialog.addEventListener("click", (event) => {
      if (event.target === dom.settingsDialog) {
        closeSettingsDialog();
      }
    });
    dom.settingsProvider.addEventListener("change", applyProviderPreset);
    dom.memoryEditorForm.addEventListener("submit", saveMemoryEditor);
    dom.memoryEditorContent.addEventListener("input", updateMemoryEditorState);
    dom.memoryEditorClose.addEventListener("click", closeMemoryEditor);
    dom.memoryEditorCancel.addEventListener("click", closeMemoryEditor);
    dom.memoryEditorDialog.addEventListener("cancel", (event) => {
      event.preventDefault();
      closeMemoryEditor();
    });
    dom.memoryEditorDialog.addEventListener("click", (event) => {
      if (event.target === dom.memoryEditorDialog) {
        closeMemoryEditor();
      }
    });
    dom.commandConfirmationCancel.addEventListener("click", declineCommandConfirmation);
    dom.commandConfirmationConfirm.addEventListener("click", confirmPendingCommand);
    dom.commandConfirmationDialog.addEventListener("cancel", (event) => {
      event.preventDefault();
      declineCommandConfirmation();
    });

    for (const button of document.querySelectorAll("[data-theme-value]")) {
      button.addEventListener("click", () => setTheme(button.dataset.themeValue));
    }

    for (const button of document.querySelectorAll("[data-suggestion]")) {
      button.addEventListener("click", () => {
        dom.promptInput.value = button.dataset.suggestion || "";
        resizePrompt();
        dom.promptInput.focus();
      });
    }

    document.addEventListener("keydown", (event) => {
      if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "k") {
        event.preventDefault();
        if (!dom.commandConfirmationDialog.open && !dom.memoryEditorDialog.open) {
          if (dom.commandDialog.open) {
            closeCommandDialog();
          } else {
            openCommandDialog();
          }
        }
        return;
      }
      if ((event.metaKey || event.ctrlKey) && event.key === ",") {
        event.preventDefault();
        if (!dom.commandConfirmationDialog.open && !dom.memoryEditorDialog.open) {
          void openSettingsDialog();
        }
        return;
      }
      if (event.key === "Escape" && !dom.slashMenu.hidden) {
        closeSlashMenu();
        return;
      }
      if (event.key === "Escape" && dom.appShell.classList.contains("sidebar-open")) {
        closeSidebar();
      }
    });

    window.addEventListener("beforeunload", closeRunTransport);
    window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => {
      if (document.documentElement.dataset.theme === "system") {
        updateThemeColor();
      }
    });
  }

  function initTheme() {
    let saved = "system";
    try {
      const stored = window.localStorage.getItem(THEME_STORAGE_KEY);
      if (["system", "light", "dark"].includes(stored)) {
        saved = stored;
      }
    } catch (_error) {
      // Storage can be unavailable in hardened/private contexts. System is safe.
    }
    applyTheme(saved, false);
  }

  function initMaestric() {
    dom.maestricAvatar.addEventListener("error", () => {
      if (dom.maestricAvatar.getAttribute("src") !== MAESTRIC_FALLBACK_ASSET) {
        dom.maestricAvatar.setAttribute("src", MAESTRIC_FALLBACK_ASSET);
      }
    });
    setMaestricMood("idle", "Ready");
  }

  function setMaestricMood(status, label) {
    const normalized = normalizeStatus(status);
    const presentation = MAESTRIC_MOODS[normalized] || MAESTRIC_MOODS.idle;
    const statusText = stringValue(label) || statusLabel(normalized);

    dom.maestricCompanion.dataset.mood = presentation.mood;
    dom.maestricStatusLabel.textContent = statusText;
    dom.maestricCompanion.setAttribute("aria-label", `Maestric status: ${statusText}`);
    if (dom.maestricAvatar.getAttribute("src") !== presentation.asset) {
      dom.maestricAvatar.setAttribute("src", presentation.asset);
    }
  }

  async function loadCommandManifest() {
    state.commandManifestError = "";
    renderCommandPalette();
    try {
      const payload = await apiFetch(`${API_ROOT}/commands`);
      state.commandEntries = mergeCommandEntries(payload);
      state.commandManifestLoaded = true;
    } catch (error) {
      state.commandManifestLoaded = false;
      state.commandManifestError = readError(error, "CLI commands are unavailable.");
      state.commandEntries = [...LOCAL_COMMANDS];
    }
    renderCommandPalette();
    updateSlashAutocomplete();
  }

  function mergeCommandEntries(payload) {
    const merged = [...LOCAL_COMMANDS];
    const seen = new Set(merged.map((entry) => entry.invocation.toLowerCase()));
    const roots = [];
    if (isRecord(payload)) {
      roots.push(payload);
      for (const key of ["manifest", "registry", "data"]) {
        if (isRecord(payload[key])) {
          roots.unshift(payload[key]);
        }
      }
      if (isRecord(payload.commands)) {
        roots.unshift(payload.commands);
      }
    }

    const addEntry = (entry, alias = false) => {
      if (!isRecord(entry)) {
        return;
      }
      let invocation = firstText(entry, ["invocation", "display_command", "syntax", "slash", "command", "name"]);
      if (!invocation) {
        return;
      }
      invocation = invocation.trim();
      const prefixed = invocation.startsWith("/") || /^(?:maestro|care)(?:\s|$)/i.test(invocation)
        ? invocation
        : `maestro ${invocation}`;
      const key = prefixed.toLowerCase();
      if (seen.has(key)) {
        return;
      }
      seen.add(key);
      const target = firstText(entry, ["target"]);
      const unavailableReason = firstText(entry, ["unavailable_reason", "reason", "detail"])
        || (entry.available === false ? firstText(entry, ["risk_detail"]) : "");
      const description = firstText(entry, ["description", "help", "summary", "risk_detail"])
        || (alias && target ? `Alias for maestro ${target}.` : "Maestro CLI command.");
      merged.push({
        invocation: prefixed,
        description: unavailableReason
          ? description === unavailableReason
            ? `Unavailable: ${description}`
            : `${description} Unavailable: ${unavailableReason}`
          : description,
        source: firstText(entry, ["source"]) || (alias ? "alias" : "cli"),
        risk: firstText(entry, ["risk"]) || "read_only",
        available: entry.available !== false,
        unavailableReason,
      });
    };

    for (const root of roots) {
      if (Array.isArray(root.leaf_routes)) {
        root.leaf_routes.forEach((entry) => addEntry(entry));
      }
      if (Array.isArray(root.aliases)) {
        root.aliases.forEach((entry) => addEntry(entry, true));
      }
      if (Array.isArray(root.slash_commands)) {
        root.slash_commands.forEach((entry) => addEntry(entry, true));
      }
      if (!Array.isArray(root.commands)) {
        continue;
      }
      for (const entry of root.commands) {
        if (isRecord(entry) && Array.isArray(entry.leaf_routes) && entry.leaf_routes.length) {
          entry.leaf_routes.forEach((leaf) => addEntry(leaf));
        } else {
          addEntry(entry);
        }
      }
    }

    return merged.sort((left, right) => {
      const localDelta = Number(right.source === "desktop") - Number(left.source === "desktop");
      return localDelta || left.invocation.localeCompare(right.invocation);
    });
  }

  function renderCommandPalette() {
    if (!dom.commandList) {
      return;
    }
    const query = dom.commandSearch ? dom.commandSearch.value.trim().toLowerCase() : "";
    const entries = filterCommandEntries(query).slice(0, MAX_PALETTE_RESULTS);
    const fragment = document.createDocumentFragment();

    for (const entry of entries) {
      const button = create("button", {
        className: `command-item${entry.available === false ? " is-unavailable" : ""}`,
        attrs: {
          type: "button",
          role: "listitem",
          disabled: entry.available === false ? "" : undefined,
          "aria-disabled": entry.available === false ? "true" : undefined,
          title: entry.available === false ? (entry.unavailableReason || "Command unavailable") : undefined,
        },
      });
      const heading = create("span", { className: "command-item-heading" });
      heading.append(
        create("code", { text: entry.invocation }),
        create("span", {
          className: `command-source command-source-${commandRiskTone(entry.risk)}`,
          text: entry.source === "desktop" ? "Local" : commandRiskLabel(entry.risk),
        }),
      );
      button.append(
        heading,
        create("span", { className: "command-item-description", text: entry.description }),
      );
      if (entry.available !== false) {
        button.addEventListener("click", () => selectCommandEntry(entry));
      }
      fragment.append(button);
    }

    if (!entries.length) {
      fragment.append(create("p", {
        className: "command-empty-state",
        text: "No matching commands.",
      }));
    }
    dom.commandList.replaceChildren(fragment);

    if (state.commandManifestError) {
      dom.commandManifestState.textContent = "CLI manifest unavailable; local commands remain available.";
      dom.commandManifestState.dataset.tone = "warning";
    } else if (!state.commandManifestLoaded) {
      dom.commandManifestState.textContent = "Loading CLI command manifest…";
      dom.commandManifestState.dataset.tone = "loading";
    } else {
      const backendCount = state.commandEntries.filter((entry) => entry.source !== "desktop").length;
      dom.commandManifestState.textContent = `${backendCount} indexed commands · ${LOCAL_COMMANDS.length} local commands`;
      dom.commandManifestState.dataset.tone = "ready";
    }
  }

  function filterCommandEntries(query) {
    if (!query) {
      return [...state.commandEntries];
    }
    const terms = query.split(/\s+/).filter(Boolean);
    return state.commandEntries
      .filter((entry) => {
        const haystack = `${entry.invocation} ${slashInvocation(entry)} ${entry.description} ${entry.risk}`.toLowerCase();
        return terms.every((term) => haystack.includes(term));
      })
      .sort((left, right) => commandMatchScore(right, query) - commandMatchScore(left, query));
  }

  function commandMatchScore(entry, query) {
    const invocation = entry.invocation.toLowerCase();
    const slash = slashInvocation(entry).toLowerCase();
    if (invocation === query || slash === query) {
      return 5;
    }
    if (invocation.startsWith(query) || slash.startsWith(query)) {
      return 3;
    }
    return invocation.includes(query) || slash.includes(query) ? 1 : 0;
  }

  function commandRiskLabel(risk) {
    const normalized = stringValue(risk).toLowerCase();
    const labels = {
      read_only: "Read only",
      filesystem_write_or_mutation: "Writes files",
      external_side_effect: "External effect",
      provider_or_tool_execution: "Runs provider/tools",
      local: "Local",
    };
    return labels[normalized] || humanize(normalized || "command");
  }

  function commandRiskTone(risk) {
    const normalized = stringValue(risk).toLowerCase();
    if (normalized === "read_only" || normalized === "local") {
      return "safe";
    }
    if (normalized.includes("provider") || normalized.includes("external")) {
      return "danger";
    }
    return "warning";
  }

  function slashInvocation(entry) {
    const invocation = stringValue(entry.invocation).trim();
    if (invocation.startsWith("/")) {
      return invocation;
    }
    return `/${invocation.replace(/^(?:maestro|care)\s+/i, "")}`;
  }

  function selectCommandEntry(entry) {
    closeCommandDialog({ restoreFocus: false });
    if (entry.invocation === "/settings") {
      void openSettingsDialog();
      return;
    }
    const needsArguments = entry.source !== "desktop"
      || ["/theme", "/mode", "/multi", "/help"].includes(entry.invocation);
    dom.promptInput.value = `${entry.invocation}${needsArguments ? " " : ""}`;
    closeSlashMenu();
    resizePrompt();
    dom.promptInput.focus();
    dom.promptInput.setSelectionRange(dom.promptInput.value.length, dom.promptInput.value.length);
  }

  function openCommandDialog() {
    if (dom.commandConfirmationDialog.open || dom.memoryEditorDialog.open) {
      return;
    }
    closeSlashMenu();
    if (dom.chainDialog.open) {
      if (typeof dom.chainDialog.close === "function") {
        dom.chainDialog.close();
      } else {
        dom.chainDialog.removeAttribute("open");
      }
    }
    dom.commandSearch.value = "";
    renderCommandPalette();
    if (typeof dom.commandDialog.showModal === "function") {
      if (!dom.commandDialog.open) {
        dom.commandDialog.showModal();
      }
    } else {
      dom.commandDialog.setAttribute("open", "");
    }
    requestAnimationFrame(() => dom.commandSearch.focus());
  }

  function closeCommandDialog({ restoreFocus = true } = {}) {
    if (typeof dom.commandDialog.close === "function" && dom.commandDialog.open) {
      dom.commandDialog.close();
    } else {
      dom.commandDialog.removeAttribute("open");
    }
    if (restoreFocus) {
      dom.commandPaletteButton.focus();
    }
  }

  function onCommandDialogBackdropClick(event) {
    if (event.target === dom.commandDialog) {
      closeCommandDialog();
    }
  }

  function onCommandSearchKeyDown(event) {
    if (!["ArrowDown", "ArrowUp", "Enter"].includes(event.key)) {
      return;
    }
    const buttons = commandPaletteButtons();
    if (!buttons.length) {
      return;
    }
    event.preventDefault();
    const target = event.key === "ArrowUp" ? buttons[buttons.length - 1] : buttons[0];
    if (event.key === "Enter") {
      target.click();
    } else {
      target.focus();
    }
  }

  function onCommandListKeyDown(event) {
    if (!["ArrowDown", "ArrowUp", "Home", "End", "Escape"].includes(event.key)) {
      return;
    }
    event.preventDefault();
    if (event.key === "Escape") {
      closeCommandDialog();
      return;
    }
    const buttons = commandPaletteButtons();
    if (!buttons.length) {
      return;
    }
    const current = buttons.indexOf(document.activeElement);
    let index = current;
    if (event.key === "Home") {
      index = 0;
    } else if (event.key === "End") {
      index = buttons.length - 1;
    } else if (event.key === "ArrowDown") {
      index = (current + 1 + buttons.length) % buttons.length;
    } else {
      index = (current - 1 + buttons.length) % buttons.length;
    }
    buttons[index].focus();
  }

  function commandPaletteButtons() {
    return Array.from(dom.commandList.querySelectorAll("button.command-item:not(:disabled)"));
  }

  function setTheme(theme) {
    if (!["system", "light", "dark"].includes(theme)) {
      return;
    }
    applyTheme(theme, true);
  }

  function applyTheme(theme, persist) {
    document.documentElement.dataset.theme = theme;
    for (const button of document.querySelectorAll("[data-theme-value]")) {
      button.setAttribute("aria-pressed", String(button.dataset.themeValue === theme));
    }
    if (persist) {
      try {
        window.localStorage.setItem(THEME_STORAGE_KEY, theme);
      } catch (_error) {
        // The visual preference still applies for this page.
      }
    }
    updateThemeColor();
  }

  function updateThemeColor() {
    const selected = document.documentElement.dataset.theme;
    const isDark = selected === "dark" || (
      selected === "system" && window.matchMedia("(prefers-color-scheme: dark)").matches
    );
    const meta = document.querySelector('meta[name="theme-color"]');
    if (meta) {
      meta.setAttribute("content", isDark ? "#0f1115" : "#faf6ef");
    }
  }

  async function openSettingsDialog() {
    if (dom.commandConfirmationDialog.open || dom.memoryEditorDialog.open || state.settingsSubmitting) {
      return;
    }
    closeSlashMenu();
    if (dom.commandDialog.open) {
      closeCommandDialog({ restoreFocus: false });
    }
    if (dom.chainDialog.open) {
      closeChainDialog();
    }
    if (typeof dom.settingsDialog.showModal === "function") {
      if (!dom.settingsDialog.open) {
        dom.settingsDialog.showModal();
      }
    } else {
      dom.settingsDialog.setAttribute("open", "");
    }
    setSettingsBusy(true, "Loading settings…");
    resetSettingsSecrets();
    try {
      const payload = await apiFetch(`${API_ROOT}/settings`);
      const settings = isRecord(payload) && isRecord(payload.settings)
        ? payload.settings
        : null;
      if (!settings) {
        throw new ApiError("The settings service returned an invalid response.");
      }
      populateSettingsForm(settings);
      applyNativeSettings(settings);
      state.settingsLoaded = true;
      setSettingsBusy(false, "Settings loaded. Existing secret values remain in secure storage.", "ready");
      requestAnimationFrame(() => dom.settingsProvider.focus());
    } catch (error) {
      state.settingsLoaded = false;
      setSettingsBusy(false, readError(error, "Could not load settings."), "error");
    }
  }

  function closeSettingsDialog() {
    if (state.settingsSubmitting) {
      return;
    }
    resetSettingsSecrets();
    state.settingsLoaded = false;
    if (typeof dom.settingsDialog.close === "function" && dom.settingsDialog.open) {
      dom.settingsDialog.close();
    } else {
      dom.settingsDialog.removeAttribute("open");
    }
    dom.settingsButton.focus();
  }

  function populateSettingsForm(settings) {
    setSelectValue(dom.settingsProvider, settings.provider, "custom");
    setSelectValue(dom.settingsGenerationMode, settings.generation_mode, "deep");
    dom.settingsBaseUrl.value = stringValue(settings.base_url);
    dom.settingsModel.value = stringValue(settings.model);
    setSelectValue(dom.settingsDefaultMode, settings.default_mode, "interactive");
    setSelectValue(dom.settingsInteractiveRun, settings.interactive_run, "ask");
    dom.settingsMemoryResearch.checked = settings.enable_memory_research === true;
    dom.settingsWebResearch.checked = settings.enable_web_research === true;
    dom.settingsReducedMotion.checked = settings.reduced_motion === true;
    setSelectValue(dom.settingsUiLanguage, settings.ui_language, "ru");
    dom.settingsMemoryUrl.value = stringValue(settings.memory_base_url);
    dom.settingsPlatformUrl.value = stringValue(settings.platform_base_url);
    dom.settingsMutationUrl.value = stringValue(settings.mutation_base_url);
    dom.settingsMutationModel.value = stringValue(settings.mutation_model);
    dom.settingsValidationUrl.value = stringValue(settings.validation_base_url);
    dom.settingsValidationModel.value = stringValue(settings.validation_model);
    setSelectValue(dom.settingsSearchProvider, settings.search_provider, "tavily");
    setSecretPresence(dom.settingsApiKeyState, settings.api_key_present === true);
    setSecretPresence(dom.settingsMemoryKeyState, settings.memory_api_key_present === true);
    setSecretPresence(dom.settingsPlatformKeyState, settings.platform_api_key_present === true);
    setSecretPresence(dom.settingsMutationKeyState, settings.mutation_api_key_present === true);
    setSecretPresence(dom.settingsValidationKeyState, settings.validation_api_key_present === true);
    setSecretPresence(dom.settingsSearchKeyState, settings.search_api_key_present === true);
    setSecretPresence(dom.settingsUploadKeyState, settings.upload_api_key_present === true);
    resetSettingsSecrets();
  }

  async function hydrateNativeSettings() {
    try {
      const payload = await apiFetch(`${API_ROOT}/settings`);
      const settings = isRecord(payload) && isRecord(payload.settings)
        ? payload.settings
        : null;
      if (settings) {
        applyNativeSettings(settings);
      }
    } catch (_error) {
      // Startup remains usable when configuration is temporarily unavailable.
      // Opening Settings surfaces the stable server-side error with retry context.
    }
  }

  function applyNativeSettings(settings) {
    const mode = stringValue(settings.generation_mode);
    const radio = document.querySelector(`input[name="mode"][value="${mode}"]`);
    if (radio) {
      radio.checked = true;
    }
    const sessionMode = stringValue(settings.default_mode);
    const sessionRadio = document.querySelector(`input[name="session-mode"][value="${sessionMode}"]`);
    if (sessionRadio) {
      sessionRadio.checked = true;
    }
    dom.executeInput.checked = settings.interactive_run === "auto";
    document.documentElement.dataset.reducedMotion = String(
      settings.reduced_motion === true,
    );
    updateComposerFootnote();
  }

  function setSelectValue(select, value, fallback) {
    const wanted = stringValue(value);
    const options = Array.from(select.options).map((option) => option.value);
    select.value = options.includes(wanted) ? wanted : fallback;
  }

  function setSecretPresence(node, present) {
    node.textContent = present ? "Credential configured" : "Not configured";
    node.dataset.present = String(present);
  }

  function resetSettingsSecrets() {
    for (const input of [
      dom.settingsApiKey,
      dom.settingsMemoryKey,
      dom.settingsPlatformKey,
      dom.settingsMutationKey,
      dom.settingsValidationKey,
      dom.settingsSearchKey,
      dom.settingsUploadKey,
    ]) {
      input.value = "";
    }
    for (const input of [
      dom.settingsApiKeyClear,
      dom.settingsMemoryKeyClear,
      dom.settingsPlatformKeyClear,
      dom.settingsMutationKeyClear,
      dom.settingsValidationKeyClear,
      dom.settingsSearchKeyClear,
      dom.settingsUploadKeyClear,
    ]) {
      input.checked = false;
    }
  }

  function applyProviderPreset() {
    const presets = {
      openrouter: "https://openrouter.ai/api/v1",
      deepseek: "https://api.deepseek.com",
      openai: "https://api.openai.com/v1",
    };
    const selected = dom.settingsProvider.value;
    if (presets[selected]) {
      dom.settingsBaseUrl.value = presets[selected];
    }
  }

  function onSettingsSubmit(event) {
    event.preventDefault();
    if (!state.settingsLoaded || state.settingsSubmitting) {
      return;
    }
    let payload;
    try {
      payload = collectSettingsPayload();
    } catch (error) {
      setSettingsBusy(false, readError(error, "Check the settings fields."), "error");
      return;
    }
    void saveSettings(payload);
  }

  function collectSettingsPayload() {
    const payload = {
      provider: dom.settingsProvider.value,
      base_url: dom.settingsBaseUrl.value.trim(),
      model: dom.settingsModel.value.trim(),
      generation_mode: dom.settingsGenerationMode.value,
      default_mode: dom.settingsDefaultMode.value,
      interactive_run: dom.settingsInteractiveRun.value,
      memory_base_url: dom.settingsMemoryUrl.value.trim(),
      platform_base_url: dom.settingsPlatformUrl.value.trim(),
      mutation_base_url: dom.settingsMutationUrl.value.trim(),
      mutation_model: dom.settingsMutationModel.value.trim(),
      validation_base_url: dom.settingsValidationUrl.value.trim(),
      validation_model: dom.settingsValidationModel.value.trim(),
      search_provider: dom.settingsSearchProvider.value,
      enable_memory_research: dom.settingsMemoryResearch.checked,
      enable_web_research: dom.settingsWebResearch.checked,
      ui_language: dom.settingsUiLanguage.value,
      reduced_motion: dom.settingsReducedMotion.checked,
    };
    addSecretUpdate(payload, "api_key", dom.settingsApiKey, dom.settingsApiKeyClear);
    addSecretUpdate(payload, "memory_api_key", dom.settingsMemoryKey, dom.settingsMemoryKeyClear);
    addSecretUpdate(payload, "platform_api_key", dom.settingsPlatformKey, dom.settingsPlatformKeyClear);
    addSecretUpdate(payload, "mutation_api_key", dom.settingsMutationKey, dom.settingsMutationKeyClear);
    addSecretUpdate(payload, "validation_api_key", dom.settingsValidationKey, dom.settingsValidationKeyClear);
    addSecretUpdate(payload, "search_api_key", dom.settingsSearchKey, dom.settingsSearchKeyClear);
    addSecretUpdate(payload, "upload_api_key", dom.settingsUploadKey, dom.settingsUploadKeyClear);
    return payload;
  }

  function addSecretUpdate(payload, field, input, clearInput) {
    const value = input.value;
    if (value && clearInput.checked) {
      throw new Error("A key cannot be replaced and removed in the same save.");
    }
    if (clearInput.checked) {
      payload[`${field}_action`] = "clear";
    } else if (value) {
      payload[`${field}_action`] = "replace";
      payload[field] = value;
    } else {
      payload[`${field}_action`] = "keep";
    }
  }

  async function saveSettings(payload, confirmationToken = "") {
    state.settingsSubmitting = true;
    setSettingsBusy(true, confirmationToken ? "Saving confirmed settings…" : "Validating settings…");
    const body = { ...payload };
    if (confirmationToken) {
      body.confirmation_token = confirmationToken;
    }
    try {
      const response = await apiFetch(`${API_ROOT}/settings`, {
        method: "POST",
        body: JSON.stringify(body),
      });
      state.settingsSubmitting = false;
      state.settingsLoaded = true;
      if (isRecord(response) && isRecord(response.settings)) {
        populateSettingsForm(response.settings);
        applyNativeSettings(response.settings);
        if (!await persistSelectedModes()) {
          setSettingsBusy(
            false,
            "Settings were saved, but the active session mode could not be switched.",
            "warning",
          );
          return;
        }
      }
      const warnings = isRecord(response) && Array.isArray(response.warnings)
        ? response.warnings.filter((warning) => typeof warning === "string" && warning)
        : [];
      if (warnings.length) {
        setSettingsBusy(false, warnings[0], "warning");
        announce("Settings saved with a Keychain cleanup warning.");
        return;
      }
      setSettingsBusy(false, "Saved. API keys are referenced through the OS Keychain.", "ready");
      announce("Settings saved securely.");
      window.setTimeout(() => {
        if (!state.settingsSubmitting && dom.settingsDialog.open) {
          closeSettingsDialog();
          showComposerAlert("Settings saved securely. New runs will use the updated configuration.");
        }
      }, 450);
    } catch (error) {
      state.settingsSubmitting = false;
      const challenge = normalizeSettingsConfirmation(error, payload);
      if (challenge) {
        state.pendingConfirmation = challenge;
        setSettingsBusy(false, "Review the save confirmation to continue.", "warning");
        openCommandConfirmationDialog(challenge);
      } else {
        setSettingsBusy(false, readError(error, "Settings could not be saved."), "error");
      }
    }
  }

  function normalizeSettingsConfirmation(error, settingsPayload) {
    const challenge = normalizeConfirmationChallenge(error, "Native settings");
    if (!challenge) {
      return null;
    }
    const payload = error instanceof ApiError && isRecord(error.payload) ? error.payload : {};
    return {
      ...challenge,
      kind: "settings",
      settingsPayload,
      displayCommand: "Model, provider, keys, services, and defaults",
      effects: normalizeCommandEffects(payload.changes),
    };
  }

  function setSettingsBusy(busy, message, tone = "loading") {
    dom.settingsFields.setAttribute("aria-busy", String(busy));
    dom.settingsStatus.textContent = message;
    dom.settingsStatus.dataset.tone = tone;
    dom.settingsSave.disabled = busy || !state.settingsLoaded;
    dom.settingsCancel.disabled = busy;
    dom.settingsDialogClose.disabled = busy;
    for (const control of dom.settingsFields.querySelectorAll("input, select")) {
      control.disabled = busy;
    }
  }

  async function initializeSession() {
    if (state.settingsHydration) {
      await state.settingsHydration;
    }
    await loadCurrentSession({ render: true });
  }

  async function loadCurrentSession({ render = false } = {}) {
    try {
      const session = await apiFetch(`${API_ROOT}/session`);
      if (!isRecord(session) || !stringValue(session.session_id)) {
        throw new ApiError("The session service returned an invalid response.");
      }
      state.session = session;
      syncProductionPointerFromSession(session);
      applySessionModes(session);
      if (render && !state.activeRun && !state.commandSubmitting) {
        renderSessionSnapshot(session);
      }
      return session;
    } catch (error) {
      showComposerAlert(readError(error, "Could not load the persistent session."));
      return null;
    }
  }

  function applySessionModes(session) {
    const generationMode = stringValue(session && session.generation_mode);
    const generationRadio = document.querySelector(`input[name="mode"][value="${generationMode}"]`);
    if (generationRadio) {
      generationRadio.checked = true;
    }
    const sessionMode = stringValue(session && session.session_mode);
    const sessionRadio = document.querySelector(`input[name="session-mode"][value="${sessionMode}"]`);
    if (sessionRadio) {
      sessionRadio.checked = true;
    }
  }

  async function persistSelectedModes() {
    const generationRadio = document.querySelector('input[name="mode"]:checked');
    const sessionRadio = document.querySelector('input[name="session-mode"]:checked');
    if (!generationRadio || !sessionRadio) {
      return false;
    }
    try {
      const previousId = stringValue(state.session && state.session.session_id);
      const session = await apiFetch(`${API_ROOT}/session/modes`, {
        method: "POST",
        body: JSON.stringify({
          generation_mode: generationRadio.value,
          session_mode: sessionRadio.value,
        }),
      });
      if (!isRecord(session) || !stringValue(session.session_id)) {
        throw new ApiError("The session service returned an invalid response.");
      }
      state.session = session;
      syncProductionPointerFromSession(session);
      applySessionModes(session);
      if (!state.activeRun) {
        renderSessionSnapshot(session);
      }
      if (previousId && previousId !== stringValue(session.session_id)) {
        announce("Session mode changed. A fresh persistent session was started.");
      }
      return true;
    } catch (error) {
      if (state.session) {
        applySessionModes(state.session);
      }
      showComposerAlert(readError(error, "Could not save the session modes."));
      return false;
    }
  }

  function renderSessionTranscript(session, { excludeRunId = "" } = {}) {
    const turns = isRecord(session) && Array.isArray(session.turns)
      ? session.turns.filter(isRecord)
      : [];
    const fragment = document.createDocumentFragment();
    const dropped = Number(session && session.dropped_turns || 0);
    if (dropped > 0) {
      fragment.append(create("p", {
        className: "session-truncation-note",
        text: `${dropped} older turn${dropped === 1 ? " was" : "s were"} removed by the local history limit.`,
      }));
    }
    for (const turn of turns) {
      const provenance = isRecord(turn.provenance) ? turn.provenance : {};
      if (excludeRunId && stringValue(provenance.run_id) === excludeRunId) {
        continue;
      }
      const role = ["user", "assistant", "system", "tool"].includes(turn.role)
        ? turn.role
        : "system";
      const labels = { user: "You", assistant: "Maestro", system: "System", tool: "Tool" };
      const article = create("article", { className: `message session-message is-${role}` });
      article.append(
        create("div", { className: "message-label", text: labels[role] }),
        create("p", { text: stringValue(turn.text) }),
      );
      fragment.append(article);
    }
    dom.sessionTranscript.replaceChildren(fragment);
    return dom.sessionTranscript.childElementCount;
  }

  function renderSessionSnapshot(session) {
    const count = renderSessionTranscript(session);
    if (!count) {
      dom.emptyState.hidden = false;
      dom.runView.hidden = true;
      setRunHeader("New chat", "idle", "Ready");
      return;
    }
    dom.emptyState.hidden = true;
    dom.runView.hidden = false;
    dom.currentUserMessage.hidden = true;
    dom.eventTimeline.replaceChildren();
    setRunHeader("Persistent session", "idle", "Ready");
  }

  async function loadRuns({ showLoading = false } = {}) {
    if (showLoading) {
      setHistoryState("loading", "Loading runs…");
      dom.refreshHistory.classList.add("is-loading");
      dom.refreshHistory.disabled = true;
    }

    try {
      const payload = await apiFetch(`${API_ROOT}/runs`);
      const runs = Array.isArray(payload) ? payload : payload && payload.runs;
      state.runs = Array.isArray(runs) ? runs.filter(isRecord) : [];
      renderHistory();
      setServiceState("connected", "Connected");
    } catch (error) {
      setHistoryError(readError(error, "Could not load run history."));
      setServiceState("offline", "API unavailable");
    } finally {
      dom.refreshHistory.classList.remove("is-loading");
      dom.refreshHistory.disabled = false;
    }
  }

  function renderHistory() {
    const fragment = document.createDocumentFragment();
    const selectedId = state.activeRun ? String(state.activeRun.id) : null;
    let sorted = [...state.runs].sort((left, right) => {
      return timestampOf(right.created_at) - timestampOf(left.created_at);
    });
    if (Number.isInteger(state.historyDisplayLimit) && state.historyDisplayLimit > 0) {
      sorted = sorted.slice(0, state.historyDisplayLimit);
    }

    for (const run of sorted) {
      const runId = stringValue(run.id);
      if (!runId) {
        continue;
      }

      const title = historyTitle(run.prompt);
      const status = normalizeStatus(run.status);
      const button = create("button", {
        className: "history-run",
        attrs: {
          type: "button",
          title,
          "aria-current": String(runId === selectedId),
        },
      });

      button.append(create("span", { className: "history-run-title", text: title }));
      const meta = create("span", { className: "history-run-meta" });
      meta.append(
        create("span", {
          className: "history-status-dot",
          attrs: { "aria-hidden": "true", "data-tone": statusTone(status) },
        }),
        create("span", { text: statusLabel(status) }),
        create("span", {
          className: "history-run-time",
          text: formatRelativeTime(run.created_at),
        }),
      );
      button.append(meta);
      button.addEventListener("click", () => void selectRun(runId));
      fragment.append(button);
    }

    dom.historyList.replaceChildren(fragment);
    if (dom.historyList.childElementCount === 0) {
      setHistoryState("empty", "No runs yet. Start a new chat.");
    } else {
      dom.historyState.hidden = true;
    }
  }

  function setHistoryState(kind, message) {
    dom.historyState.hidden = false;
    dom.historyState.replaceChildren();
    if (kind === "loading") {
      dom.historyState.append(create("span", {
        className: "spinner",
        attrs: { "aria-hidden": "true" },
      }));
    }
    dom.historyState.append(create("span", { text: message }));
    if (kind !== "error") {
      dom.historyList.replaceChildren();
    }
  }

  function setHistoryError(message) {
    dom.historyList.replaceChildren();
    dom.historyState.hidden = false;
    const copy = create("div");
    copy.append(
      create("div", { text: message }),
      create("button", {
        className: "history-retry-button",
        text: "Retry",
        attrs: { type: "button" },
      }),
    );
    copy.querySelector("button").addEventListener("click", () => {
      void loadRuns({ showLoading: true });
    });
    dom.historyState.replaceChildren(copy);
  }

  async function selectRun(runId) {
    const token = ++state.loadToken;
    closeRunTransport();
    closeSidebar();
    state.activeRun = { id: runId, status: "loading", prompt: "" };
    state.chain = null;
    state.cards.clear();
    state.seenEventKeys.clear();
    dom.chainButton.hidden = true;
    dom.cancelButton.hidden = true;
    showRunShell("");
    setRunHeader("Loading run…", "loading", "Loading");
    dom.eventTimeline.replaceChildren(makeStateCard(
      "Loading run",
      "Fetching the saved run and its latest result.",
      "loading",
    ));
    renderHistory();
    syncComposerState();

    try {
      const run = await apiFetch(`${API_ROOT}/runs/${encodeURIComponent(runId)}`);
      if (token !== state.loadToken) {
        return;
      }
      if (!isRecord(run) || !run.id) {
        throw new ApiError("The server returned an invalid run.");
      }
      renderRun(run, { connect: true });
      setServiceState("connected", "Connected");
    } catch (error) {
      if (token !== state.loadToken) {
        return;
      }
      const message = readError(error, "Could not load this run.");
      state.activeRun = null;
      setRunHeader("Run unavailable", "failed", "Load failed");
      showRunErrorState(message, () => void selectRun(runId));
      setServiceState("offline", "API unavailable");
      syncComposerState();
      renderHistory();
    }
  }

  function renderRun(run, { connect = true } = {}) {
    clearEphemeralSecret();
    closeRunTransport();
    state.activeRun = { ...run };
    state.chain = null;
    state.cards.clear();
    state.seenEventKeys.clear();
    state.currentExecutionStep = null;
    state.transportNoticeShown = false;

    showRunShell(stringValue(run.prompt));
    renderSessionTranscript(state.session, { excludeRunId: stringValue(run.id) });
    void refreshDisplayedRunSession(run);
    dom.eventTimeline.replaceChildren();
    setRunStatus(run.status);

    const chain = extractChain(run);
    if (chain) {
      setChain(chain);
    }

    const status = normalizeStatus(run.status);
    if (run.waiting_for_input) {
      const waiting = isRecord(run.waiting_for_input) ? run.waiting_for_input : {};
      setMaestricMood("waiting_for_input", statusLabel("waiting_for_input"));
      renderHumanInput({
        prompt: waiting.prompt || "This run is waiting for your input.",
        request_id: waiting.request_id,
      });
    }

    if (status === "completed") {
      renderFinalAnswer(run.result);
    } else if (status === "failed") {
      renderRunError(run.error || "The run failed.", "Run failed");
    } else if (status === "cancelled") {
      renderCancelled();
    } else {
      appendTimeline(makeStateCard(
        "Connecting to live run",
        "Waiting for the next Maestro event…",
        "loading",
        "transport-state-card",
      ));
    }

    syncComposerState();
    renderHistory();
    if (connect && (!TERMINAL_STATUSES.has(status) || Number(run.event_count || 0) > 0)) {
      connectRunEvents(stringValue(run.id));
    }
  }

  async function refreshDisplayedRunSession(run) {
    const runId = stringValue(run && run.id);
    const sessionId = stringValue(run && run.session_id);
    if (!runId || !sessionId) {
      return;
    }
    try {
      const session = await apiFetch(`${API_ROOT}/sessions/${encodeURIComponent(sessionId)}`);
      if (!state.activeRun || stringValue(state.activeRun.id) !== runId || !isRecord(session)) {
        return;
      }
      renderSessionTranscript(session, { excludeRunId: runId });
      if (session.is_current === true) {
        state.session = session;
        syncProductionPointerFromSession(session);
        applySessionModes(session);
      }
    } catch (_error) {
      // The run itself remains inspectable even if its archived session was pruned.
    }
  }

  async function newChat() {
    const previousWasActive = state.activeRun && isRunActive(state.activeRun.status);
    const generationRadio = document.querySelector('input[name="mode"]:checked');
    const sessionRadio = document.querySelector('input[name="session-mode"]:checked');
    try {
      const session = await apiFetch(`${API_ROOT}/session/new`, {
        method: "POST",
        body: JSON.stringify({
          generation_mode: generationRadio ? generationRadio.value : "fast",
          session_mode: sessionRadio ? sessionRadio.value : "interactive",
        }),
      });
      if (!isRecord(session) || !stringValue(session.session_id)) {
        throw new ApiError("The session service returned an invalid response.");
      }
      state.session = session;
      syncProductionPointerFromSession(session);
      applySessionModes(session);
    } catch (error) {
      showComposerAlert(readError(error, "Could not start a new persistent session."));
      return;
    }
    resetConversationView();
    if (previousWasActive) {
      announce("New session opened. The previous run continues in its archived session.");
    } else {
      announce("New persistent session opened.");
    }
  }

  async function clearConversation() {
    try {
      const session = await apiFetch(`${API_ROOT}/session/clear`, {
        method: "POST",
        body: JSON.stringify({}),
      });
      if (!isRecord(session) || !stringValue(session.session_id)) {
        throw new ApiError("The session service returned an invalid response.");
      }
      state.session = session;
      syncProductionPointerFromSession(session);
      applySessionModes(session);
    } catch (error) {
      showComposerAlert(readError(error, "Could not clear the persistent session."));
      return;
    }
    resetConversationView();
    announce("Persistent session cleared.");
  }

  function resetConversationView() {
    clearEphemeralSecret();
    ++state.loadToken;
    closeRunTransport();
    state.activeRun = null;
    state.chain = null;
    state.cards.clear();
    state.seenEventKeys.clear();
    state.currentExecutionStep = null;
    dom.sessionTranscript.replaceChildren();
    dom.emptyState.hidden = false;
    dom.runView.hidden = true;
    dom.currentUserMessage.hidden = true;
    dom.eventTimeline.replaceChildren();
    dom.userPrompt.textContent = "";
    dom.chainButton.hidden = true;
    dom.cancelButton.hidden = true;
    setRunHeader("New chat", "idle", "Ready");
    hideComposerAlert();
    closeSlashMenu();
    dom.promptInput.value = "";
    deactivateMultiLineMode();
    resizePrompt();
    syncComposerState();
    renderHistory();
    closeSidebar();
    dom.promptInput.focus();
  }

  function normalizeProductionPointer(value) {
    if (!isRecord(value)) {
      return null;
    }
    const entityId = stringValue(value.entity_id || value.id);
    const channel = stringValue(value.channel || "latest").trim().toLowerCase();
    if (!entityId.trim() || entityId.length > 512 || /[\u0000-\u001f\u007f]/.test(entityId)) {
      return null;
    }
    if (channel !== "latest") {
      return null;
    }
    return { entity_id: entityId, channel };
  }

  function syncProductionPointerFromSession(session) {
    state.productionPointer = null;
    const turns = isRecord(session) && Array.isArray(session.turns) ? session.turns : [];
    for (let index = turns.length - 1; index >= 0; index -= 1) {
      const turn = isRecord(turns[index]) ? turns[index] : {};
      const provenance = isRecord(turn.provenance) ? turn.provenance : {};
      const pointer = normalizeProductionPointer(provenance.production_revision_pointer);
      if (pointer) {
        state.productionPointer = pointer;
        return;
      }
    }
  }

  function quoteCommandArgument(value) {
    return `'${stringValue(value).replace(/'/g, `'"'"'`)}'`;
  }

  function parseComposerCommand(value) {
    const command = stringValue(value).trim();
    if (!command) {
      return null;
    }
    if (command.startsWith("/")) {
      const match = command.match(/^\/([^\s]+)(?:\s+([\s\S]*))?$/);
      const name = match ? match[1].toLowerCase() : "";
      const args = match && match[2] ? match[2] : "";
      const local = LOCAL_COMMANDS.some((entry) => entry.invocation === `/${name}`);
      return { kind: local ? "local" : "backend", command, name, args };
    }
    if (/^(?:maestro|care)(?:\s|$)/i.test(command)) {
      return { kind: "backend", command, name: "", args: "" };
    }
    return null;
  }

  async function dispatchComposerCommand(request) {
    if (state.activeRun && isRunActive(state.activeRun.status)) {
      showComposerAlert("Cancel the active run or start a new chat first.");
      return;
    }
    closeSlashMenu();
    hideComposerAlert();
    const unavailable = unavailableCommandFor(request.command);
    if (unavailable) {
      renderLocalCommandResult(
        redactCommandForDisplay(request.command),
        unavailable.unavailableReason || "This command is unavailable in the desktop app.",
        false,
      );
      return;
    }
    if (request.kind === "local") {
      await executeLocalCommand(request);
      return;
    }
    dom.promptInput.value = "";
    deactivateMultiLineMode();
    resizePrompt();
    await executeBackendCommand(request.command);
  }

  function unavailableCommandFor(command) {
    const normalized = stringValue(command)
      .trim()
      .toLowerCase()
      .replace(/^\//, "")
      .replace(/^(?:maestro|care)\s+/, "");
    return state.commandEntries
      .filter((entry) => entry.available === false)
      .sort((left, right) => right.invocation.length - left.invocation.length)
      .find((entry) => {
        const candidate = stringValue(entry.invocation)
          .trim()
          .toLowerCase()
          .replace(/^\//, "")
          .replace(/^(?:maestro|care)\s+/, "");
        return normalized === candidate || normalized.startsWith(`${candidate} `);
      }) || null;
  }

  async function executeLocalCommand(request) {
    const command = `/${request.name}`;
    const args = stringValue(request.args).trim();
    const invocation = args ? `${command} ${args}` : command;

    if (request.name === "new" || request.name === "clear") {
      if (args) {
        renderLocalCommandResult(command, `${command} does not accept arguments.`, false);
        return;
      }
      if (request.name === "new") {
        await newChat();
      } else {
        await clearConversation();
      }
      return;
    }

    if (request.name === "theme") {
      const themeMap = { auto: "system", warm: "light", crystal: "dark" };
      if (!args) {
        const selected = document.documentElement.dataset.theme || "system";
        const current = { system: "Auto", light: "Warm", dark: "Crystal" }[selected] || "Auto";
        renderLocalCommandResult(command, `Current theme: ${current}. Use /theme auto|warm|crystal.`);
        return;
      }
      const target = themeMap[args.toLowerCase()];
      if (!target) {
        renderLocalCommandResult(command, "Unknown theme. Use /theme auto|warm|crystal.", false);
        return;
      }
      setTheme(target);
      renderLocalCommandResult(`${command} ${args.toLowerCase()}`, `Theme changed to ${humanize(args)}.`);
      return;
    }

    if (request.name === "settings") {
      if (args) {
        renderLocalCommandResult(command, "/settings does not accept arguments.", false);
        return;
      }
      clearLocalCommandInput();
      await openSettingsDialog();
      return;
    }

    if (request.name === "mode") {
      const selectedMode = document.querySelector('input[name="session-mode"]:checked');
      if (!args) {
        renderLocalCommandResult(command, `Current mode: ${humanize(selectedMode ? selectedMode.value : "interactive")}. Use /mode interactive|production.`);
        return;
      }
      const aliases = {
        interactive: "interactive",
        chat: "interactive",
        "ad-hoc": "interactive",
        adhoc: "interactive",
        production: "production",
        prod: "production",
        agent: "production",
      };
      const target = aliases[args.toLowerCase()];
      if (!target) {
        renderLocalCommandResult(command, "Unknown mode. Use /mode interactive|production.", false);
        return;
      }
      const radio = document.querySelector(`input[name="session-mode"][value="${target}"]`);
      if (!radio) {
        renderLocalCommandResult(command, "The requested mode is unavailable.", false);
        return;
      }
      radio.checked = true;
      if (!await persistSelectedModes()) {
        return;
      }
      renderLocalCommandResult(`${command} ${target}`, `Session mode changed to ${humanize(target)}.`);
      return;
    }

    if (request.name === "multi") {
      activateMultiLineMode(request.args);
      announce("Multi-line composer opened. Press Command or Control plus Enter to submit.");
      return;
    }

    if (request.name === "tour") {
      renderDesktopTour(invocation, args);
      return;
    }

    if (request.name === "sessions") {
      await renderDesktopSessions(invocation, args);
      return;
    }

    if (request.name === "resume") {
      await resumeDesktopRun(invocation, args);
      return;
    }

    if (request.name === "edit") {
      renderDesktopEdit(invocation, args);
      return;
    }

    if (request.name === "artifacts") {
      renderDesktopArtifacts(invocation, args);
      return;
    }

    if (request.name === "blocks") {
      await handleDesktopBlocks(invocation, args);
      return;
    }

    if (request.name === "subagents") {
      renderDesktopSubagents(invocation, args);
      return;
    }

    if (request.name === "branch") {
      await handleDesktopBranch(invocation, args);
      return;
    }

    if (request.name === "history" || request.name === "runs") {
      if (args && !/^\d+$/.test(args)) {
        renderLocalCommandResult(command, `Use ${command} [N] with a positive integer limit.`, false);
        return;
      }
      const requestedLimit = args ? Number(args) : 10;
      if (!Number.isSafeInteger(requestedLimit) || requestedLimit < 1 || requestedLimit > 1000) {
        renderLocalCommandResult(command, `${command} limit must be between 1 and 1000.`, false);
        return;
      }
      state.historyDisplayLimit = requestedLimit;
      dom.promptInput.value = "";
      deactivateMultiLineMode();
      resizePrompt();
      syncComposerState();
      openSidebar();
      await loadRuns({ showLoading: true });
      announce(`Run history opened with a ${requestedLimit}-run limit.`);
      return;
    }

    if (request.name === "help") {
      dom.promptInput.value = "";
      deactivateMultiLineMode();
      resizePrompt();
      renderCommandHelp(args);
      return;
    }

    if (request.name === "quit" || request.name === "exit") {
      if (args) {
        renderLocalCommandResult(command, `${command} does not accept arguments.`, false);
        return;
      }
      dom.promptInput.value = "";
      deactivateMultiLineMode();
      resizePrompt();
      await closeNativeApp(command);
    }
  }

  function clearLocalCommandInput() {
    dom.promptInput.value = "";
    deactivateMultiLineMode();
    resizePrompt();
    closeSlashMenu();
    hideComposerAlert();
  }

  function makeLocalInspectorCard(command, title, message, tone = "complete") {
    const className = tone === "error"
      ? "command-result-card is-error local-inspector-card"
      : tone === "warning"
        ? "command-result-card is-warning local-inspector-card"
        : "command-help-card local-inspector-card";
    const card = create("article", { className });
    const header = create("div", { className: "local-inspector-header" });
    header.append(
      create("div", { className: "command-help-heading", text: title }),
      create("span", { className: "command-exit-badge", text: "Local" }),
    );
    card.append(
      header,
      create("code", { className: "command-result-invocation", text: command }),
    );
    if (message) {
      card.append(create("p", { className: "command-result-message", text: message }));
    }
    return card;
  }

  function presentLocalInspector(command, card, announcement, { preserveRun = true } = {}) {
    const keepRun = Boolean(
      preserveRun
      && state.activeRun
      && !isRunActive(state.activeRun.status)
      && !dom.runView.hidden,
    );
    clearLocalCommandInput();
    if (keepRun) {
      card.classList.add("is-inline-inspector");
      appendTimeline(card, { beforeFinal: false });
      syncComposerState();
      scrollConversation(true);
    } else {
      const failed = card.classList.contains("is-error");
      prepareCommandView(
        command,
        failed ? "failed" : "completed",
        failed ? "Command error" : "Local view",
      );
      dom.eventTimeline.replaceChildren(card);
      syncComposerState();
      scrollConversation(true);
    }
    announce(announcement);
  }

  function renderLocalInspectorError(command, message, { preserveRun = true } = {}) {
    const card = makeLocalInspectorCard(command, "Desktop command rejected", message, "error");
    presentLocalInspector(command, card, message, { preserveRun });
  }

  function sortedDesktopRuns(newestFirst = true) {
    const runs = state.runs
      .filter((run) => isRecord(run) && stringValue(run.id))
      .map((run, index) => ({ run, index }));
    runs.sort((left, right) => {
      const delta = timestampOf(left.run.created_at) - timestampOf(right.run.created_at);
      if (delta !== 0) {
        return newestFirst ? -delta : delta;
      }
      return newestFirst ? left.index - right.index : right.index - left.index;
    });
    return runs.map((entry) => entry.run);
  }

  function renderDesktopTour(command, args) {
    const note = args
      ? "Arguments are ignored; the desktop walkthrough always starts at step one."
      : "A five-step walkthrough of the native Maestro workflow.";
    const card = makeLocalInspectorCard(command, "Welcome to Maestro", note);
    const steps = [
      ["01", "Describe the outcome", "Write a task in plain language. Maestro turns it into a validated chain."],
      ["02", "Choose depth", "Fast is the compact default; Deep spends more reasoning on the chain design."],
      ["03", "Decide whether to execute", "Leave Execute off to inspect the chain first, or enable it to run after validation."],
      ["04", "Follow Maestric", "The companion and timeline show generation, execution, tools, input requests, and completion."],
      ["05", "Stay on the keyboard", "Type / for slash commands or press Command/Control + K for the complete command palette."],
    ];
    const list = create("ol", { className: "local-tour-list" });
    for (const [number, title, description] of steps) {
      const item = create("li", { className: "local-tour-step" });
      item.append(
        create("span", { className: "local-tour-number", text: number, attrs: { "aria-hidden": "true" } }),
        create("div", { className: "local-tour-copy" }),
      );
      item.lastElementChild.append(
        create("strong", { text: title }),
        create("span", { text: description }),
      );
      list.append(item);
    }
    const actions = create("div", { className: "local-inspector-actions" });
    const focusTask = create("button", {
      className: "state-action-button",
      text: "Start a task",
      attrs: { type: "button" },
    });
    const openCommands = create("button", {
      className: "state-action-button",
      text: "Open commands",
      attrs: { type: "button" },
    });
    focusTask.addEventListener("click", () => dom.promptInput.focus());
    openCommands.addEventListener("click", openCommandDialog);
    actions.append(focusTask, openCommands);
    card.append(list, actions);
    presentLocalInspector(command, card, "Desktop tour opened.");
  }

  async function renderDesktopSessions(command, args) {
    if (args) {
      renderLocalInspectorError(
        command,
        "Use /sessions without arguments, then /resume latest or /resume <session-id>.",
      );
      return;
    }
    let sessions;
    try {
      const payload = await apiFetch(`${API_ROOT}/sessions`);
      sessions = isRecord(payload) && Array.isArray(payload.sessions)
        ? payload.sessions.filter(isRecord)
        : [];
      state.sessions = sessions;
    } catch (error) {
      renderLocalInspectorError(command, readError(error, "Could not load persistent sessions."));
      return;
    }
    const card = makeLocalInspectorCard(
      command,
      "Persistent sessions",
      "Workspace-scoped conversations survive native app restarts. Run telemetry remains separately bounded in memory.",
    );
    appendSessionRows(card, sessions);
    presentLocalInspector(command, card, sessions.length ? "Persistent sessions listed." : "No persistent sessions found.");
  }

  function appendSessionRows(card, sessions) {
    const visible = sessions.slice(0, MAX_LOCAL_LIST_ROWS);
    const list = create("div", { className: "local-inspector-list" });
    for (const session of visible) {
      const sessionId = stringValue(session.session_id);
      const preview = stringValue(session.first_user_preview) || "Untitled session";
      const row = create("div", { className: "local-inspector-row" });
      const copy = create("div", { className: "local-inspector-row-copy" });
      copy.append(
        create("strong", { text: preview }),
        create("span", {
          text: `${humanize(session.session_mode || "interactive")} · ${Number(session.turn_count || 0)} turns · ${formatRelativeTime(session.updated_at) || "time unavailable"}${session.is_current ? " · current" : ""}`,
        }),
        create("code", { text: sessionId }),
      );
      const resume = create("button", {
        className: "local-row-action",
        text: "Resume",
        attrs: { type: "button", "aria-label": `Resume ${preview}` },
      });
      resume.addEventListener("click", () => void resumePersistentSession(sessionId));
      row.append(copy, resume);
      list.append(row);
    }
    if (!visible.length) {
      list.append(create("p", {
        className: "command-empty-state",
        text: "No persistent sessions are available yet.",
      }));
    }
    card.append(list);
    if (sessions.length > visible.length) {
      card.append(create("p", {
        className: "command-result-meta",
        text: `Showing the newest ${visible.length} of ${sessions.length} sessions.`,
      }));
    }
  }

  async function resumeDesktopRun(command, args) {
    const target = args.trim();
    if (!target) {
      await renderDesktopSessions(command, "");
      return;
    }
    if (/\s/.test(target)) {
      renderLocalInspectorError(command, "Use /resume latest or /resume <exact-session-id>.");
      return;
    }
    await resumePersistentSession(target, command);
  }

  async function resumePersistentSession(target, command = "/resume") {
    try {
      const session = await apiFetch(`${API_ROOT}/session/resume`, {
        method: "POST",
        body: JSON.stringify({ target }),
      });
      if (!isRecord(session) || !stringValue(session.session_id)) {
        throw new ApiError("The session service returned an invalid response.");
      }
      state.session = session;
      syncProductionPointerFromSession(session);
      resetConversationView();
      applySessionModes(session);
      renderSessionSnapshot(session);
      const previewTurn = Array.isArray(session.turns)
        ? session.turns.find((turn) => isRecord(turn) && turn.role === "user")
        : null;
      setRunHeader(
        historyTitle(previewTurn ? previewTurn.text : "Persistent session"),
        "idle",
        "Resumed",
      );
      announce("Persistent session resumed.");
    } catch (error) {
      renderLocalInspectorError(
        command,
        readError(error, `Could not resume session “${truncate(target, 120)}”.`),
      );
    }
  }

  function desktopPromptRows() {
    return sortedDesktopRuns(false)
      .filter((run) => stringValue(run.prompt).trim())
      .map((run, index) => ({
        index: index + 1,
        prompt: stringValue(run.prompt),
        runId: stringValue(run.id),
        createdAt: run.created_at,
      }));
  }

  function renderDesktopEdit(command, args) {
    const prompts = desktopPromptRows();
    const target = args.trim().toLowerCase();
    if (target === "list") {
      const card = makeLocalInspectorCard(
        command,
        "Editable desktop prompts",
        "Indices refer to runs in this app process, oldest first. Editing only pre-fills a fresh task; it never re-executes automatically.",
      );
      const list = create("div", { className: "local-inspector-list" });
      for (const row of prompts.slice(-MAX_LOCAL_LIST_ROWS)) {
        const item = create("div", { className: "local-inspector-row" });
        const copy = create("div", { className: "local-inspector-row-copy" });
        copy.append(
          create("strong", { text: `${row.index}. ${historyTitle(row.prompt)}` }),
          create("span", { text: formatRelativeTime(row.createdAt) || row.runId }),
        );
        const edit = create("button", {
          className: "local-row-action",
          text: "Edit",
          attrs: { type: "button", "aria-label": `Edit prompt ${row.index}` },
        });
        edit.addEventListener("click", () => prefillDesktopPrompt(row.prompt, row.index));
        item.append(copy, edit);
        list.append(item);
      }
      if (!prompts.length) {
        list.append(create("p", { className: "command-empty-state", text: "No prior desktop prompts to edit." }));
      }
      card.append(list);
      presentLocalInspector(command, card, prompts.length ? "Editable prompts listed." : "No editable prompts found.");
      return;
    }
    if (!prompts.length) {
      renderLocalInspectorError(command, "No prior desktop prompts are available to edit.");
      return;
    }
    let selected = prompts[prompts.length - 1];
    if (target) {
      if (!/^\d+$/.test(target)) {
        renderLocalInspectorError(command, `Desktop /edit expects a 1-based integer or “list”, not “${truncate(args, 80)}”.`);
        return;
      }
      const index = Number(target);
      selected = prompts.find((row) => row.index === index);
      if (!selected) {
        renderLocalInspectorError(command, `Prompt ${index} is out of range; this app process has ${prompts.length}.`);
        return;
      }
    }
    const card = makeLocalInspectorCard(
      command,
      `Editing desktop prompt ${selected.index}`,
      "The original text is in the composer. Change it and submit to create a fresh run; nothing has executed yet.",
    );
    card.append(create("pre", {
      className: "local-inspector-preview",
      text: truncate(selected.prompt, MAX_PREVIEW_CHARS),
    }));
    presentLocalInspector(command, card, `Prompt ${selected.index} loaded for editing.`);
    prefillDesktopPrompt(selected.prompt, selected.index);
  }

  function prefillDesktopPrompt(prompt, index) {
    const value = stringValue(prompt);
    hideComposerAlert();
    closeSlashMenu();
    if (value.includes("\n")) {
      activateMultiLineMode(value);
    } else {
      deactivateMultiLineMode();
      dom.promptInput.value = value;
      syncComposerState();
      resizePrompt();
      dom.promptInput.focus();
      dom.promptInput.setSelectionRange(value.length, value.length);
    }
    announce(`Prompt ${index} is ready to edit; submitting creates a new run.`);
  }

  function renderDesktopArtifacts(command, args) {
    if (args) {
      renderLocalInspectorError(command, "Desktop /artifacts does not accept arguments.");
      return;
    }
    const run = state.activeRun;
    const chain = state.chain || extractChain(run);
    const artifacts = collectDesktopArtifacts(run, chain);
    if (!run && !chain) {
      renderLocalInspectorError(command, "No current run is open. Use /sessions and /resume first.", { preserveRun: false });
      return;
    }
    const card = makeLocalInspectorCard(
      command,
      "Current run artifacts",
      "A bounded view of the generated chain, intermediate artifacts, and result attached to this desktop run. This is not the persisted TUI artifact store.",
    );
    const list = create("div", { className: "local-artifact-list" });
    for (const artifact of artifacts) {
      const details = create("details", { className: "local-artifact" });
      details.append(
        create("summary", { text: artifact.label }),
        create("pre", { text: truncate(valueToDisplay(artifact.value), MAX_PREVIEW_CHARS) }),
      );
      list.append(details);
    }
    if (!artifacts.length) {
      list.append(create("p", { className: "command-empty-state", text: "This run has no inspectable artifacts yet." }));
    }
    card.append(list);
    presentLocalInspector(command, card, artifacts.length ? "Run artifacts opened." : "No run artifacts found.");
  }

  function collectDesktopArtifacts(run, chain) {
    const artifacts = [];
    const labels = new Set();
    const add = (label, value) => {
      if (!hasUsefulValue(value) || artifacts.length >= MAX_LOCAL_ARTIFACTS || labels.has(label)) {
        return;
      }
      labels.add(label);
      artifacts.push({ label, value });
    };
    if (chain) {
      add(`Generated chain · ${chainSteps(chain).length} steps`, chain);
    }
    const result = isRecord(run) ? run.result : null;
    const addContainer = (prefix, value) => {
      if (Array.isArray(value)) {
        value.forEach((item, index) => add(`${prefix} ${index + 1}`, item));
      } else if (isRecord(value)) {
        for (const [key, item] of Object.entries(value)) {
          add(`${prefix} · ${humanize(key)}`, item);
        }
      }
    };
    if (isRecord(result)) {
      addContainer("Intermediate artifact", result.intermediate_artifacts);
      addContainer("Artifact", result.artifacts);
      if (isRecord(result.result)) {
        addContainer("Intermediate artifact", result.result.intermediate_artifacts);
        addContainer("Artifact", result.result.artifacts);
      }
    }
    if (hasUsefulValue(result)) {
      add("Run result", result);
    }
    return artifacts;
  }

  async function handleDesktopBlocks(command, args) {
    const blocks = collectVisibleCodeBlocks();
    const rawArgs = args.trim();
    const tokens = rawArgs ? rawArgs.split(/\s+/) : [];
    if (!rawArgs) {
      renderDesktopBlockList(command, blocks);
      return;
    }
    const action = tokens[0].toLowerCase();
    if (action === "save") {
      const match = rawArgs.match(/^save\s+(\d+)\s+(.+)$/s);
      if (!match) {
        renderLocalInspectorError(command, "Use /blocks save <N> <workspace-relative-path>.");
        return;
      }
      const index = Number(match[1]);
      const block = blocks[index - 1];
      if (!block) {
        renderLocalInspectorError(command, `Code block ${index} is out of range; the visible run has ${blocks.length}.`);
        return;
      }
      if (block.truncated) {
        renderLocalInspectorError(command, `Code block ${index} exceeds the ${MAX_COMMAND_OUTPUT_CHARS}-character file safety limit.`);
        return;
      }
      let relativePath = match[2].trim();
      if (
        relativePath.length >= 2
        && ((relativePath.startsWith('"') && relativePath.endsWith('"'))
          || (relativePath.startsWith("'") && relativePath.endsWith("'")))
      ) {
        relativePath = relativePath.slice(1, -1);
      }
      if (!relativePath) {
        renderLocalInspectorError(command, "The workspace-relative destination path is empty.");
        return;
      }
      clearLocalCommandInput();
      prepareCommandView(command, "executing", "Saving code block");
      dom.eventTimeline.replaceChildren(makeStateCard(
        "Saving code block",
        "Writing the selected block atomically inside the current workspace…",
        "loading",
      ));
      try {
        const result = await apiFetch(`${API_ROOT}/workspace-files`, {
          method: "POST",
          body: JSON.stringify({ path: relativePath, content: block.body }),
        });
        const savedPath = isRecord(result) ? stringValue(result.path) : relativePath;
        const byteCount = isRecord(result) ? numberOr(result.bytes, utf8ByteLength(block.body)) : utf8ByteLength(block.body);
        renderLocalCommandResult(
          command,
          `Saved code block ${index} to ${savedPath} (${byteCount.toLocaleString()} UTF-8 bytes).`,
        );
      } catch (error) {
        renderLocalCommandResult(command, readError(error, "The code block could not be saved."), false);
      }
      return;
    }
    if (action !== "copy" || tokens.length !== 2 || !/^\d+$/.test(tokens[1])) {
      renderLocalInspectorError(command, "Use /blocks, /blocks copy <N>, or /blocks save <N> <workspace-relative-path>.");
      return;
    }
    const index = Number(tokens[1]);
    const block = blocks[index - 1];
    if (!block) {
      renderLocalInspectorError(command, `Code block ${index} is out of range; the visible run has ${blocks.length}.`);
      return;
    }
    if (block.truncated) {
      renderLocalInspectorError(command, `Code block ${index} exceeds the ${MAX_COMMAND_OUTPUT_CHARS}-character clipboard safety limit.`);
      return;
    }
    const copied = await copyTextToClipboard(block.body);
    if (!copied) {
      renderLocalInspectorError(command, "The operating system denied clipboard access. No text was copied.");
      return;
    }
    const card = makeLocalInspectorCard(
      command,
      `Copied code block ${index}`,
      `${block.language || "Plain text"} · ${block.body.split("\n").length} lines · source: ${block.source}`,
    );
    card.append(create("pre", { className: "local-inspector-preview", text: truncate(block.body, 700) }));
    presentLocalInspector(command, card, `Code block ${index} copied.`);
  }

  function renderDesktopBlockList(command, blocks) {
    const card = makeLocalInspectorCard(
      command,
      "Code blocks in the visible run",
      "Blocks are indexed in visible timeline order. Copy stays local; save writes atomically inside this workspace.",
    );
    const list = create("div", { className: "local-inspector-list" });
    blocks.forEach((block, index) => {
      const row = create("div", { className: "local-inspector-row" });
      const copy = create("div", { className: "local-inspector-row-copy" });
      const firstLine = block.body.split("\n")[0] || "(empty block)";
      copy.append(
        create("strong", { text: `${index + 1}. ${block.language || "plain"} · ${block.body.split("\n").length} lines` }),
        create("span", { text: `${block.source} · ${truncate(firstLine, 100)}` }),
      );
      const button = create("button", {
        className: "local-row-action",
        text: block.truncated ? "Too large" : "Copy",
        attrs: {
          type: "button",
          disabled: block.truncated ? "" : undefined,
          "aria-label": `Copy code block ${index + 1}`,
        },
      });
      button.addEventListener("click", async () => {
        const copied = await copyTextToClipboard(block.body);
        button.textContent = copied ? "Copied" : "Denied";
        announce(copied ? `Code block ${index + 1} copied.` : "Clipboard access was denied.");
      });
      row.append(copy, button);
      list.append(row);
    });
    if (!blocks.length) {
      list.append(create("p", {
        className: "command-empty-state",
        text: "No fenced code blocks are visible in this run.",
      }));
    }
    card.append(list);
    presentLocalInspector(command, card, blocks.length ? "Code blocks listed." : "No code blocks found.");
  }

  function collectVisibleCodeBlocks() {
    const blocks = [];
    const seen = new Set();
    const addBlock = (block, node) => {
      const key = `${block.language}\u0000${block.body}`;
      if (seen.has(key) || blocks.length >= MAX_LOCAL_LIST_ROWS) {
        return;
      }
      seen.add(key);
      const owner = node.closest("article");
      const source = owner && owner.querySelector("h3")
        ? stringValue(owner.querySelector("h3").textContent)
        : "timeline";
      const truncated = block.body.length > MAX_COMMAND_OUTPUT_CHARS;
      blocks.push({
        ...block,
        body: truncated ? block.body.slice(0, MAX_COMMAND_OUTPUT_CHARS) : block.body,
        truncated,
        source: truncate(source, 80),
      });
    };
    for (const code of dom.eventTimeline.querySelectorAll("pre code")) {
      const pre = code.closest("pre");
      const className = stringValue(code.className);
      const classLanguage = className.match(/(?:^|\s)language-([^\s]+)/);
      const language = stringValue(
        code.dataset.language
        || (pre && pre.dataset.language)
        || (classLanguage && classLanguage[1]),
      ).slice(0, 40);
      addBlock({ language, body: stringValue(code.textContent) }, code);
      if (blocks.length >= MAX_LOCAL_LIST_ROWS) {
        return blocks;
      }
    }
    const nodes = dom.eventTimeline.querySelectorAll(
      ".assistant-answer-content, .stream-output, .result-preview, .command-output-section pre",
    );
    for (const node of nodes) {
      for (const block of extractFencedCodeBlocks(node.textContent)) {
        addBlock(block, node);
        if (blocks.length >= MAX_LOCAL_LIST_ROWS) {
          return blocks;
        }
      }
    }
    return blocks;
  }

  function extractFencedCodeBlocks(value) {
    const lines = stringValue(value).replace(/\r\n?/g, "\n").split("\n");
    const blocks = [];
    let fence = null;
    let language = "";
    let body = [];
    for (const line of lines) {
      if (!fence) {
        const opening = line.match(/^ {0,3}(`{3,}|~{3,})(.*)$/);
        if (!opening) {
          continue;
        }
        fence = { marker: opening[1][0], length: opening[1].length };
        language = opening[2].trim().split(/\s+/, 1)[0].slice(0, 40);
        body = [];
        continue;
      }
      if (isClosingFence(line, fence.marker, fence.length)) {
        blocks.push({ language, body: body.join("\n") });
        fence = null;
        language = "";
        body = [];
      } else {
        body.push(line);
      }
    }
    return blocks;
  }

  function isClosingFence(line, marker, minimumLength) {
    const value = line.trim();
    let count = 0;
    while (value[count] === marker) {
      count += 1;
    }
    return count >= minimumLength && value.slice(count).trim() === "";
  }

  async function copyTextToClipboard(value) {
    if (!navigator.clipboard || typeof navigator.clipboard.writeText !== "function") {
      return false;
    }
    try {
      await navigator.clipboard.writeText(stringValue(value));
      return true;
    } catch (_error) {
      return false;
    }
  }

  function renderDesktopSubagents(command, args) {
    const action = stringValue(args).trim().toLowerCase();
    if (action === "clear") {
      let cleared = 0;
      for (const [key, card] of Array.from(state.cards.entries())) {
        if (key.startsWith("execution:step:") || key.startsWith("execution:tool:")) {
          if (card && card.node) {
            card.node.remove();
          }
          state.cards.delete(key);
          cleared += 1;
        }
      }
      const result = makeLocalInspectorCard(
        command,
        "Subagent buffers cleared",
        `Removed ${cleared} captured step/tool card${cleared === 1 ? "" : "s"} from this native session.`,
      );
      presentLocalInspector(command, result, "Subagent buffers cleared.");
      return;
    }
    if (action) {
      renderLocalInspectorError(
        command,
        "Use /subagents or /subagents clear.",
      );
      return;
    }
    if (!state.activeRun) {
      renderLocalInspectorError(command, "No current run is open. Use /sessions and /resume first.", { preserveRun: false });
      return;
    }
    const snapshot = desktopStepEventSnapshot();
    const card = makeLocalInspectorCard(
      command,
      "Captured step and tool activity",
      `${snapshot.steps.length} chain steps · ${snapshot.capturedEvents} captured step/tool cards · ${numberOr(state.activeRun.event_count, 0)} server events reported. Historical runs may not replay every prior live event.`,
    );
    const list = create("div", { className: "local-agent-tree" });
    for (const step of snapshot.steps) {
      const row = create("details", { className: "local-agent-step" });
      const summary = create("summary");
      summary.append(
        create("span", { className: "local-agent-marker", text: String(step.number) }),
        create("span", { className: "local-agent-title", text: step.title }),
        create("span", { className: "local-agent-status", text: step.status }),
      );
      row.append(summary);
      if (step.lines.length) {
        row.append(create("pre", { text: truncate(step.lines.join("\n"), MAX_PREVIEW_CHARS) }));
      } else {
        row.append(create("p", { text: "No process-local event detail was captured for this step." }));
      }
      list.append(row);
    }
    for (const tool of snapshot.tools) {
      const row = create("details", { className: "local-agent-step local-agent-tool" });
      const summary = create("summary");
      summary.append(
        create("span", { className: "local-agent-marker", text: "T" }),
        create("span", { className: "local-agent-title", text: tool.title }),
        create("span", { className: "local-agent-status", text: tool.status }),
      );
      row.append(summary, create("pre", { text: truncate(tool.lines.join("\n"), MAX_PREVIEW_CHARS) }));
      list.append(row);
    }
    if (!snapshot.steps.length && !snapshot.tools.length) {
      list.append(create("p", {
        className: "command-empty-state",
        text: "No chain-step or tool activity is available for this run.",
      }));
    }
    card.append(list);
    presentLocalInspector(command, card, "Step and tool activity opened.");
  }

  function desktopStepEventSnapshot() {
    const chain = state.chain || extractChain(state.activeRun);
    const definedSteps = chainSteps(chain);
    const cardByStep = new Map();
    const tools = [];
    let capturedEvents = 0;
    for (const [key, card] of state.cards.entries()) {
      if (key.startsWith("execution:step:")) {
        cardByStep.set(key.slice("execution:step:".length), card);
        capturedEvents += 1;
      } else if (key.startsWith("tool:")) {
        tools.push(localEventCardSnapshot(card));
        capturedEvents += 1;
      }
    }
    const steps = definedSteps.map((step, index) => {
      const number = step.number !== undefined ? String(step.number) : String(index + 1);
      const card = cardByStep.get(number);
      const event = card ? localEventCardSnapshot(card) : null;
      return {
        number,
        title: firstText(step, ["title", "name", "task"]) || `Step ${number}`,
        status: event ? event.status : "No event captured",
        lines: event ? event.lines : [],
      };
    });
    for (const [number, card] of cardByStep.entries()) {
      if (steps.some((step) => step.number === number)) {
        continue;
      }
      const event = localEventCardSnapshot(card);
      steps.push({ number, title: event.title || `Step ${number}`, status: event.status, lines: event.lines });
    }
    return { steps, tools, capturedEvents };
  }

  function localEventCardSnapshot(card) {
    const lines = [];
    for (const node of card.body.querySelectorAll(".event-line, .stream-output, .result-preview")) {
      const value = stringValue(node.textContent).trim();
      if (value) {
        lines.push(value);
      }
    }
    return {
      title: stringValue(card.title.textContent),
      status: stringValue(card.badge.textContent) || "Captured",
      lines: lines.slice(0, 16),
    };
  }

  async function handleDesktopBranch(command, args) {
    const tokens = args.trim() ? args.trim().split(/\s+/) : [];
    const action = tokens[0] ? tokens[0].toLowerCase() : "save";
    if (action === "list") {
      if (tokens.length !== 1) {
        renderLocalInspectorError(command, "Use /branch list without additional arguments.");
        return;
      }
      renderDesktopBranchList(command);
      return;
    }
    if (action === "switch" || action === "delete") {
      if (tokens.length !== 2) {
        renderLocalInspectorError(command, `Use /branch ${action} <checkpoint-name>.`);
        return;
      }
      const name = tokens[1];
      const checkpoint = state.branches.get(name);
      if (!checkpoint) {
        renderLocalInspectorError(command, `No process-local checkpoint is named “${truncate(name, 64)}”. Use /branch list.`);
        return;
      }
      if (action === "delete") {
        state.branches.delete(name);
        const card = makeLocalInspectorCard(
          command,
          "Checkpoint deleted",
          `Removed process-local checkpoint “${name}”. The underlying run was not deleted.`,
        );
        presentLocalInspector(command, card, `Checkpoint ${name} deleted.`);
        return;
      }
      clearLocalCommandInput();
      await selectRun(checkpoint.runId);
      announce(`Switched to checkpoint ${name}.`);
      return;
    }
    if (!state.activeRun || !stringValue(state.activeRun.id)) {
      renderLocalInspectorError(command, "Open a completed desktop run before saving a checkpoint.", { preserveRun: false });
      return;
    }
    if (tokens.length > 1) {
      renderLocalInspectorError(command, "Checkpoint names cannot contain spaces. Use letters, numbers, underscore, or hyphen.");
      return;
    }
    let name = tokens[0] || nextDesktopBranchName();
    if (!LOCAL_BRANCH_NAME.test(name) || ["list", "switch", "delete"].includes(name.toLowerCase())) {
      renderLocalInspectorError(command, "Checkpoint names must be 1–64 letters, numbers, underscores, or hyphens, and cannot be a branch action.");
      return;
    }
    if (state.branches.has(name)) {
      renderLocalInspectorError(command, `Checkpoint “${name}” already exists; choose another name or delete it first.`);
      return;
    }
    if (state.branches.size >= MAX_LOCAL_BRANCHES) {
      renderLocalInspectorError(command, `This window already has ${MAX_LOCAL_BRANCHES} checkpoints; delete one before adding another.`);
      return;
    }
    const checkpoint = {
      name,
      runId: stringValue(state.activeRun.id),
      prompt: stringValue(state.activeRun.prompt),
      createdAt: new Date().toISOString(),
    };
    state.branches.set(name, checkpoint);
    const card = makeLocalInspectorCard(
      command,
      "Process-local checkpoint saved",
      `“${name}” points to run ${checkpoint.runId}. It restores that run with /branch switch ${name} and is discarded when the app closes.`,
    );
    presentLocalInspector(command, card, `Checkpoint ${name} saved.`);
  }

  function nextDesktopBranchName() {
    let name;
    do {
      name = `branch-${state.nextBranchNumber}`;
      state.nextBranchNumber += 1;
    } while (state.branches.has(name));
    return name;
  }

  function renderDesktopBranchList(command) {
    const checkpoints = [...state.branches.values()].sort(
      (left, right) => timestampOf(right.createdAt) - timestampOf(left.createdAt),
    );
    const card = makeLocalInspectorCard(
      command,
      "Process-local checkpoints",
      "These checkpoints store run IDs, not full TUI transcript snapshots. They live only for this native app window.",
    );
    const list = create("div", { className: "local-inspector-list" });
    for (const checkpoint of checkpoints) {
      const row = create("div", { className: "local-inspector-row" });
      const copy = create("div", { className: "local-inspector-row-copy" });
      copy.append(
        create("strong", { text: checkpoint.name }),
        create("span", { text: historyTitle(checkpoint.prompt) }),
        create("code", { text: checkpoint.runId }),
      );
      const button = create("button", {
        className: "local-row-action",
        text: "Switch",
        attrs: { type: "button", "aria-label": `Switch to checkpoint ${checkpoint.name}` },
      });
      button.addEventListener("click", () => void selectRun(checkpoint.runId));
      row.append(copy, button);
      list.append(row);
    }
    if (!checkpoints.length) {
      list.append(create("p", {
        className: "command-empty-state",
        text: "No process-local checkpoints yet. Use /branch <name> while a run is open.",
      }));
    }
    card.append(list);
    presentLocalInspector(command, card, checkpoints.length ? "Checkpoints listed." : "No checkpoints found.");
  }

  function prepareCommandView(command, status = "executing", label = "Running command") {
    clearEphemeralSecret();
    const safeCommand = redactCommandForDisplay(command);
    ++state.loadToken;
    closeRunTransport();
    state.activeRun = null;
    state.chain = null;
    state.cards.clear();
    state.seenEventKeys.clear();
    state.currentExecutionStep = null;
    dom.chainButton.hidden = true;
    dom.cancelButton.hidden = true;
    showRunShell(safeCommand);
    dom.sessionTranscript.replaceChildren();
    dom.eventTimeline.replaceChildren();
    setRunHeader(`Command · ${historyTitle(safeCommand)}`, status, label);
    renderHistory();
    syncComposerState();
    scrollConversation(true);
  }

  async function executeBackendCommand(command, confirmationToken = "", transcript = null) {
    const requestedCommand = stringValue(command).trim();
    state.commandSubmitting = true;
    state.pendingConfirmation = null;
    prepareCommandView(requestedCommand);
    dom.eventTimeline.replaceChildren(makeStateCard(
      "Running Maestro command",
      "Waiting for the command result…",
      "loading",
      "command-state-card",
    ));
    syncComposerState();

    const body = { command: requestedCommand };
    if (confirmationToken) {
      body.confirmation_token = confirmationToken;
    }
    try {
      const submitted = await apiFetch(`${API_ROOT}/command-jobs`, {
        method: "POST",
        body: JSON.stringify(body),
      });
      if (!isRecord(submitted) || !stringValue(submitted.id)) {
        throw new ApiError("The command service created no usable job.");
      }
      state.activeCommandJob = submitted;
      dom.cancelButton.hidden = false;
      dom.cancelButton.disabled = false;
      setRunHeader(`Command · ${historyTitle(redactCommandForDisplay(requestedCommand))}`, "executing", "Running in background");
      const terminal = await waitForCommandJob(stringValue(submitted.id));
      const transcriptPayload = await apiFetch(
        `${API_ROOT}/command-jobs/${encodeURIComponent(submitted.id)}/result`,
        { method: "POST", body: JSON.stringify({}) },
      );
      state.activeCommandJob = null;
      dom.cancelButton.hidden = true;
      state.commandSubmitting = false;
      if (terminal.status === "cancelled" || transcriptPayload.status === "cancelled") {
        renderCommandResult(
          { ok: false, stderr: "Command cancelled before completion." },
          requestedCommand,
        );
        syncComposerState();
        return;
      }
      if (!isRecord(transcriptPayload.result)) {
        const errorRecord = isRecord(transcriptPayload.error) ? transcriptPayload.error : {};
        throw new ApiError(
          firstText(errorRecord, ["message"]) || "The background command failed.",
          500,
          { error: errorRecord },
        );
      }
      const dispatch = transcriptPayload.result;
      const payload = isRecord(dispatch.payload) ? dispatch.payload : {};
      const status = Number(dispatch.http_status || 500);
      if (status < 200 || status >= 300) {
        throw new ApiError(extractApiError(payload) || `Command failed (${status}).`, status, payload);
      }
      if (transcriptPayload.cancellation_requested === true) {
        const warning = "Cancellation arrived after this non-cooperative command had already started; its result is shown honestly.";
        payload.stderr = commandPayloadText(payload.stderr)
          ? `${commandPayloadText(payload.stderr)}\n${warning}`
          : warning;
      }
      renderCommandResult(payload, requestedCommand);
    } catch (error) {
      const challenge = normalizeConfirmationChallenge(error, requestedCommand, transcript);
      state.activeCommandJob = null;
      dom.cancelButton.hidden = true;
      state.commandSubmitting = false;
      if (challenge) {
        state.pendingConfirmation = challenge;
        renderCommandConfirmationWaiting(challenge);
        openCommandConfirmationDialog(challenge);
      } else {
        const payload = error instanceof ApiError && isRecord(error.payload)
          ? { ...error.payload, ok: false }
          : { ok: false };
        if (!commandPayloadText(payload.stderr)) {
          payload.stderr = readError(error, "The command failed.");
        }
        renderCommandResult(payload, requestedCommand);
      }
    }
    syncComposerState();
  }

  async function waitForCommandJob(jobId) {
    const terminal = new Set(["completed", "failed", "cancelled"]);
    while (true) {
      const job = await apiFetch(`${API_ROOT}/command-jobs/${encodeURIComponent(jobId)}`);
      if (!isRecord(job)) {
        throw new ApiError("The command service returned an invalid job.");
      }
      state.activeCommandJob = job;
      if (job.status === "cancelling") {
        setRunHeader("Command", "cancelling", "Cancellation requested");
        dom.cancelButton.disabled = true;
      }
      if (terminal.has(job.status)) {
        return job;
      }
      await new Promise((resolve) => window.setTimeout(resolve, 180));
    }
  }

  function normalizeConfirmationChallenge(error, fallbackCommand, transcript = null) {
    if (!(error instanceof ApiError) || error.status !== 409 || !isRecord(error.payload)) {
      return null;
    }
    const payload = error.payload;
    const confirmation = isRecord(payload.confirmation) ? payload.confirmation : {};
    const errorRecord = isRecord(payload.error) ? payload.error : {};
    const token = firstText(confirmation, ["token", "confirmation_token"])
      || firstText(payload, ["confirmation_token", "token"]);
    const required = payload.confirmation_required === true
      || errorRecord.code === "confirmation_required"
      || Boolean(token);
    if (!required || !token) {
      return null;
    }
    const command = extractCommandDisplay(payload, fallbackCommand);
    const risk = firstText(payload, ["risk"])
      || firstText(confirmation, ["risk"])
      || (isRecord(payload.command) ? firstText(payload.command, ["risk"]) : "")
      || "side_effect";
    const reason = firstText(payload, ["reason"])
      || firstText(confirmation, ["reason"])
      || firstText(errorRecord, ["message"])
      || "This command can change state or call an external provider.";
    return {
      command: fallbackCommand,
      displayCommand: command,
      token,
      risk,
      reason,
      effects: normalizeCommandEffects(
        payload.effects
          || confirmation.effects
          || (isRecord(payload.command) ? payload.command.effects : null),
      ),
      transcript: Array.isArray(transcript) ? transcript : null,
    };
  }

  function normalizeCommandEffects(value) {
    if (Array.isArray(value)) {
      return value.slice(0, 12).map((item) => truncate(commandPayloadText(item), 800)).filter(Boolean);
    }
    if (typeof value === "string" && value.trim()) {
      return [truncate(value.trim(), 800)];
    }
    if (isRecord(value)) {
      return Object.entries(value).slice(0, 12).map(([key, item]) => {
        return `${humanize(key)}: ${truncate(commandPayloadText(item), 720)}`;
      });
    }
    return [];
  }

  function renderCommandConfirmationWaiting(challenge) {
    removeById("command-state-card");
    dom.userPrompt.textContent = challenge.displayCommand;
    const card = create("article", { className: "command-result-card is-warning", attrs: { id: "command-state-card" } });
    const header = create("div", { className: "command-result-header" });
    header.append(
      create("span", { className: "command-result-icon", text: "!", attrs: { "aria-hidden": "true" } }),
      create("h3", { text: "Confirmation required" }),
      create("span", { className: "command-exit-badge", text: commandRiskLabel(challenge.risk) }),
    );
    card.append(header, create("p", { className: "command-result-message", text: challenge.reason }));
    appendTimeline(card);
    setRunHeader(`Command · ${historyTitle(challenge.displayCommand)}`, "waiting_for_input", "Confirmation required");
    syncComposerState();
  }

  function openCommandConfirmationDialog(challenge) {
    const settingsSave = challenge.kind === "settings";
    dom.commandConfirmationTitle.textContent = settingsSave
      ? "Save these settings?"
      : "Allow this command?";
    dom.commandConfirmationRisk.textContent = commandRiskLabel(challenge.risk);
    dom.commandConfirmationRisk.dataset.tone = commandRiskTone(challenge.risk);
    dom.commandConfirmationReason.textContent = challenge.reason;
    dom.commandConfirmationCommand.textContent = challenge.displayCommand;
    const effects = challenge.effects.map((effect) => create("li", { text: effect }));
    dom.commandConfirmationEffectsList.replaceChildren(...effects);
    dom.commandConfirmationEffects.hidden = effects.length === 0;
    dom.commandConfirmationCancel.disabled = false;
    dom.commandConfirmationConfirm.disabled = false;
    dom.commandConfirmationConfirm.textContent = settingsSave ? "Confirm and save" : "Confirm and run";
    if (dom.commandDialog.open) {
      closeCommandDialog({ restoreFocus: false });
    }
    if (typeof dom.commandConfirmationDialog.showModal === "function") {
      if (!dom.commandConfirmationDialog.open) {
        dom.commandConfirmationDialog.showModal();
      }
    } else {
      dom.commandConfirmationDialog.setAttribute("open", "");
    }
    requestAnimationFrame(() => dom.commandConfirmationCancel.focus());
    announce("Command confirmation required. Review the risk before continuing.");
  }

  function closeCommandConfirmationDialog() {
    if (typeof dom.commandConfirmationDialog.close === "function" && dom.commandConfirmationDialog.open) {
      dom.commandConfirmationDialog.close();
    } else {
      dom.commandConfirmationDialog.removeAttribute("open");
    }
  }

  function declineCommandConfirmation() {
    const pending = state.pendingConfirmation;
    state.pendingConfirmation = null;
    closeCommandConfirmationDialog();
    if (pending && pending.kind === "settings") {
      setSettingsBusy(false, "Save cancelled. No settings were changed.", "warning");
      dom.settingsSave.focus();
      announce("Settings were not changed.");
      return;
    }
    if (pending) {
      removeById("command-state-card");
      const card = create("article", { className: "command-result-card is-warning" });
      card.append(
        create("h3", { text: "Command not run" }),
        create("p", { className: "command-result-message", text: "Confirmation was declined. No command was executed." }),
      );
      appendTimeline(card);
      setRunHeader(`Command · ${historyTitle(pending.displayCommand)}`, "idle", "Not run");
    }
    syncComposerState();
    dom.promptInput.focus();
    announce("Command not run.");
  }

  async function confirmPendingCommand() {
    const pending = state.pendingConfirmation;
    if (!pending) {
      closeCommandConfirmationDialog();
      return;
    }
    dom.commandConfirmationCancel.disabled = true;
    dom.commandConfirmationConfirm.disabled = true;
    state.pendingConfirmation = null;
    closeCommandConfirmationDialog();
    if (pending.kind === "settings") {
      await saveSettings(pending.settingsPayload, pending.token);
      return;
    }
    await executeBackendCommand(pending.command, pending.token, pending.transcript);
  }

  function consumeMemoryEditorDescriptor(payload) {
    if (!isRecord(payload) || stringValue(payload.action) !== "edit_memory") {
      return null;
    }

    const contentIsText = typeof payload.content === "string";
    const content = contentIsText ? payload.content : "";
    // The document is a private transport field. Remove it from the command
    // result object before any generic renderer or diagnostic projection can
    // inspect the payload.
    payload.content = "";
    delete payload.content;

    const advertisedLimit = finiteNumber(payload.max_size_bytes);
    const maxBytes = advertisedLimit !== null
      && Number.isInteger(advertisedLimit)
      && advertisedLimit > 0
      && advertisedLimit <= MAX_MEMORY_EDITOR_BYTES
      ? advertisedLimit
      : MAX_MEMORY_EDITOR_BYTES;
    const baseSha256 = stringValue(payload.base_sha256).toLowerCase();
    const safeRevision = /^[0-9a-f]{64}$/.test(baseSha256);
    const serverBlocked = payload.content_redacted === true
      || payload.editable !== true
      || payload.save_available !== true;
    const contentBytes = utf8ByteLength(content);
    const oversized = contentBytes > maxBytes;
    const malformed = !contentIsText
      || typeof payload.content_redacted !== "boolean"
      || stringValue(payload.encoding).toLowerCase() !== "utf-8"
      || stringValue(payload.save_encoding) !== "base64url-utf8-unpadded";
    const blocked = serverBlocked || !safeRevision || oversized || malformed;
    let blockedMessage = "";
    if (payload.content_redacted === true) {
      blockedMessage = "CARE.md contains credential-like content. For safety, its contents were not loaded into the native editor.";
    } else if (serverBlocked) {
      blockedMessage = "Maestro marked this CARE.md document as read-only, so its contents were not loaded into the editor.";
    } else if (oversized) {
      blockedMessage = `CARE.md exceeds the ${maxBytes.toLocaleString()}-byte native editor limit.`;
    } else if (!safeRevision || malformed) {
      blockedMessage = "Maestro returned an invalid editor descriptor. Reload CARE.md before editing.";
    }

    return {
      baseSha256: safeRevision ? baseSha256 : "",
      content: blocked ? "" : content,
      maxBytes,
      exists: payload.exists === true,
      saveAvailable: !blocked,
      blockedMessage,
    };
  }

  function openMemoryEditor(descriptor) {
    closeMemoryEditor({ restoreFocus: false });
    state.memoryEditor = {
      baseSha256: descriptor.baseSha256,
      maxBytes: descriptor.maxBytes,
      saveAvailable: descriptor.saveAvailable,
    };
    dom.memoryEditorContent.value = descriptor.content;
    dom.memoryEditorContent.disabled = !descriptor.saveAvailable;
    dom.memoryEditorWarning.textContent = descriptor.blockedMessage;
    dom.memoryEditorWarning.hidden = !descriptor.blockedMessage;
    dom.memoryEditorStatus.textContent = descriptor.saveAvailable
      ? (descriptor.exists ? "Ready to edit" : "New document")
      : "Editing unavailable";
    dom.memoryEditorStatus.dataset.tone = descriptor.saveAvailable ? "ready" : "error";
    updateMemoryEditorState();

    if (typeof dom.memoryEditorDialog.showModal === "function") {
      if (!dom.memoryEditorDialog.open) {
        dom.memoryEditorDialog.showModal();
      }
    } else {
      dom.memoryEditorDialog.setAttribute("open", "");
    }
    requestAnimationFrame(() => {
      if (descriptor.saveAvailable) {
        dom.memoryEditorContent.focus();
      } else {
        dom.memoryEditorClose.focus();
      }
    });
    announce(descriptor.saveAvailable ? "CARE.md editor opened." : "CARE.md cannot be edited safely.");
  }

  function closeMemoryEditor(options = {}) {
    const restoreFocus = options.restoreFocus !== false;
    if (typeof dom.memoryEditorDialog.close === "function" && dom.memoryEditorDialog.open) {
      dom.memoryEditorDialog.close();
    } else {
      dom.memoryEditorDialog.removeAttribute("open");
    }
    dom.memoryEditorContent.value = "";
    dom.memoryEditorContent.disabled = true;
    dom.memoryEditorContent.setCustomValidity("");
    dom.memoryEditorWarning.textContent = "";
    dom.memoryEditorWarning.hidden = true;
    dom.memoryEditorMeta.textContent = "UTF-8 · 0 bytes";
    dom.memoryEditorStatus.textContent = "";
    delete dom.memoryEditorStatus.dataset.tone;
    dom.memoryEditorSave.disabled = true;
    state.memoryEditor = null;
    if (restoreFocus) {
      dom.promptInput.focus();
    }
  }

  function updateMemoryEditorState() {
    const editor = state.memoryEditor;
    if (!editor) {
      dom.memoryEditorSave.disabled = true;
      return;
    }
    const sizeBytes = utf8ByteLength(dom.memoryEditorContent.value);
    const oversized = sizeBytes > editor.maxBytes;
    dom.memoryEditorMeta.textContent = `UTF-8 · ${sizeBytes.toLocaleString()} / ${editor.maxBytes.toLocaleString()} bytes`;
    dom.memoryEditorContent.setCustomValidity(oversized ? "CARE.md is too large to save." : "");
    dom.memoryEditorSave.disabled = !editor.saveAvailable || oversized;
    if (oversized) {
      dom.memoryEditorStatus.textContent = "Reduce the document before saving";
      dom.memoryEditorStatus.dataset.tone = "error";
    } else if (editor.saveAvailable) {
      dom.memoryEditorStatus.textContent = "Ready to review";
      dom.memoryEditorStatus.dataset.tone = "ready";
    }
  }

  async function saveMemoryEditor(event) {
    event.preventDefault();
    const editor = state.memoryEditor;
    if (!editor || !editor.saveAvailable || !/^[0-9a-f]{64}$/.test(editor.baseSha256)) {
      updateMemoryEditorState();
      announce("CARE.md cannot be saved from this editor state.");
      return;
    }
    const content = dom.memoryEditorContent.value;
    if (utf8ByteLength(content) > editor.maxBytes) {
      updateMemoryEditorState();
      dom.memoryEditorContent.reportValidity();
      return;
    }
    const encoded = utf8ToBase64Url(content);
    const command = `/memory edit save ${editor.baseSha256} '${encoded}'`;
    closeMemoryEditor({ restoreFocus: false });
    await executeBackendCommand(command);
  }

  function utf8ByteLength(value) {
    return new TextEncoder().encode(stringValue(value)).byteLength;
  }

  function utf8ToBase64Url(value) {
    const bytes = new TextEncoder().encode(stringValue(value));
    let binary = "";
    const chunkSize = 0x4000;
    for (let offset = 0; offset < bytes.length; offset += chunkSize) {
      binary += String.fromCharCode(...bytes.subarray(offset, offset + chunkSize));
    }
    return window.btoa(binary)
      .replace(/\+/g, "-")
      .replace(/\//g, "_")
      .replace(/=+$/g, "");
  }

  function renderCommandResult(payload, fallbackCommand) {
    removeById("command-state-card");
    const record = isRecord(payload) ? payload : {};
    const command = extractCommandDisplay(record, fallbackCommand);
    const exitCode = finiteNumber(record.exit_code ?? record.exitCode ?? record.code);
    const ok = typeof record.ok === "boolean" ? record.ok : exitCode === null || exitCode === 0;
    const stdout = commandPayloadText(record.stdout ?? record.output ?? record.result);
    let stderr = commandPayloadText(record.stderr ?? record.error_output);
    const structured = isRecord(record.payload) ? record.payload : null;
    const memoryEditorDescriptor = consumeMemoryEditorDescriptor(structured);
    const memoryEditor = ok ? memoryEditorDescriptor : null;
    const commandName = isRecord(record.command)
      ? firstText(record.command, ["name", "canonical_name", "command_id"])
      : "";
    if (!stderr && isRecord(record.error)) {
      stderr = readValueError(record.error, "");
    }

    dom.userPrompt.textContent = command;
    const card = create("article", {
      className: `command-result-card ${ok ? "is-complete" : "is-error"}`,
      attrs: { id: "command-result-card" },
    });
    const header = create("div", { className: "command-result-header" });
    header.append(
      create("span", { className: "command-result-icon", text: ">_", attrs: { "aria-hidden": "true" } }),
      create("h3", { text: ok ? "Command completed" : "Command failed" }),
      create("span", {
        className: "command-exit-badge",
        text: exitCode === null ? (ok ? "Done" : "Error") : `Exit ${exitCode}`,
      }),
    );
    card.append(header, create("code", { className: "command-result-invocation", text: command }));
    if (stdout) {
      card.append(commandOutputSection("Standard output", stdout, "stdout"));
    }
    if (stderr) {
      card.append(commandOutputSection("Standard error", stderr, "stderr"));
    }
    const ephemeral = renderEphemeralSecret(record);
    if (ephemeral) {
      card.append(ephemeral);
    }
    if (structured && commandName === "/imgpreview") {
      const preview = workspaceImagePreview(structured);
      if (preview) {
        card.append(preview);
      }
    } else if (memoryEditor) {
      card.append(create("p", {
        className: "command-result-message",
        text: memoryEditor.saveAvailable
          ? "CARE.md was opened in the private native editor. Its contents are not shown in this command result."
          : "CARE.md was not loaded into the editor because the document could not be round-tripped safely.",
      }));
    } else if (structured && commandName !== "/deploy" && hasUsefulValue(structured)) {
      const details = create("details", { className: "command-structured-details" });
      details.append(
        create("summary", { text: commandName === "/revise" ? "Revision details" : "Structured details" }),
        create("pre", {
          className: "result-preview",
          text: truncate(safeJson(redactStructuredValue(structured), 2), MAX_COMMAND_OUTPUT_CHARS),
        }),
      );
      card.append(details);
    }
    const revisionActions = revisionDraftActions(structured);
    if (revisionActions) {
      card.append(revisionActions);
    }
    if (!stdout && !stderr) {
      card.append(create("p", {
        className: "command-result-message",
        text: ok ? "The command completed without output." : "The command did not return an error message.",
      }));
    }
    const meta = [];
    const duration = finiteNumber(record.duration_ms ?? record.durationMs);
    if (duration !== null) {
      meta.push(`${duration} ms`);
    }
    const risk = isRecord(record.command) ? firstText(record.command, ["risk"]) : firstText(record, ["risk"]);
    if (risk) {
      meta.push(commandRiskLabel(risk));
    }
    if (meta.length) {
      card.append(create("p", { className: "command-result-meta", text: meta.join(" · ") }));
    }
    dom.eventTimeline.replaceChildren(card);
    setRunHeader(`Command · ${historyTitle(command)}`, ok ? "completed" : "failed", ok ? "Command completed" : "Command failed");
    syncComposerState();
    scrollConversation(true);
    if (memoryEditor) {
      openMemoryEditor(memoryEditor);
    } else {
      announce(ok ? "Command completed." : "Command failed.");
    }
  }

  function commandOutputSection(title, value, tone) {
    const section = create("section", { className: `command-output-section command-output-${tone}` });
    section.append(
      create("h4", { text: title }),
      create("pre", { text: truncate(value, MAX_COMMAND_OUTPUT_CHARS) }),
    );
    return section;
  }

  function revisionDraftActions(payload) {
    if (
      !isRecord(payload)
      || stringValue(payload.action) !== "preview_revision"
      || payload.changed !== true
    ) {
      return null;
    }
    const draftId = stringValue(payload.draft_id);
    if (!/^[A-Za-z0-9_-]{8,128}$/.test(draftId)) {
      return null;
    }
    const actions = create("div", { className: "local-inspector-actions" });
    const save = create("button", {
      className: "state-action-button",
      text: "Review & save revision",
      attrs: { type: "button" },
    });
    const discard = create("button", {
      className: "secondary-button",
      text: "Discard draft",
      attrs: { type: "button" },
    });
    const run = async (verb) => {
      save.disabled = true;
      discard.disabled = true;
      await executeBackendCommand(`/revise ${verb} ${draftId}`);
    };
    save.addEventListener("click", () => void run("save"));
    discard.addEventListener("click", () => void run("discard"));
    actions.append(save, discard);
    return actions;
  }

  function clearEphemeralSecret() {
    if (typeof state.ephemeralSecretClear === "function") {
      state.ephemeralSecretClear();
    }
    state.ephemeralSecretClear = null;
  }

  function renderEphemeralSecret(record) {
    const envelope = isRecord(record.ephemeral_secret) ? record.ephemeral_secret : null;
    let secret = envelope ? stringValue(envelope.value) : "";
    if (!secret || secret.length > 256 || /[\x00-\x20\x7f]/.test(secret)) {
      return null;
    }
    record.ephemeral_secret = null;
    envelope.value = "";
    const section = create("section", { className: "command-ephemeral-secret" });
    const heading = create("h4", { text: "New agent credential" });
    const value = create("code", { text: "••••••••••••••••••••" });
    const note = create("p", {
      text: "Copy now. Maestro clears this value from the page after copying, after 60 seconds, or when you leave this result.",
    });
    const copy = create("button", {
      className: "state-action-button",
      text: "Copy credential",
      attrs: { type: "button" },
    });
    let cleared = false;
    const clear = () => {
      if (cleared) {
        return;
      }
      cleared = true;
      secret = "";
      value.textContent = "Cleared";
      copy.disabled = true;
      copy.textContent = "Credential cleared";
    };
    state.ephemeralSecretClear = clear;
    window.setTimeout(() => {
      if (state.ephemeralSecretClear === clear) {
        clear();
        state.ephemeralSecretClear = null;
      }
    }, Math.min(60, Math.max(5, numberOr(envelope.expires_in_seconds, 60))) * 1000);
    copy.addEventListener("click", async () => {
      if (!secret) {
        return;
      }
      try {
        await navigator.clipboard.writeText(secret);
        clear();
        state.ephemeralSecretClear = null;
        note.textContent = "Copied to the system clipboard and cleared from this page.";
        announce("Credential copied and cleared from the page.");
      } catch (_error) {
        note.textContent = "Clipboard access was denied. The credential remains hidden and will still be cleared automatically.";
        announce("The operating system denied clipboard access.");
      }
    });
    section.append(heading, value, copy, note);
    return section;
  }

  function workspaceImagePreview(payload) {
    const route = stringValue(payload.route);
    const mime = stringValue(payload.mime_type).toLowerCase();
    if (!route.startsWith("/api/workspace-images/") || !/^image\/(?:png|jpeg|gif|webp|bmp)$/.test(mime)) {
      return null;
    }
    const figure = create("figure", { className: "command-image-preview" });
    const image = create("img", {
      attrs: {
        src: route,
        alt: stringValue(payload.display_name || payload.relative_path || "Workspace image"),
        loading: "lazy",
        decoding: "async",
      },
    });
    figure.append(image, create("figcaption", {
      text: `${stringValue(payload.relative_path)} · ${numberOr(payload.size_bytes, 0)} bytes`,
    }));
    return figure;
  }

  function redactStructuredValue(value, depth = 0) {
    if (depth > 8) {
      return "[truncated]";
    }
    if (Array.isArray(value)) {
      return value.slice(0, 500).map((item) => redactStructuredValue(item, depth + 1));
    }
    if (!isRecord(value)) {
      return value;
    }
    const projected = {};
    for (const [key, item] of Object.entries(value).slice(0, 500)) {
      if (/(?:api.?key|authorization|password|passwd|secret|token|credential)/i.test(key)) {
        projected[key] = "[redacted]";
      } else {
        projected[key] = redactStructuredValue(item, depth + 1);
      }
    }
    return projected;
  }

  function extractCommandDisplay(payload, fallbackCommand) {
    let display = firstText(payload, ["display_command", "invocation"]);
    if (!display && typeof payload.command === "string") {
      display = payload.command;
    } else if (!display && isRecord(payload.command)) {
      display = firstText(payload.command, ["display_command", "invocation", "command", "name"]);
    }
    return redactCommandForDisplay(display || fallbackCommand || "maestro");
  }

  function redactCommandForDisplay(value) {
    const command = stringValue(value);
    const privateMemorySave = command.match(
      /^(\s*(?:(?:care|maestro)\s+)?\/memory\s+edit\s+save)\b/i,
    );
    if (privateMemorySave) {
      return `${privateMemorySave[1]} [private revision] [private content]`;
    }
    const secret = "(?:api[-_]?key|api[-_]?token|access[-_]?token|refresh[-_]?token|client[-_]?secret|password|passwd|secret|token)";
    return command
      .replace(new RegExp(`(--${secret})=(?:"[^"]*"|'[^']*'|\\S+)`, "gi"), "$1=[redacted]")
      .replace(new RegExp(`(--${secret})(\\s+)(?:"[^"]*"|'[^']*'|\\S+)`, "gi"), "$1$2[redacted]");
  }

  function commandPayloadText(value) {
    if (value === null || value === undefined) {
      return "";
    }
    if (typeof value === "string") {
      return value;
    }
    return valueToDisplay(value);
  }

  function renderLocalCommandResult(command, message, ok = true) {
    dom.promptInput.value = "";
    deactivateMultiLineMode();
    resizePrompt();
    const safeCommand = redactCommandForDisplay(command);
    prepareCommandView(safeCommand, ok ? "completed" : "failed", ok ? "Updated" : "Command error");
    const card = create("article", { className: `command-result-card ${ok ? "is-complete" : "is-error"}` });
    card.append(
      create("h3", { text: ok ? "Desktop command applied" : "Desktop command rejected" }),
      create("code", { className: "command-result-invocation", text: safeCommand }),
      create("p", { className: "command-result-message", text: message }),
    );
    dom.eventTimeline.replaceChildren(card);
    syncComposerState();
    announce(message);
  }

  function renderCommandHelp(query) {
    const command = query ? `/help ${query}` : "/help";
    prepareCommandView(command, "completed", "Command help");
    const terms = stringValue(query).toLowerCase().split(/\s+/).filter(Boolean);
    const entries = state.commandEntries.filter((entry) => {
      const haystack = `${entry.invocation} ${entry.description}`.toLowerCase();
      return terms.every((term) => haystack.includes(term));
    }).slice(0, MAX_PALETTE_RESULTS);
    const card = create("article", { className: "command-help-card" });
    card.append(
      create("div", { className: "command-help-heading", text: query ? `Help matching “${query}”` : "Maestro commands" }),
      create("p", {
        className: "command-result-message",
        text: state.commandManifestLoaded
          ? "Local interface commands and the current CLI manifest. Use Command or Control + K to search."
          : "Local interface commands. The CLI manifest is currently unavailable.",
      }),
    );
    const list = create("div", { className: "command-help-list" });
    for (const entry of entries) {
      const row = create("div", { className: "command-help-entry" });
      row.append(
        create("code", { text: entry.invocation }),
        create("span", { text: entry.description }),
      );
      list.append(row);
    }
    if (!entries.length) {
      list.append(create("p", { className: "command-empty-state", text: "No commands match this help query." }));
    }
    card.append(list);
    dom.eventTimeline.replaceChildren(card);
    syncComposerState();
    scrollConversation(true);
    announce("Command help opened.");
  }

  async function closeNativeApp(command = "/quit") {
    prepareCommandView(command, "cancelling", "Closing application");
    const nativeApi = window.pywebview && window.pywebview.api;
    if (!nativeApi || typeof nativeApi.close_app !== "function") {
      renderLocalCommandResult(
        command,
        "This is the browser fallback. Close the browser tab, or launch `maestro app` to use /quit.",
        false,
      );
      return;
    }
    try {
      const closed = await nativeApi.close_app();
      if (closed !== true) {
        renderLocalCommandResult(command, "The native window could not be closed.", false);
      }
    } catch (_error) {
      renderLocalCommandResult(command, "The native window could not be closed.", false);
    }
  }

  async function startRun(event) {
    event.preventDefault();
    const prompt = dom.promptInput.value.trim();
    if (!prompt || state.submitting || state.commandSubmitting) {
      if (!prompt) {
        dom.promptInput.focus();
      }
      return;
    }

    if (state.settingsHydration) {
      await state.settingsHydration;
    }
    if (state.submitting || state.commandSubmitting) {
      return;
    }

    const commandRequest = parseComposerCommand(prompt);
    if (commandRequest) {
      await dispatchComposerCommand(commandRequest);
      return;
    }

    if (state.activeRun && isRunActive(state.activeRun.status)) {
      showComposerAlert("Cancel the active run or start a new chat first.");
      return;
    }

    const selectedSessionMode = document.querySelector('input[name="session-mode"]:checked');
    const productionPointer = normalizeProductionPointer(state.productionPointer);
    if (
      selectedSessionMode
      && selectedSessionMode.value === "production"
      && productionPointer
    ) {
      dom.promptInput.value = "";
      deactivateMultiLineMode();
      resizePrompt();
      await executeBackendCommand(
        `/revise ${quoteCommandArgument(productionPointer.entity_id)} ${quoteCommandArgument(prompt)}`,
      );
      return;
    }

    ++state.loadToken;
    closeRunTransport();
    hideComposerAlert();
    state.submitting = true;
    state.activeRun = null;
    state.chain = null;
    state.cards.clear();
    state.seenEventKeys.clear();
    state.currentExecutionStep = null;
    deactivateMultiLineMode();
    showRunShell(prompt);
    dom.eventTimeline.replaceChildren(makeStateCard(
      "Creating run",
      "Sending your request to Maestro…",
      "loading",
      "transport-state-card",
    ));
    dom.chainButton.hidden = true;
    setRunHeader(historyTitle(prompt), "pending", "Creating");
    syncComposerState();
    scrollConversation(true);

    const selectedMode = document.querySelector('input[name="mode"]:checked');
    const request = {
      prompt,
      mode: selectedMode ? selectedMode.value : "fast",
      session_mode: selectedSessionMode ? selectedSessionMode.value : "interactive",
      execute: dom.executeInput.checked,
    };

    try {
      const run = await apiFetch(`${API_ROOT}/runs`, {
        method: "POST",
        body: JSON.stringify(request),
      });
      if (!isRecord(run) || !run.id) {
        throw new ApiError("The server created no usable run.");
      }
      state.submitting = false;
      dom.promptInput.value = "";
      resizePrompt();
      upsertRun(run);
      renderRun(run, { connect: true });
      setServiceState("connected", "Connected");
      announce("Run started.");
    } catch (error) {
      state.submitting = false;
      state.activeRun = null;
      const message = readError(error, "Maestro could not start this run.");
      removeById("transport-state-card");
      renderRunError(message, "Could not start run");
      setRunHeader(historyTitle(prompt), "failed", "Start failed");
      setServiceState("offline", "API unavailable");
      showComposerAlert(message);
      syncComposerState();
      announce("Run could not be started.");
    }
  }

  async function cancelRun() {
    if (state.activeCommandJob && stringValue(state.activeCommandJob.id)) {
      const jobId = stringValue(state.activeCommandJob.id);
      dom.cancelButton.disabled = true;
      try {
        const job = await apiFetch(
          `${API_ROOT}/command-jobs/${encodeURIComponent(jobId)}/cancel`,
          { method: "POST", body: JSON.stringify({}) },
        );
        if (state.activeCommandJob && stringValue(state.activeCommandJob.id) === jobId) {
          state.activeCommandJob = job;
        }
        setRunHeader("Command", "cancelling", "Cancellation requested");
        announce("Command cancellation requested.");
      } catch (error) {
        dom.cancelButton.disabled = false;
        showComposerAlert(readError(error, "Could not cancel the command."));
      }
      return;
    }
    if (!state.activeRun || !state.activeRun.id || state.cancelling) {
      return;
    }
    const runId = stringValue(state.activeRun.id);
    if (!isRunActive(state.activeRun.status)) {
      return;
    }

    state.cancelling = true;
    state.activeRun.status = "cancelling";
    setRunStatus("cancelling");
    syncComposerState();
    hideComposerAlert();

    try {
      const run = await apiFetch(`${API_ROOT}/runs/${encodeURIComponent(runId)}/cancel`, {
        method: "POST",
        body: "{}",
      });
      if (state.activeRun && String(state.activeRun.id) === runId && isRecord(run)) {
        state.activeRun = { ...state.activeRun, ...run };
        upsertRun(state.activeRun);
        if (normalizeStatus(state.activeRun.status) === "cancelled") {
          finishRun("cancelled", state.activeRun.result, state.activeRun.error);
        } else {
          setRunStatus(state.activeRun.status);
        }
      }
    } catch (error) {
      if (state.activeRun && String(state.activeRun.id) === runId) {
        state.activeRun.status = "running";
        setRunStatus("running");
      }
      showComposerAlert(readError(error, "Could not cancel the run."));
    } finally {
      state.cancelling = false;
      syncComposerState();
    }
  }

  function connectRunEvents(runId) {
    if (!runId || !state.activeRun || String(state.activeRun.id) !== runId) {
      return;
    }

    closeRunTransport();
    state.sourceRunId = runId;

    if (!("EventSource" in window)) {
      setServiceState("reconnecting", "Live events unsupported");
      startRunPolling(runId);
      return;
    }

    const source = new EventSource(`${API_ROOT}/runs/${encodeURIComponent(runId)}/events`, {
      withCredentials: true,
    });
    state.source = source;
    setServiceState("checking", "Opening live stream…");

    source.addEventListener("open", () => {
      if (state.source !== source) {
        return;
      }
      removeById("transport-state-card");
      setServiceState("connected", "Live");
    });

    for (const type of SSE_EVENT_TYPES) {
      source.addEventListener(type, (event) => consumeSseEvent(event, type, runId));
    }
    source.addEventListener("message", (event) => consumeSseEvent(event, "message", runId));

    source.addEventListener("error", () => {
      if (state.source !== source || !state.activeRun || String(state.activeRun.id) !== runId) {
        return;
      }
      if (TERMINAL_STATUSES.has(normalizeStatus(state.activeRun.status))) {
        closeRunTransport();
        setServiceState("connected", "Connected");
        return;
      }
      setServiceState("reconnecting", "Reconnecting stream…");
      if (!state.transportNoticeShown) {
        state.transportNoticeShown = true;
        announce("Live event stream interrupted. Reconnecting.");
      }
    });
  }

  function consumeSseEvent(event, typeHint, runId) {
    if (!state.activeRun || String(state.activeRun.id) !== runId) {
      return;
    }

    let envelope;
    try {
      envelope = event.data ? JSON.parse(event.data) : {};
    } catch (_error) {
      renderRunError("Maestro sent a malformed live event.", "Live event error", false);
      return;
    }

    const record = isRecord(envelope) ? envelope : { data: envelope };
    const type = stringValue(record.type || typeHint || "message");
    const payload = Object.prototype.hasOwnProperty.call(record, "data") ? record.data : record;
    const key = eventIdentity(record, event, type, payload);
    if (state.seenEventKeys.has(key)) {
      return;
    }
    state.seenEventKeys.add(key);

    removeById("transport-state-card");
    state.transportNoticeShown = false;
    setServiceState("connected", "Live");
    processRunEvent({
      type,
      data: isRecord(payload) ? payload : { value: payload },
      seq: record.seq,
      timestamp: record.timestamp,
    });
  }

  function eventIdentity(record, event, type, payload) {
    if (record.seq !== undefined && record.seq !== null) {
      return `seq:${String(record.seq)}`;
    }
    if (event.lastEventId) {
      return `id:${event.lastEventId}`;
    }
    return `fallback:${type}:${stringValue(record.timestamp)}:${safeJson(payload).slice(0, 700)}`;
  }

  function processRunEvent(event) {
    const type = event.type;
    const data = event.data || {};

    switch (type) {
      case "run.created":
        updateActiveStatus("pending");
        ensureEventCard("run-lifecycle", {
          title: "Run created",
          icon: "M",
          status: "running",
          badge: "Queued",
        });
        break;

      case "run.started": {
        updateActiveStatus("running");
        const card = ensureEventCard("run-lifecycle", {
          title: "Maestro started the run",
          icon: "M",
          status: "running",
          badge: "Running",
        });
        setCardTitle(card, "Maestro started the run");
        break;
      }

      case "generation.stage.started": {
        updateActiveStatus("generating");
        const stage = stageName(data.stage);
        ensureEventCard(`generation:${stage.key}`, {
          title: stage.label,
          icon: "G",
          status: "running",
          badge: "Generating",
        });
        break;
      }

      case "generation.stage.completed": {
        const stage = stageName(data.stage);
        const card = ensureEventCard(`generation:${stage.key}`, {
          title: stage.label,
          icon: "G",
          status: "complete",
          badge: "Complete",
        });
        setCardState(card, "complete", "Complete");
        if (hasUsefulValue(data.result)) {
          setResultPreview(card, "Stage output", data.result);
        }
        break;
      }

      case "generation.stage.error": {
        const stage = stageName(data.stage);
        const card = ensureEventCard(`generation:${stage.key}`, {
          title: stage.label,
          icon: "!",
          status: "error",
          badge: "Error",
        });
        setCardState(card, "error", "Error");
        appendCardLine(card, readValueError(data.error, "Generation stage failed."));
        break;
      }

      case "generation.chunk": {
        updateActiveStatus("generating");
        const stage = stageName(data.stage);
        const card = ensureEventCard(`generation:${stage.key}`, {
          title: stage.label,
          icon: "G",
          status: "running",
          badge: "Generating",
        });
        appendStreamText(card, stringValue(data.delta));
        break;
      }

      case "generation.progress":
      case "generation.stage.progress": {
        updateActiveStatus("generating");
        const stage = stageName(data.stage);
        const card = ensureEventCard(`generation:${stage.key}`, {
          title: stage.label,
          icon: "G",
          status: "running",
          badge: "Generating",
        });
        if (hasUsefulValue(data.artifact)) {
          setResultPreview(card, "Latest artifact", data.artifact);
        }
        break;
      }

      case "generation.retry":
      case "generation.stage.retry": {
        updateActiveStatus("generating");
        const stage = stageName(data.stage);
        const card = ensureEventCard(`generation:${stage.key}`, {
          title: stage.label,
          icon: "↻",
          status: "warning",
          badge: "Retrying",
        });
        setCardState(card, "warning", "Retrying");
        appendCardLine(
          card,
          `Attempt ${numberOr(data.attempt, "—")}: ${readValueError(data.error, "retry requested")}`,
        );
        break;
      }

      case "generation.cost": {
        const card = ensureEventCard("generation:cost", {
          title: "Generation estimate",
          icon: "$",
          status: "neutral",
          badge: "Estimate",
        });
        setSingleCardLine(card, valueToDisplay(data.estimate));
        break;
      }

      case "generation.completed": {
        const lifecycle = state.cards.get("run-lifecycle");
        if (lifecycle) {
          setCardState(lifecycle, "complete", "Started");
        }
        if (isRecord(data.chain)) {
          setChain(data.chain);
          renderChainReadyCard(data.chain);
        }
        break;
      }

      case "production.stage": {
        const stage = stageName(data.stage || "production");
        const stageState = normalizeStatus(data.state);
        const presentation = {
          running: ["running", "Running"],
          waiting_confirmation: ["running", "Confirm"],
          done: ["complete", "Complete"],
          skipped: ["warning", "Skipped"],
          failed: ["error", "Failed"],
          cancelled: ["cancelled", "Cancelled"],
        }[stageState] || ["neutral", humanize(stageState)];
        const card = ensureEventCard(`production:${stage.key}`, {
          title: `Agent · ${stage.label}`,
          icon: "P",
          status: presentation[0],
          badge: presentation[1],
        });
        setCardState(card, presentation[0], presentation[1]);
        setSingleCardLine(card, stringValue(data.message) || humanize(data.code));
        break;
      }

      case "production.completed": {
        const productionState = normalizeStatus(data.state || "completed");
        const partial = productionState === "partial";
        const failed = productionState === "failed";
        const card = ensureEventCard("production:summary", {
          title: "Production transaction",
          icon: "P",
          status: failed ? "error" : partial ? "warning" : "complete",
          badge: humanize(productionState),
        });
        setCardState(
          card,
          failed ? "error" : partial ? "warning" : "complete",
          humanize(productionState),
        );
        const parts = [];
        if (data.chain_id) {
          parts.push(`chain ${stringValue(data.chain_id)}`);
        }
        if (isRecord(data.baseline) && data.baseline.card_id) {
          parts.push(`baseline ${stringValue(data.baseline.card_id)}`);
        }
        if (isRecord(data.evolution) && data.evolution.evolution_id) {
          parts.push(`evolution ${stringValue(data.evolution.evolution_id)}`);
        }
        const pointer = normalizeProductionPointer(data.revision_pointer);
        if (pointer) {
          state.productionPointer = pointer;
        }
        setSingleCardLine(card, parts.length ? parts.join(" · ") : "No durable receipt was created.");
        break;
      }

      case "execution.started": {
        updateActiveStatus("executing");
        const steps = finiteNumber(data.steps);
        const card = ensureEventCard("execution:overview", {
          title: "Executing chain",
          icon: "E",
          status: "running",
          badge: "Running",
        });
        if (steps !== null) {
          setSingleCardLine(card, `${steps} ${steps === 1 ? "step" : "steps"} scheduled.`);
          setProgress(card, 0, steps);
        }
        break;
      }

      case "execution.step.started": {
        updateActiveStatus("executing");
        const number = numberOr(data.step_number, "?");
        state.currentExecutionStep = String(number);
        const title = stringValue(data.step_title) || `Step ${number}`;
        ensureEventCard(`execution:step:${String(number)}`, {
          title: `Step ${number} · ${title}`,
          icon: String(number).slice(0, 2),
          status: "running",
          badge: "Running",
        });
        break;
      }

      case "execution.step.completed": {
        const number = data.step_number !== undefined
          ? String(data.step_number)
          : state.currentExecutionStep || "?";
        const card = ensureEventCard(`execution:step:${number}`, {
          title: `Step ${number}`,
          icon: number.slice(0, 2),
          status: "complete",
          badge: "Complete",
        });
        setCardState(card, "complete", "Complete");
        if (hasUsefulValue(data.result)) {
          setResultPreview(card, "Step result", data.result);
        }
        break;
      }

      case "execution.progress": {
        updateActiveStatus("executing");
        const card = ensureEventCard("execution:overview", {
          title: "Executing chain",
          icon: "E",
          status: "running",
          badge: "Running",
        });
        const completed = finiteNumber(data.completed);
        const total = finiteNumber(data.total);
        if (completed !== null && total !== null) {
          setSingleCardLine(card, `${completed} of ${total} steps complete.`);
          setProgress(card, completed, total);
          if (total > 0 && completed >= total) {
            setCardState(card, "complete", "Complete");
          }
        }
        break;
      }

      case "execution.chunk": {
        updateActiveStatus("executing");
        const number = data.step_number !== undefined
          ? String(data.step_number)
          : state.currentExecutionStep || "?";
        const card = ensureEventCard(`execution:step:${number}`, {
          title: data.stage ? `Step ${number} · ${humanize(data.stage)}` : `Step ${number}`,
          icon: number.slice(0, 2),
          status: "running",
          badge: "Running",
        });
        appendStreamText(card, stringValue(data.chunk));
        break;
      }

      case "execution.step.event": {
        const number = data.step_number !== undefined
          ? String(data.step_number)
          : state.currentExecutionStep || "?";
        const card = ensureEventCard(`execution:step:${number}`, {
          title: `Step ${number}`,
          icon: number.slice(0, 2),
          status: "running",
          badge: "Running",
        });
        const eventName = humanize(data.event_type || "Step event");
        const payload = hasUsefulValue(data.payload) ? ` · ${valueToDisplay(data.payload)}` : "";
        appendCardLine(card, `${eventName}${payload}`);
        break;
      }

      case "execution.chain.completed": {
        const card = state.cards.get("execution:overview") || ensureEventCard("execution:overview", {
          title: "Chain execution",
          icon: "E",
          status: "complete",
          badge: "Complete",
        });
        setCardState(card, "complete", "Complete");
        if (hasUsefulValue(data.result)) {
          setResultPreview(card, "Execution result", data.result);
        }
        break;
      }

      case "human_input.requested":
        updateActiveStatus("waiting_for_input");
        renderHumanInput(data);
        break;

      case "human_input.resolved":
        markHumanInputResolved(data.request_id);
        updateActiveStatus("running");
        break;

      case "human_input.expired":
        removeById("human-input-card");
        updateActiveStatus("running");
        renderGenericEvent("Input request expired", "!", data);
        break;

      case "execution.routing":
        renderGenericEvent("Live-data routing", "↗", data);
        break;

      case "execution.warning": {
        const card = ensureEventCard(`execution:warning:${state.seenEventKeys.size}`, {
          title: "Execution warning",
          icon: "!",
          status: "warning",
          badge: "Warning",
        });
        appendCardLine(card, readValueError(data.message || data, "Execution reported a warning."));
        break;
      }

      case "execution.tool.status":
      case "execution.tool.reused":
      case "execution.tool.created":
      case "execution.tool.failed": {
        const failed = type === "execution.tool.failed";
        const card = ensureEventCard(`tool:${type}:${state.seenEventKeys.size}`, {
          title: failed ? "Tool preparation failed" : "Tool preparation",
          icon: failed ? "!" : "T",
          status: failed ? "error" : "neutral",
          badge: failed ? "Error" : "Tool",
        });
        setResultPreview(card, "Details", data);
        break;
      }

      case "run.completed":
        finishRun("completed", data.result, null);
        break;

      case "run.failed":
        finishRun("failed", data.result, data.error || "The run failed.");
        break;

      case "run.cancelled":
        finishRun("cancelled", data.result, data.error);
        break;

      case "generation.event":
        renderGenericEvent("Generation event", "G", data);
        break;

      case "execution.event":
        renderGenericEvent("Execution event", "E", data);
        break;

      case "message":
      default:
        renderGenericEvent(humanize(type || "Maestro event"), "·", data);
        break;
    }

    scrollConversation(false);
  }

  function finishRun(status, result, error) {
    if (!state.activeRun) {
      return;
    }

    state.activeRun.status = status;
    if (result !== undefined) {
      state.activeRun.result = result;
    }
    if (error !== undefined && error !== null) {
      state.activeRun.error = error;
    }

    removeById("transport-state-card");
    removeById("human-input-card");

    const lifecycle = state.cards.get("run-lifecycle");
    if (lifecycle && status === "completed") {
      setCardState(lifecycle, "complete", "Complete");
    }

    if (status === "completed") {
      const chain = extractChain(state.activeRun) || extractChain({ result });
      if (chain) {
        setChain(chain);
      }
      renderFinalAnswer(result !== undefined ? result : state.activeRun.result);
      announce("Run completed.");
    } else if (status === "failed") {
      renderRunError(error || state.activeRun.error || "The run failed.", "Run failed");
      announce("Run failed.");
    } else {
      renderCancelled();
      announce("Run cancelled.");
    }

    setRunStatus(status);
    upsertRun(state.activeRun);
    renderHistory();
    syncComposerState();
    closeRunTransport();
    setServiceState("connected", "Connected");
    void loadRuns({ showLoading: false });
    void refreshDisplayedRunSession(state.activeRun);
    scrollConversation(true);
  }

  function renderGenericEvent(title, icon, data) {
    const sequence = state.seenEventKeys.size;
    const card = ensureEventCard(`generic:${sequence}`, {
      title,
      icon,
      status: "neutral",
      badge: "Event",
    });
    if (hasUsefulValue(data)) {
      setResultPreview(card, "Details", data);
    }
  }

  function ensureEventCard(key, options) {
    const existing = state.cards.get(key);
    if (existing) {
      if (options.title) {
        setCardTitle(existing, options.title);
      }
      return existing;
    }

    const node = create("article", { className: "event-card" });
    const header = create("div", { className: "event-card-header" });
    const titleRow = create("div", { className: "event-card-title-row" });
    const icon = create("span", {
      className: "event-icon",
      text: options.icon || "·",
      attrs: { "aria-hidden": "true" },
    });
    const title = create("h3", { text: options.title || "Maestro event" });
    const badge = create("span", {
      className: "event-badge",
      text: options.badge || "Event",
    });
    const body = create("div", { className: "event-card-body" });
    titleRow.append(icon, title);
    header.append(titleRow, badge);
    node.append(header, body);

    const card = { node, title, badge, body, stream: null, result: null, progress: null };
    state.cards.set(key, card);
    setCardState(card, options.status || "neutral", options.badge || "Event");
    appendTimeline(node);
    return card;
  }

  function setCardTitle(card, title) {
    card.title.textContent = stringValue(title) || "Maestro event";
  }

  function setCardState(card, status, badge) {
    card.node.classList.remove(
      "is-running",
      "is-complete",
      "is-error",
      "is-warning",
      "is-cancelled",
    );
    const className = {
      running: "is-running",
      complete: "is-complete",
      error: "is-error",
      warning: "is-warning",
      cancelled: "is-cancelled",
    }[status];
    if (className) {
      card.node.classList.add(className);
    }
    card.badge.textContent = stringValue(badge) || "Event";
  }

  function appendCardLine(card, value) {
    const text = truncate(stringValue(value), 1400);
    if (!text) {
      return;
    }
    const lines = card.body.querySelectorAll(".event-line");
    if (lines.length >= 12) {
      lines[0].remove();
    }
    card.body.append(create("p", { className: "event-line", text }));
  }

  function setSingleCardLine(card, value) {
    let line = card.body.querySelector(".event-line");
    if (!line) {
      line = create("p", { className: "event-line" });
      card.body.prepend(line);
    }
    line.textContent = truncate(stringValue(value), 1400);
  }

  function appendStreamText(card, chunk) {
    if (!chunk) {
      return;
    }
    if (!card.stream) {
      card.stream = create("pre", {
        className: "stream-output",
        attrs: { "aria-label": "Live output" },
      });
      card.body.append(card.stream);
    }
    const combined = `${card.stream.textContent}${chunk}`;
    card.stream.textContent = combined.length > MAX_STREAM_CHARS
      ? `…${combined.slice(-(MAX_STREAM_CHARS - 1))}`
      : combined;
  }

  function setResultPreview(card, label, value) {
    if (card.result) {
      card.result.remove();
    }
    const details = create("details", { className: "event-result-details" });
    const summary = create("summary", { text: label });
    const preview = create("pre", {
      className: "result-preview",
      text: truncate(valueToDisplay(value), MAX_PREVIEW_CHARS),
    });
    details.append(summary, preview);
    card.body.append(details);
    card.result = details;
  }

  function setProgress(card, completed, total) {
    if (!card.progress) {
      const track = create("div", {
        className: "progress-track",
        attrs: {
          role: "progressbar",
          "aria-valuemin": "0",
        },
      });
      const bar = create("div", { className: "progress-bar" });
      track.append(bar);
      card.body.append(track);
      card.progress = { track, bar };
    }
    const safeTotal = Math.max(0, Number(total) || 0);
    const safeCompleted = Math.max(0, Number(completed) || 0);
    const percentage = safeTotal > 0 ? Math.min(100, (safeCompleted / safeTotal) * 100) : 0;
    card.progress.track.setAttribute("aria-valuemax", String(safeTotal));
    card.progress.track.setAttribute("aria-valuenow", String(Math.min(safeCompleted, safeTotal)));
    card.progress.bar.style.width = `${percentage}%`;
  }

  function renderChainReadyCard(chain) {
    const card = ensureEventCard("generation:chain", {
      title: "Chain generated",
      icon: "◇",
      status: "complete",
      badge: "Ready",
    });
    card.body.replaceChildren();
    card.stream = null;
    card.result = null;
    card.progress = null;
    const steps = chainSteps(chain);
    const overview = create("div", { className: "chain-overview" });
    overview.append(
      create("span", {
        className: "chain-overview-copy",
        text: `${steps.length} ${steps.length === 1 ? "step" : "steps"} · ${chainShape(steps)}`,
      }),
      create("button", {
        className: "inline-chain-button",
        text: "View chain",
        attrs: { type: "button" },
      }),
    );
    overview.querySelector("button").addEventListener("click", openChainDialog);
    card.body.append(overview);
  }

  function setChain(chain) {
    if (!isRecord(chain)) {
      return;
    }
    state.chain = chain;
    dom.chainButton.hidden = false;
    if (dom.chainDialog.open) {
      renderChainDialog();
    }
  }

  function openChainDialog() {
    if (!state.chain) {
      return;
    }
    renderChainDialog();
    if (typeof dom.chainDialog.showModal === "function") {
      if (!dom.chainDialog.open) {
        dom.chainDialog.showModal();
      }
    } else {
      dom.chainDialog.setAttribute("open", "");
    }
  }

  function closeChainDialog() {
    if (typeof dom.chainDialog.close === "function" && dom.chainDialog.open) {
      dom.chainDialog.close();
    } else {
      dom.chainDialog.removeAttribute("open");
    }
    dom.chainButton.focus();
  }

  function onDialogBackdropClick(event) {
    if (event.target === dom.chainDialog) {
      closeChainDialog();
    }
  }

  function renderChainDialog() {
    const chain = state.chain;
    if (!chain) {
      dom.chainDialogBody.replaceChildren(create("p", { text: "No chain is available." }));
      return;
    }

    const steps = chainSteps(chain);
    const fragment = document.createDocumentFragment();
    const summary = create("dl", { className: "chain-summary-grid" });
    addDefinition(summary, "Steps", String(steps.length));
    addDefinition(summary, "Shape", chainShape(steps));
    addDefinition(summary, "Mode", stringValue(state.activeRun && state.activeRun.mode) || "—");
    fragment.append(summary);

    const description = firstText(chain, ["task_description", "description", "task", "name"]);
    if (description) {
      fragment.append(create("p", { className: "chain-description", text: description }));
    }

    fragment.append(create("h3", {
      className: "chain-section-title",
      text: steps.length ? "Execution steps" : "No steps reported",
    }));

    if (steps.length) {
      const list = create("ol", { className: "chain-steps" });
      steps.forEach((step, index) => {
        const number = step.number !== undefined ? step.number : index + 1;
        const item = create("li", { className: "chain-step" });
        const marker = create("span", {
          className: "chain-step-number",
          text: String(number),
          attrs: { "aria-hidden": "true" },
        });
        const content = create("div");
        const title = firstText(step, ["title", "name", "task"]) || `Step ${number}`;
        content.append(create("h4", { text: title }));

        const type = firstText(step, ["step_type", "type", "kind"]) || "step";
        const dependencies = normalizeList(step.dependencies || step.depends_on);
        const metaParts = [humanize(type)];
        if (dependencies.length) {
          metaParts.push(`after ${dependencies.join(", ")}`);
        }
        content.append(create("p", {
          className: "chain-step-meta",
          text: metaParts.join(" · "),
        }));

        const stepDescription = firstText(step, ["description", "prompt", "instruction"]);
        if (stepDescription && stepDescription !== title) {
          content.append(create("p", {
            className: "chain-step-description",
            text: truncate(stepDescription, 1600),
          }));
        }
        item.append(marker, content);
        list.append(item);
      });
      fragment.append(list);
    }

    const rawDetails = create("details", { className: "raw-chain-details" });
    rawDetails.append(
      create("summary", { text: "Raw chain JSON" }),
      create("pre", { className: "raw-chain", text: safeJson(chain, 2) }),
    );
    fragment.append(rawDetails);
    dom.chainDialogBody.replaceChildren(fragment);
  }

  function addDefinition(list, term, definition) {
    const group = create("div");
    group.append(create("dt", { text: term }), create("dd", { text: definition }));
    list.append(group);
  }

  function renderHumanInput(data) {
    removeById("human-input-card");
    const requestId = stringValue(data.request_id);
    const card = create("article", {
      className: "human-input-card",
      attrs: { id: "human-input-card" },
    });
    card.append(
      create("h3", { text: "Maestro needs your input" }),
      create("p", { text: stringValue(data.prompt) || "Please provide the requested value." }),
    );

    const form = create("form", { className: "human-input-form" });
    const label = create("label", {
      text: "Your response",
      attrs: { for: "human-input-value" },
    });
    const input = create("textarea", {
      attrs: {
        id: "human-input-value",
        name: "value",
        rows: "2",
        maxlength: "32000",
        required: "",
      },
    });
    const actions = create("div", { className: "human-input-actions" });
    const submit = create("button", {
      className: "human-submit-button",
      text: "Continue",
      attrs: { type: "submit" },
    });
    actions.append(submit);
    form.append(label, input, actions);
    card.append(form);
    form.addEventListener("submit", (event) => {
      event.preventDefault();
      void submitHumanInput(form, input, submit, requestId);
    });
    input.addEventListener("keydown", (event) => {
      if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
        event.preventDefault();
        form.requestSubmit();
      }
    });

    appendTimeline(card);
    requestAnimationFrame(() => input.focus());
  }

  async function submitHumanInput(form, input, submit, requestId) {
    if (!state.activeRun || !state.activeRun.id) {
      return;
    }
    const value = input.value.trim();
    if (!value) {
      input.focus();
      return;
    }

    input.disabled = true;
    submit.disabled = true;
    hideComposerAlert();
    const body = { value };
    if (requestId) {
      body.request_id = requestId;
    }

    try {
      const runId = encodeURIComponent(String(state.activeRun.id));
      const run = await apiFetch(`${API_ROOT}/runs/${runId}/input`, {
        method: "POST",
        body: JSON.stringify(body),
      });
      if (isRecord(run) && state.activeRun && String(run.id) === String(state.activeRun.id)) {
        state.activeRun = { ...state.activeRun, ...run };
        upsertRun(state.activeRun);
      }
      markHumanInputResolved(requestId);
      updateActiveStatus("running");
      announce("Input sent. The run is continuing.");
    } catch (error) {
      input.disabled = false;
      submit.disabled = false;
      showComposerAlert(readError(error, "Could not send your input."));
      input.focus();
    }
  }

  function markHumanInputResolved(requestId) {
    const card = document.getElementById("human-input-card");
    if (!card) {
      return;
    }
    const form = card.querySelector("form");
    if (form) {
      form.remove();
    }
    card.classList.add("is-resolved");
    card.append(create("p", {
      className: "human-resolved-note",
      text: requestId ? "Response accepted. Maestro is continuing." : "Input accepted.",
    }));
  }

  function renderFinalAnswer(result) {
    removeById("final-card");
    const answer = extractAnswer(result);
    const chain = extractChain({ result });
    if (chain) {
      setChain(chain);
    }

    const card = create("article", {
      className: "assistant-answer",
      attrs: { id: "final-card" },
    });
    const header = create("div", { className: "assistant-answer-header" });
    header.append(
      create("img", {
        className: "assistant-answer-mark",
        attrs: { src: "maestro-mark.svg", alt: "", width: "26", height: "26" },
      }),
      create("h3", { text: answer ? "Final answer" : "Chain ready" }),
    );
    card.append(header);

    if (answer) {
      card.append(renderSafeAnswerContent(answer));
    } else if (chain) {
      card.append(create("p", {
        className: "assistant-answer-content",
        text: "Maestro generated and validated the chain. Open Chain to inspect every step.",
      }));
    } else if (hasUsefulValue(result)) {
      card.append(create("pre", {
        className: "assistant-answer-content",
        text: truncate(valueToDisplay(result), MAX_PREVIEW_CHARS),
      }));
    } else {
      card.append(create("p", {
        className: "assistant-answer-content",
        text: "The run completed without a textual answer.",
      }));
    }

    const mode = state.activeRun && state.activeRun.mode;
    if (mode) {
      card.append(create("p", {
        className: "assistant-answer-meta",
        text: `${humanize(mode)} mode`,
      }));
    }
    appendTimeline(card, { beforeFinal: false });
  }

  function renderSafeAnswerContent(answer) {
    const content = create("div", { className: "assistant-answer-content" });
    const lines = stringValue(answer).replace(/\r\n?/g, "\n").split("\n");
    let textLines = [];
    let codeLines = [];
    let fence = null;
    let language = "";
    const flushText = () => {
      if (!textLines.length) {
        return;
      }
      content.append(create("p", { text: textLines.join("\n") }));
      textLines = [];
    };
    for (const line of lines) {
      if (!fence) {
        const opening = line.match(/^ {0,3}(`{3,}|~{3,})(.*)$/);
        if (!opening) {
          textLines.push(line);
          continue;
        }
        flushText();
        fence = { marker: opening[1][0], length: opening[1].length, opening: line };
        language = opening[2].trim().split(/\s+/, 1)[0]
          .replace(/[^A-Za-z0-9_+-]/g, "")
          .slice(0, 40);
        codeLines = [];
        continue;
      }
      if (!isClosingFence(line, fence.marker, fence.length)) {
        codeLines.push(line);
        continue;
      }
      const pre = create("pre");
      const code = create("code", {
        className: language ? `language-${language}` : "",
        text: codeLines.join("\n"),
        attrs: language ? { "data-language": language } : {},
      });
      pre.append(code);
      content.append(pre);
      fence = null;
      language = "";
      codeLines = [];
    }
    if (fence) {
      textLines.push(fence.opening, ...codeLines);
    }
    flushText();
    return content;
  }

  function renderRunError(error, title = "Run failed", deduplicate = true) {
    if (deduplicate) {
      removeById("run-error-card");
    }
    const node = create("article", {
      className: "event-card is-error",
      attrs: deduplicate ? { id: "run-error-card" } : {},
    });
    const header = create("div", { className: "event-card-header" });
    const titleRow = create("div", { className: "event-card-title-row" });
    titleRow.append(
      create("span", {
        className: "event-icon",
        text: "!",
        attrs: { "aria-hidden": "true" },
      }),
      create("h3", { text: title }),
    );
    header.append(titleRow, create("span", { className: "event-badge", text: "Error" }));
    const body = create("div", { className: "event-card-body" });
    body.append(create("p", {
      className: "event-line",
      text: readValueError(error, "An unknown error occurred."),
    }));
    node.append(header, body);
    appendTimeline(node);
  }

  function renderCancelled() {
    if (document.getElementById("cancelled-card")) {
      return;
    }
    const card = ensureEventCard("run:cancelled", {
      title: "Run cancelled",
      icon: "■",
      status: "cancelled",
      badge: "Stopped",
    });
    card.node.id = "cancelled-card";
    appendCardLine(card, "Execution stopped before completion.");
  }

  function showRunErrorState(message, retry) {
    dom.emptyState.hidden = true;
    dom.runView.hidden = false;
    dom.userPrompt.textContent = "";
    const card = makeStateCard("Could not load run", message, "error");
    const button = create("button", {
      className: "state-action-button",
      text: "Try again",
      attrs: { type: "button" },
    });
    button.addEventListener("click", retry);
    card.append(button);
    dom.eventTimeline.replaceChildren(card);
  }

  function makeStateCard(title, message, kind = "neutral", id = "") {
    const classNames = ["conversation-state-card"];
    if (kind === "loading") {
      classNames.push("is-loading");
    } else if (kind === "error") {
      classNames.push("is-error");
    }
    const attrs = id ? { id } : {};
    const card = create("article", { className: classNames.join(" "), attrs });
    card.append(create("h3", { text: title }), create("p", { text: message }));
    return card;
  }

  function appendTimeline(node, { beforeFinal = true } = {}) {
    const shouldFollow = isConversationNearBottom();
    const final = document.getElementById("final-card");
    if (beforeFinal && final && final.parentNode === dom.eventTimeline && node !== final) {
      dom.eventTimeline.insertBefore(node, final);
    } else {
      dom.eventTimeline.append(node);
    }
    if (shouldFollow) {
      scrollConversation(false);
    }
  }

  function showRunShell(prompt) {
    dom.emptyState.hidden = true;
    dom.runView.hidden = false;
    dom.userPrompt.textContent = prompt;
    dom.currentUserMessage.hidden = !stringValue(prompt);
  }

  function setRunHeader(title, status, label) {
    dom.runTitle.textContent = title || "New chat";
    dom.runStatus.dataset.tone = statusTone(status);
    const statusText = label || statusLabel(status);
    dom.runStatusLabel.textContent = statusText;
    setMaestricMood(status, statusText);
  }

  function setRunStatus(status) {
    const normalized = normalizeStatus(status);
    const title = state.activeRun && state.activeRun.prompt
      ? historyTitle(state.activeRun.prompt)
      : "Run";
    setRunHeader(title, normalized, statusLabel(normalized));
    dom.cancelButton.hidden = !isRunActive(normalized);
    dom.cancelButton.disabled = normalized === "cancelling" || state.cancelling;
  }

  function updateActiveStatus(status) {
    if (!state.activeRun) {
      return;
    }
    state.activeRun.status = status;
    setRunStatus(status);
    upsertRun(state.activeRun);
    renderHistory();
    syncComposerState();
  }

  function syncComposerState() {
    const busy = state.submitting || state.commandSubmitting || Boolean(state.pendingConfirmation) || (
      state.activeRun && isRunActive(state.activeRun.status)
    );
    dom.composerForm.classList.toggle("is-busy", Boolean(busy));
    dom.promptInput.disabled = Boolean(busy);
    dom.executeInput.disabled = Boolean(busy);
    dom.sendButton.disabled = Boolean(busy);
    for (const radio of document.querySelectorAll('input[name="mode"], input[name="session-mode"]')) {
      radio.disabled = Boolean(busy);
    }
    dom.promptInput.placeholder = busy
      ? "Maestro is working…"
      : state.multiLineMode
        ? "Write a multi-line task…"
        : "Ask Maestro to build a chain or type / for commands…";
    dom.keyboardHint.textContent = state.multiLineMode
      ? "⌘/Ctrl+Enter to send"
      : "Shift+Enter for newline";
    updateComposerFootnote();
  }

  function updateComposerFootnote() {
    const selectedSessionMode = document.querySelector('input[name="session-mode"]:checked');
    const productionMode = selectedSessionMode && selectedSessionMode.value === "production";
    if (state.submitting) {
      dom.composerFootnote.textContent = "Creating the run…";
    } else if (state.commandSubmitting) {
      dom.composerFootnote.textContent = "Running the Maestro command…";
    } else if (state.pendingConfirmation) {
      dom.composerFootnote.textContent = "Review the command risk and explicitly confirm or cancel.";
    } else if (state.activeRun && isRunActive(state.activeRun.status)) {
      if (normalizeStatus(state.activeRun.status) === "waiting_for_input") {
        dom.composerFootnote.textContent = "Respond to Maestro’s question in the conversation.";
      } else {
        dom.composerFootnote.textContent = "Live progress is shown above. You can cancel at any time.";
      }
    } else if (state.multiLineMode) {
      dom.composerFootnote.textContent = "Multi-line mode is on. Press Command or Control + Enter, or use the send button.";
    } else if (productionMode && dom.executeInput.checked) {
      dom.composerFootnote.textContent = "Agent mode: generate a deployable chain, save it, confirm the baseline run, then request optional evolution.";
    } else if (productionMode) {
      dom.composerFootnote.textContent = "Agent preview: generate and typed-validate a deployable chain without saving or running it.";
    } else if (dom.executeInput.checked) {
      dom.composerFootnote.textContent = "Execute is on: Maestro will run the generated chain after validation.";
    } else {
      dom.composerFootnote.textContent = "Execute is off: Maestro will generate and validate the chain only.";
    }
  }

  function resizePrompt() {
    dom.promptInput.style.height = "auto";
    const minimum = state.multiLineMode ? 126 : 34;
    dom.promptInput.style.height = `${Math.max(minimum, Math.min(dom.promptInput.scrollHeight, 210))}px`;
  }

  function onPromptInput() {
    resizePrompt();
    updateSlashAutocomplete();
  }

  function onPromptKeyDown(event) {
    if (!dom.slashMenu.hidden) {
      if (["ArrowDown", "ArrowUp"].includes(event.key)) {
        event.preventDefault();
        moveSlashSelection(event.key === "ArrowDown" ? 1 : -1);
        return;
      }
      if (event.key === "Tab" && state.slashMatches.length) {
        event.preventDefault();
        selectSlashMatch(Math.max(0, state.slashIndex));
        return;
      }
      if (event.key === "Enter" && !event.shiftKey && !event.isComposing && state.slashMatches.length) {
        const selected = state.slashMatches[Math.max(0, state.slashIndex)];
        const current = dom.promptInput.value.trim().toLowerCase();
        const selectedValue = selected.slash.toLowerCase();
        if (current !== selectedValue && !current.startsWith(`${selectedValue} `)) {
          event.preventDefault();
          selectSlashMatch(Math.max(0, state.slashIndex));
          return;
        }
        closeSlashMenu();
      }
    }

    if (state.multiLineMode) {
      if (event.key === "Enter" && (event.metaKey || event.ctrlKey) && !event.isComposing) {
        event.preventDefault();
        if (!dom.sendButton.disabled) {
          dom.composerForm.requestSubmit();
        }
      }
      return;
    }

    if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
      event.preventDefault();
      if (!dom.sendButton.disabled) {
        dom.composerForm.requestSubmit();
      }
    }
  }

  function activateMultiLineMode(initialValue = "") {
    state.multiLineMode = true;
    dom.composerForm.classList.add("is-multiline");
    dom.promptInput.value = stringValue(initialValue).replace(/^\s+/, "");
    dom.promptInput.setAttribute("rows", "5");
    closeSlashMenu();
    hideComposerAlert();
    syncComposerState();
    resizePrompt();
    dom.promptInput.focus();
    dom.promptInput.setSelectionRange(dom.promptInput.value.length, dom.promptInput.value.length);
  }

  function deactivateMultiLineMode() {
    state.multiLineMode = false;
    dom.composerForm.classList.remove("is-multiline");
    dom.promptInput.setAttribute("rows", "1");
  }

  function updateSlashAutocomplete() {
    const input = dom.promptInput.value.replace(/^\s+/, "");
    if (!input.startsWith("/") || input.includes("\n") || state.commandSubmitting || state.pendingConfirmation) {
      closeSlashMenu();
      return;
    }
    const query = input.trim().toLowerCase();
    const seen = new Set();
    const matches = [];
    for (const entry of state.commandEntries) {
      if (entry.available === false) {
        continue;
      }
      const slash = slashInvocation(entry);
      const key = slash.toLowerCase();
      if (seen.has(key)) {
        continue;
      }
      seen.add(key);
      const description = stringValue(entry.description);
      const haystack = `${key} ${description.toLowerCase()}`;
      if (query && !key.startsWith(query) && !query.startsWith(`${key} `) && !haystack.includes(query)) {
        continue;
      }
      matches.push({ ...entry, slash });
    }
    matches.sort((left, right) => {
      const leftPrefix = left.slash.toLowerCase().startsWith(query) ? 1 : 0;
      const rightPrefix = right.slash.toLowerCase().startsWith(query) ? 1 : 0;
      return rightPrefix - leftPrefix || left.slash.localeCompare(right.slash);
    });
    state.slashMatches = matches.slice(0, MAX_SLASH_RESULTS);
    state.slashIndex = state.slashMatches.length ? 0 : -1;
    renderSlashAutocomplete();
  }

  function renderSlashAutocomplete() {
    if (!state.slashMatches.length) {
      closeSlashMenu();
      return;
    }
    const fragment = document.createDocumentFragment();
    state.slashMatches.forEach((entry, index) => {
      const button = create("button", {
        className: "slash-command-item",
        attrs: {
          id: `slash-command-${index}`,
          type: "button",
          role: "option",
          "aria-selected": String(index === state.slashIndex),
        },
      });
      button.append(
        create("code", { text: entry.slash }),
        create("span", { text: entry.description }),
      );
      button.addEventListener("mouseenter", () => setSlashSelection(index));
      button.addEventListener("click", () => selectSlashMatch(index));
      fragment.append(button);
    });
    dom.slashCommandList.replaceChildren(fragment);
    dom.slashMenu.hidden = false;
    dom.promptInput.setAttribute("aria-expanded", "true");
    updateSlashActiveDescendant();
  }

  function moveSlashSelection(delta) {
    if (!state.slashMatches.length) {
      return;
    }
    const index = (state.slashIndex + delta + state.slashMatches.length) % state.slashMatches.length;
    setSlashSelection(index);
  }

  function setSlashSelection(index) {
    if (index < 0 || index >= state.slashMatches.length) {
      return;
    }
    state.slashIndex = index;
    const buttons = Array.from(dom.slashCommandList.querySelectorAll("button"));
    buttons.forEach((button, buttonIndex) => {
      button.setAttribute("aria-selected", String(buttonIndex === index));
    });
    updateSlashActiveDescendant();
    const active = buttons[index];
    if (active) {
      active.scrollIntoView({ block: "nearest" });
    }
  }

  function updateSlashActiveDescendant() {
    if (state.slashIndex >= 0) {
      dom.promptInput.setAttribute("aria-activedescendant", `slash-command-${state.slashIndex}`);
    } else {
      dom.promptInput.removeAttribute("aria-activedescendant");
    }
  }

  function selectSlashMatch(index) {
    const entry = state.slashMatches[index];
    if (!entry) {
      return;
    }
    dom.promptInput.value = `${entry.slash} `;
    closeSlashMenu();
    resizePrompt();
    dom.promptInput.focus();
    dom.promptInput.setSelectionRange(dom.promptInput.value.length, dom.promptInput.value.length);
  }

  function closeSlashMenu() {
    state.slashMatches = [];
    state.slashIndex = -1;
    dom.slashMenu.hidden = true;
    dom.slashCommandList.replaceChildren();
    dom.promptInput.setAttribute("aria-expanded", "false");
    dom.promptInput.removeAttribute("aria-activedescendant");
  }

  function showComposerAlert(message) {
    dom.composerAlertText.textContent = message;
    dom.composerAlert.hidden = false;
  }

  function hideComposerAlert() {
    dom.composerAlert.hidden = true;
    dom.composerAlertText.textContent = "";
  }

  function setServiceState(kind, label) {
    dom.serviceState.dataset.state = kind;
    dom.serviceStateLabel.textContent = label;
  }

  function openSidebar() {
    dom.appShell.classList.add("sidebar-open");
    dom.sidebarOpen.setAttribute("aria-expanded", "true");
    dom.sidebarScrim.tabIndex = 0;
    requestAnimationFrame(() => dom.sidebarClose.focus());
  }

  function closeSidebar() {
    const wasOpen = dom.appShell.classList.contains("sidebar-open");
    dom.appShell.classList.remove("sidebar-open");
    dom.sidebarOpen.setAttribute("aria-expanded", "false");
    dom.sidebarScrim.tabIndex = -1;
    if (wasOpen && window.matchMedia("(max-width: 820px)").matches) {
      dom.sidebarOpen.focus();
    }
  }

  function closeRunTransport() {
    if (state.source) {
      state.source.close();
      state.source = null;
    }
    if (state.pollTimer) {
      window.clearTimeout(state.pollTimer);
      state.pollTimer = null;
    }
    state.sourceRunId = null;
  }

  function startRunPolling(runId) {
    const poll = async () => {
      if (!state.activeRun || String(state.activeRun.id) !== runId) {
        return;
      }
      try {
        const run = await apiFetch(`${API_ROOT}/runs/${encodeURIComponent(runId)}`);
        if (!state.activeRun || String(state.activeRun.id) !== runId || !isRecord(run)) {
          return;
        }
        state.activeRun = { ...state.activeRun, ...run };
        setRunStatus(run.status);
        if (TERMINAL_STATUSES.has(normalizeStatus(run.status))) {
          finishRun(run.status, run.result, run.error);
          return;
        }
        setServiceState("reconnecting", "Polling");
      } catch (_error) {
        setServiceState("offline", "API unavailable");
      }
      state.pollTimer = window.setTimeout(poll, 1600);
    };
    void poll();
  }

  function scrollConversation(force) {
    if (!force && !isConversationNearBottom()) {
      return;
    }
    requestAnimationFrame(() => {
      dom.conversation.scrollTop = dom.conversation.scrollHeight;
    });
  }

  function isConversationNearBottom() {
    const distance = dom.conversation.scrollHeight
      - dom.conversation.scrollTop
      - dom.conversation.clientHeight;
    return distance < 180;
  }

  async function apiFetch(path, options = {}) {
    const headers = new Headers(options.headers || {});
    headers.set("Accept", "application/json");
    if (options.body !== undefined && !headers.has("Content-Type")) {
      headers.set("Content-Type", "application/json");
    }

    let response;
    try {
      response = await fetch(path, {
        ...options,
        headers,
        credentials: "same-origin",
      });
    } catch (_error) {
      throw new ApiError("Cannot reach the Maestro server.");
    }

    const contentType = response.headers.get("content-type") || "";
    let payload = null;
    if (response.status !== 204) {
      if (contentType.includes("application/json")) {
        try {
          payload = await response.json();
        } catch (_error) {
          throw new ApiError("The server returned invalid JSON.", response.status);
        }
      } else {
        const text = await response.text();
        payload = text ? { detail: text } : null;
      }
    }

    if (!response.ok) {
      throw new ApiError(extractApiError(payload) || `Request failed (${response.status}).`, response.status, payload);
    }
    return payload;
  }

  function extractApiError(payload) {
    if (typeof payload === "string") {
      return payload;
    }
    if (!isRecord(payload)) {
      return "";
    }
    for (const key of ["detail", "message", "error"]) {
      const value = payload[key];
      if (typeof value === "string" && value.trim()) {
        return value.trim();
      }
      if (isRecord(value) && typeof value.message === "string") {
        return value.message;
      }
    }
    return "";
  }

  function upsertRun(run) {
    if (!isRecord(run) || !run.id) {
      return;
    }
    const id = String(run.id);
    const index = state.runs.findIndex((candidate) => String(candidate.id) === id);
    if (index >= 0) {
      state.runs[index] = { ...state.runs[index], ...run };
    } else {
      state.runs.unshift(run);
    }
    renderHistory();
  }

  function extractChain(run) {
    if (!isRecord(run)) {
      return null;
    }
    if (isRecord(run.chain)) {
      return run.chain;
    }
    const result = run.result;
    if (isRecord(result)) {
      if (isRecord(result.chain)) {
        return result.chain;
      }
      if (isRecord(result.result) && isRecord(result.result.chain)) {
        return result.result.chain;
      }
    }
    return null;
  }

  function extractAnswer(result) {
    if (typeof result === "string") {
      return result;
    }
    if (!isRecord(result)) {
      return "";
    }
    for (const key of ["answer", "final_answer", "output", "response"]) {
      if (typeof result[key] === "string" && result[key].trim()) {
        return result[key];
      }
    }
    if (isRecord(result.result)) {
      return extractAnswer(result.result);
    }
    return "";
  }

  function chainSteps(chain) {
    return isRecord(chain) && Array.isArray(chain.steps)
      ? chain.steps.filter(isRecord)
      : [];
  }

  function chainShape(steps) {
    if (!steps.length) {
      return "Empty chain";
    }
    const hasDependencies = steps.some((step) => normalizeList(step.dependencies || step.depends_on).length > 0);
    return hasDependencies ? "Dependency graph" : "Linear flow";
  }

  function stageName(value) {
    const raw = stringValue(value) || "generation";
    return {
      key: raw.toLowerCase(),
      label: humanize(raw),
    };
  }

  function normalizeStatus(value) {
    return stringValue(value).trim().toLowerCase().replace(/[\s-]+/g, "_") || "idle";
  }

  function statusTone(status) {
    const normalized = normalizeStatus(status);
    if (["completed", "succeeded", "success"].includes(normalized)) {
      return "success";
    }
    if (["failed", "error"].includes(normalized)) {
      return "danger";
    }
    if (["cancelled", "canceled"].includes(normalized)) {
      return "warning";
    }
    if (ACTIVE_STATUSES.has(normalized) || normalized === "loading") {
      return "running";
    }
    return "idle";
  }

  function statusLabel(status) {
    const normalized = normalizeStatus(status);
    const labels = {
      idle: "Ready",
      loading: "Loading",
      created: "Created",
      pending: "Queued",
      queued: "Queued",
      running: "Running",
      generating: "Generating chain",
      executing: "Executing chain",
      waiting_for_input: "Waiting for input",
      cancelling: "Cancelling",
      completed: "Completed",
      failed: "Failed",
      cancelled: "Cancelled",
      canceled: "Cancelled",
    };
    return labels[normalized] || humanize(normalized);
  }

  function isRunActive(status) {
    return ACTIVE_STATUSES.has(normalizeStatus(status));
  }

  function historyTitle(prompt) {
    const text = stringValue(prompt).trim().replace(/\s+/g, " ");
    if (!text) {
      return "Untitled run";
    }
    return truncate(text, 72);
  }

  function formatRelativeTime(value) {
    const timestamp = timestampOf(value);
    if (!timestamp) {
      return "";
    }
    const deltaSeconds = Math.round((timestamp - Date.now()) / 1000);
    const absolute = Math.abs(deltaSeconds);
    const formatter = new Intl.RelativeTimeFormat(undefined, { numeric: "auto" });
    if (absolute < 60) {
      return formatter.format(deltaSeconds, "second");
    }
    if (absolute < 3600) {
      return formatter.format(Math.round(deltaSeconds / 60), "minute");
    }
    if (absolute < 86400) {
      return formatter.format(Math.round(deltaSeconds / 3600), "hour");
    }
    if (absolute < 604800) {
      return formatter.format(Math.round(deltaSeconds / 86400), "day");
    }
    return new Intl.DateTimeFormat(undefined, { month: "short", day: "numeric" }).format(timestamp);
  }

  function timestampOf(value) {
    const timestamp = Date.parse(stringValue(value));
    return Number.isFinite(timestamp) ? timestamp : 0;
  }

  function firstText(record, keys) {
    if (!isRecord(record)) {
      return "";
    }
    for (const key of keys) {
      const value = record[key];
      if (typeof value === "string" && value.trim()) {
        return value.trim();
      }
    }
    return "";
  }

  function normalizeList(value) {
    if (Array.isArray(value)) {
      return value.map(stringValue).filter(Boolean);
    }
    if (value === null || value === undefined || value === "") {
      return [];
    }
    return [stringValue(value)].filter(Boolean);
  }

  function humanize(value) {
    const text = stringValue(value)
      .replace(/[._/-]+/g, " ")
      .replace(/\s+/g, " ")
      .trim();
    if (!text) {
      return "Maestro event";
    }
    return text.charAt(0).toUpperCase() + text.slice(1);
  }

  function valueToDisplay(value) {
    if (typeof value === "string") {
      return value;
    }
    if (value === null || value === undefined) {
      return "";
    }
    if (typeof value === "number" || typeof value === "boolean" || typeof value === "bigint") {
      return String(value);
    }
    return safeJson(value, 2);
  }

  function safeJson(value, spacing = 0) {
    try {
      return JSON.stringify(value, null, spacing);
    } catch (_error) {
      return "[Unserializable value]";
    }
  }

  function hasUsefulValue(value) {
    if (value === null || value === undefined || value === "") {
      return false;
    }
    if (Array.isArray(value)) {
      return value.length > 0;
    }
    if (isRecord(value)) {
      return Object.keys(value).length > 0;
    }
    return true;
  }

  function stringValue(value) {
    if (value === null || value === undefined) {
      return "";
    }
    if (typeof value === "string") {
      return value;
    }
    if (typeof value === "number" || typeof value === "boolean" || typeof value === "bigint") {
      return String(value);
    }
    return valueToDisplay(value);
  }

  function readError(error, fallback) {
    if (error instanceof ApiError && error.message) {
      return error.message;
    }
    if (error instanceof Error && error.message) {
      return error.message;
    }
    return readValueError(error, fallback);
  }

  function readValueError(value, fallback) {
    if (typeof value === "string" && value.trim()) {
      return value.trim();
    }
    if (isRecord(value)) {
      for (const key of ["message", "detail", "error", "reason"]) {
        if (typeof value[key] === "string" && value[key].trim()) {
          return value[key].trim();
        }
      }
    }
    return fallback;
  }

  function finiteNumber(value) {
    const number = Number(value);
    return Number.isFinite(number) ? number : null;
  }

  function numberOr(value, fallback) {
    const number = finiteNumber(value);
    return number === null ? fallback : number;
  }

  function truncate(value, length) {
    const text = stringValue(value);
    if (text.length <= length) {
      return text;
    }
    return `${text.slice(0, Math.max(0, length - 1))}…`;
  }

  function isRecord(value) {
    return value !== null && typeof value === "object" && !Array.isArray(value);
  }

  function create(tag, { className = "", text, attrs = {} } = {}) {
    const node = document.createElement(tag);
    if (className) {
      node.className = className;
    }
    if (text !== undefined) {
      node.textContent = stringValue(text);
    }
    for (const [name, value] of Object.entries(attrs)) {
      if (value !== undefined && value !== null) {
        node.setAttribute(name, String(value));
      }
    }
    return node;
  }

  function removeById(id) {
    const node = document.getElementById(id);
    if (node) {
      node.remove();
    }
  }

  function announce(message) {
    dom.announcer.textContent = "";
    window.setTimeout(() => {
      dom.announcer.textContent = message;
    }, 20);
  }

  function toCamel(value) {
    return value.replace(/-([a-z])/g, (_match, letter) => letter.toUpperCase());
  }

  init();
})();
