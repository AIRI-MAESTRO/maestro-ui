window.__ModuleLoader__.load({
  id: "@maestro-care/dsh-client-ui-settings",
  factory: (require) => {
    var module = { exports: {} };
    var exports = module.exports;
    Object.defineProperty(exports, Symbol.toStringTag, { value: "Module" });

    const React = require("react");
    const { createElement: h, useEffect, useState } = React;

    const css = `
      .maestro-settings{color:var(--dsw-alias-label-primary);max-width:760px;padding:4px 0 36px}
      .maestro-settings *{box-sizing:border-box}
      .maestro-settings__hero{background:linear-gradient(135deg,rgba(30,197,177,.14),rgba(27,130,119,.04));border:1px solid rgba(62,216,194,.25);border-radius:22px;margin-bottom:20px;overflow:hidden;padding:22px;position:relative}
      .maestro-settings__hero:after{background:radial-gradient(circle,rgba(65,225,201,.32),transparent 64%);content:"";height:170px;pointer-events:none;position:absolute;right:-72px;top:-88px;width:170px}
      .maestro-settings__eyebrow{color:#42d8c2;font-size:11px;font-weight:750;letter-spacing:.18em;margin-bottom:7px;text-transform:uppercase}
      .maestro-settings h2{font-size:23px;letter-spacing:-.025em;line-height:1.2;margin:0 0 7px}
      .maestro-settings__lead,.maestro-settings__hint{color:var(--dsw-alias-label-secondary);font-size:13px;line-height:1.55;margin:0}
      .maestro-settings__card{background:var(--dsw-alias-bg-module-platform);border:1px solid var(--dsw-alias-border-l2);border-radius:18px;margin-top:12px;padding:18px}
      .maestro-settings__card h3{font-size:15px;margin:0 0 14px}
      .maestro-settings__grid{display:grid;gap:14px;grid-template-columns:repeat(2,minmax(0,1fr))}
      .maestro-settings__field{display:flex;flex-direction:column;gap:6px;min-width:0}
      .maestro-settings__field--wide{grid-column:1/-1}
      .maestro-settings__label{font-size:12px;font-weight:650}
      .maestro-settings input,.maestro-settings select{background:var(--dsw-alias-bg-base);border:1px solid var(--dsw-alias-border-l2);border-radius:11px;color:var(--dsw-alias-label-primary);font:inherit;font-size:13px;height:40px;outline:none;padding:0 12px;width:100%}
      .maestro-settings input:focus,.maestro-settings select:focus{border-color:#32cbb5;box-shadow:0 0 0 3px rgba(50,203,181,.12)}
      .maestro-settings__key-state{color:#44cfb9;font-size:11px;font-weight:650}
      .maestro-settings__secret-row{align-items:center;display:flex;flex-wrap:wrap;gap:9px}
      .maestro-settings__secret-row button{min-height:36px}
      .maestro-settings__check{align-items:center;color:var(--dsw-alias-label-secondary);display:flex;font-size:12px;gap:8px;margin-top:3px}
      .maestro-settings__check input{accent-color:#31cdb6;height:15px;width:15px}
      .maestro-settings__actions{align-items:center;display:flex;gap:10px;justify-content:flex-end;margin-top:18px}
      .maestro-settings button{border:0;border-radius:11px;cursor:pointer;font:inherit;font-size:13px;font-weight:700;min-height:38px;padding:0 16px}
      .maestro-settings button:disabled{cursor:default;opacity:.55}
      .maestro-settings__primary{background:#35cfb8;color:#072b26}
      .maestro-settings__secondary{background:var(--dsw-alias-interactive-bg-hover);color:var(--dsw-alias-label-primary)}
      .maestro-settings__notice{border-radius:12px;font-size:12px;line-height:1.5;margin-top:13px;padding:11px 13px}
      .maestro-settings__notice--error{background:rgba(240,78,89,.12);border:1px solid rgba(240,78,89,.3);color:#ff8a92}
      .maestro-settings__notice--success{background:rgba(53,207,184,.11);border:1px solid rgba(53,207,184,.28);color:#64dec9}
      .maestro-settings__notice--restart{background:rgba(238,174,74,.12);border:1px solid rgba(238,174,74,.34);color:#f2c46f;position:sticky;top:10px;z-index:2}
      .maestro-settings__changes{color:var(--dsw-alias-label-secondary);font-size:12px;line-height:1.5;margin:0;padding-left:20px}
      .maestro-settings__changes li+li{margin-top:5px}
      .maestro-settings__lock-note{border-left:2px solid #35cfb8;margin-top:10px;padding-left:10px}
      .maestro-settings__toggle-grid{display:grid;gap:10px;grid-template-columns:repeat(2,minmax(0,1fr));margin-top:14px}
      .maestro-settings__toggle{align-items:center;background:var(--dsw-alias-bg-base);border:1px solid var(--dsw-alias-border-l2);border-radius:12px;display:flex;font-size:12px;gap:9px;padding:11px 12px}
      .maestro-settings__toggle input{accent-color:#31cdb6;height:15px;width:15px}
      .maestro-settings details.maestro-settings__card{padding:0}
      .maestro-settings details>summary{cursor:pointer;font-size:15px;font-weight:700;list-style:none;padding:18px}
      .maestro-settings details>summary:after{color:#42d8c2;content:"+";float:right;font-size:19px;font-weight:400}
      .maestro-settings details[open]>summary:after{content:"−"}
      .maestro-settings details>.maestro-settings__details{border-top:1px solid var(--dsw-alias-border-l2);padding:18px}
      @media(max-width:680px){.maestro-settings__grid{grid-template-columns:1fr}.maestro-settings__field--wide{grid-column:auto}}
    `;
    if (typeof document !== "undefined") {
      const tag = document.createElement("style");
      tag.dataset.plugin = "@maestro-care/dsh-client-ui-settings";
      tag.textContent = css;
      document.head.appendChild(tag);
    }

    const PROVIDER_URLS = Object.freeze({
      openrouter: "https://openrouter.ai/api/v1",
      deepseek: "https://api.deepseek.com",
      openai: "https://api.openai.com/v1",
      custom: "",
    });
    const SECRET_FIELDS = Object.freeze([
      "api_key",
      "memory_api_key",
      "platform_api_key",
      "mutation_api_key",
      "validation_api_key",
      "search_api_key",
      "upload_api_key",
    ]);

    function clearPendingSecretHandles(current) {
      const next = { ...current };
      for (const field of SECRET_FIELDS) next[`${field}_handle`] = "";
      return next;
    }

    function bridgeMethod(name) {
      const api = globalThis.pywebview?.api;
      const method = api?.[name];
      if (typeof method !== "function") {
        throw new Error("Native MAESTRO settings bridge is unavailable. Restart the native app.");
      }
      return method.bind(api);
    }

    function errorMessage(response, fallback) {
      const message = response?.payload?.error?.message;
      return typeof message === "string" && message.length > 0 ? message : fallback;
    }

    function initialForm(settings) {
      return {
        provider: settings.provider ?? "openrouter",
        base_url: settings.base_url ?? "",
        model: settings.model ?? "",
        generation_mode: settings.generation_mode ?? "fast",
        app_lock_mode: settings.app_lock_mode ?? "disabled",
        api_key_handle: "",
        clear_api_key: false,
        memory_base_url: settings.memory_base_url ?? "",
        memory_api_key_handle: "",
        clear_memory_api_key: false,
        platform_base_url: settings.platform_base_url ?? "",
        platform_api_key_handle: "",
        clear_platform_api_key: false,
        mutation_base_url: settings.mutation_base_url ?? "",
        mutation_model: settings.mutation_model ?? "",
        mutation_api_key_handle: "",
        clear_mutation_api_key: false,
        validation_base_url: settings.validation_base_url ?? "",
        validation_model: settings.validation_model ?? "",
        validation_api_key_handle: "",
        clear_validation_api_key: false,
        search_provider: settings.search_provider ?? "duckduckgo",
        search_api_key_handle: "",
        clear_search_api_key: false,
        upload_api_key_handle: "",
        clear_upload_api_key: false,
        enable_web_research: settings.enable_web_research === true,
        enable_memory_research: settings.enable_memory_research === true,
      };
    }

    function Field({ label, wide, hint, children }) {
      return h("label", { className: `maestro-settings__field${wide ? " maestro-settings__field--wide" : ""}` },
        h("span", { className: "maestro-settings__label" }, label),
        children,
        hint ? h("span", { className: "maestro-settings__hint" }, hint) : null,
      );
    }

    function MaestroSettingsSection() {
      const [snapshot, setSnapshot] = useState(null);
      const [form, setForm] = useState(null);
      const [phase, setPhase] = useState("loading");
      const [error, setError] = useState("");
      const [notice, setNotice] = useState("");
      const [review, setReview] = useState(null);
      const [restartRequired, setRestartRequired] = useState(false);

      const load = async () => {
        setPhase("loading");
        setError("");
        try {
          const response = await bridgeMethod("maestro_settings_snapshot")();
          if (response?.status !== 200) throw new Error(errorMessage(response, "Settings could not be loaded."));
          setSnapshot(response.payload);
          setForm(initialForm(response.payload.settings ?? {}));
          setPhase("editing");
        } catch (caught) {
          setError(caught instanceof Error ? caught.message : "Settings could not be loaded.");
          setPhase("error");
        }
      };

      useEffect(() => { void load(); }, []);

      const update = (field, value) => setForm(current => ({ ...current, [field]: value }));
      const payload = () => {
        const value = {
          provider: form.provider,
          base_url: form.base_url,
          model: form.model,
          generation_mode: form.generation_mode,
          app_lock_mode: form.app_lock_mode,
          api_key_action: form.api_key_handle ? "replace" : (form.clear_api_key ? "clear" : "keep"),
          memory_base_url: form.memory_base_url,
          memory_api_key_action: form.memory_api_key_handle ? "replace" : (form.clear_memory_api_key ? "clear" : "keep"),
          platform_base_url: form.platform_base_url,
          platform_api_key_action: form.platform_api_key_handle ? "replace" : (form.clear_platform_api_key ? "clear" : "keep"),
          mutation_base_url: form.mutation_base_url,
          mutation_model: form.mutation_model,
          mutation_api_key_action: form.mutation_api_key_handle ? "replace" : (form.clear_mutation_api_key ? "clear" : "keep"),
          validation_base_url: form.validation_base_url,
          validation_model: form.validation_model,
          validation_api_key_action: form.validation_api_key_handle ? "replace" : (form.clear_validation_api_key ? "clear" : "keep"),
          search_provider: form.search_provider,
          search_api_key_action: form.search_api_key_handle ? "replace" : (form.clear_search_api_key ? "clear" : "keep"),
          upload_api_key_action: form.upload_api_key_handle ? "replace" : (form.clear_upload_api_key ? "clear" : "keep"),
          enable_web_research: form.enable_web_research,
          enable_memory_research: form.enable_memory_research,
        };
        for (const field of SECRET_FIELDS) {
          const handle = form[`${field}_handle`];
          if (handle) value[`${field}_handle`] = handle;
        }
        return value;
      };

      const captureSecret = async (field) => {
        setError("");
        setNotice("");
        try {
          const response = await bridgeMethod("maestro_settings_capture_secret")(field);
          if (response?.status !== 200 || typeof response?.payload?.secret_handle !== "string") {
            throw new Error(errorMessage(response, "Credential could not be captured."));
          }
          const handleField = `${field}_handle`;
          const clearField = `clear_${field}`;
          setForm(current => ({ ...current, [handleField]: response.payload.secret_handle, [clearField]: false }));
          setNotice("Credential captured by the native secure dialog. Review and save before the handle expires.");
        } catch (caught) {
          setError(caught instanceof Error ? caught.message : "Credential could not be captured.");
        }
      };

      const secretField = (label, field, presentField, clearField) => {
        const handleField = `${field}_handle`;
        const replacementReady = Boolean(form[handleField]);
        return h(React.Fragment, null,
        h(Field, {
          label,
          wide: true,
          hint: "Keys are entered only in a native secure dialog and never enter this page or JavaScript state.",
        }, h("div", { className: "maestro-settings__secret-row" },
          h("button", {
            className: "maestro-settings__secondary",
            type: "button",
            onClick: () => { void captureSecret(field); },
          }, snapshot?.settings?.[presentField] ? "Replace in native dialog" : "Set in native dialog"),
          h("span", { className: "maestro-settings__key-state" }, replacementReady
            ? "Replacement captured — pending save"
            : (snapshot?.settings?.[presentField] ? "Configured in Keychain" : "Not configured")),
        )),
        snapshot?.settings?.[presentField] ? h("label", { className: "maestro-settings__check maestro-settings__field--wide" },
          h("input", {
            type: "checkbox",
            checked: form[clearField],
            disabled: replacementReady,
            onChange: event => update(clearField, event.target.checked),
          }),
          "Remove the stored key",
        ) : null,
      );
      };

      const preview = async (event) => {
        event.preventDefault();
        setError("");
        setNotice("");
        setPhase("previewing");
        try {
          const response = await bridgeMethod("maestro_settings_preview")(payload());
          const confirmation = response?.payload?.confirmation;
          if (response?.status !== 409 || confirmation?.token === undefined) {
            throw new Error(errorMessage(response, "Settings preview failed."));
          }
          setReview({
            token: confirmation.token,
            changes: Array.isArray(response.payload.changes) ? response.payload.changes : [],
          });
          setPhase("review");
        } catch (caught) {
          setError(caught instanceof Error ? caught.message : "Settings preview failed.");
          setPhase("editing");
        }
      };

      const save = async () => {
        if (!review) return;
        setError("");
        setPhase("saving");
        try {
          const response = await bridgeMethod("maestro_settings_save")(payload(), review.token);
          if (response?.status !== 200) throw new Error(errorMessage(response, "Settings could not be saved."));
          const next = response.payload.settings ?? {};
          setSnapshot({ ...snapshot, ...response.payload, settings: next });
          setForm(initialForm(next));
          setReview(null);
          setRestartRequired(response.payload.restart_required === true);
          setNotice("MAESTRO settings saved.");
          setPhase("editing");
        } catch (caught) {
          setError(caught instanceof Error ? caught.message : "Settings could not be saved.");
          setForm(clearPendingSecretHandles);
          setReview(null);
          setPhase("editing");
        }
      };

      if (phase === "loading") return h("div", { className: "maestro-settings" }, "Loading MAESTRO settings…");
      if (phase === "error" || !form) return h("div", { className: "maestro-settings" },
        h("div", { className: "maestro-settings__notice maestro-settings__notice--error", role: "alert" }, error),
        h("div", { className: "maestro-settings__actions" }, h("button", { className: "maestro-settings__secondary", type: "button", onClick: load }, "Retry")),
      );

      return h("form", { className: "maestro-settings", onSubmit: preview },
        h("header", { className: "maestro-settings__hero" },
          h("div", { className: "maestro-settings__eyebrow" }, "MAESTRO"),
          h("h2", null, "Agent and chain runtime"),
          h("p", { className: "maestro-settings__lead" }, "Configure the model used to generate MAESTRO task chains and the native app lock. Chat model settings remain in the separate Chat model section."),
        ),
        restartRequired ? h("div", {
          className: "maestro-settings__notice maestro-settings__notice--restart",
          role: "status",
        }, "Restart required: fully quit and reopen MAESTRO before the current agent uses the saved provider, model, credentials, or runtime settings.") : null,
        h("section", { className: "maestro-settings__card" },
          h("h3", null, "Chain generation"),
          h("div", { className: "maestro-settings__grid" },
            h(Field, { label: "Provider" }, h("select", {
              value: form.provider,
              onChange: event => {
                const provider = event.target.value;
                setForm(current => ({ ...current, provider, base_url: PROVIDER_URLS[provider] ?? current.base_url }));
              },
            }, (snapshot?.choices?.provider ?? Object.keys(PROVIDER_URLS)).map(value => h("option", { key: value, value }, value)))),
            h(Field, { label: "Generation mode" }, h("select", { value: form.generation_mode, onChange: event => update("generation_mode", event.target.value) },
              (snapshot?.choices?.generation_mode ?? ["fast", "deep"]).map(value => h("option", { key: value, value }, value)))),
            h(Field, { label: "Base URL", wide: true }, h("input", { type: "url", value: form.base_url, autoComplete: "url", onChange: event => update("base_url", event.target.value), placeholder: "https://api.example.com/v1" })),
            h(Field, { label: "Model ID", wide: true }, h("input", { type: "text", value: form.model, autoComplete: "off", onChange: event => update("model", event.target.value), placeholder: "provider/model" })),
            secretField("Generation API key", "api_key", "api_key_present", "clear_api_key"),
          ),
        ),
        h("section", { className: "maestro-settings__card" },
          h("h3", null, "Research and Memory"),
          h("div", { className: "maestro-settings__grid" },
            h(Field, { label: "Memory base URL", wide: true }, h("input", { type: "url", value: form.memory_base_url, autoComplete: "url", onChange: event => update("memory_base_url", event.target.value) })),
            secretField("Memory API key", "memory_api_key", "memory_api_key_present", "clear_memory_api_key"),
            h(Field, { label: "Web search provider", wide: true }, h("select", { value: form.search_provider, onChange: event => update("search_provider", event.target.value) },
              (snapshot?.choices?.search_provider ?? ["duckduckgo"]).map(value => h("option", { key: value, value }, value)))),
            secretField("Web search API key", "search_api_key", "search_api_key_present", "clear_search_api_key"),
          ),
          h("div", { className: "maestro-settings__toggle-grid" },
            h("label", { className: "maestro-settings__toggle" }, h("input", { type: "checkbox", checked: form.enable_web_research, onChange: event => update("enable_web_research", event.target.checked) }), "Enable web research"),
            h("label", { className: "maestro-settings__toggle" }, h("input", { type: "checkbox", checked: form.enable_memory_research, onChange: event => update("enable_memory_research", event.target.checked) }), "Enable memory research"),
          ),
        ),
        h("details", { className: "maestro-settings__card" },
          h("summary", null, "Platform, evolution and validation"),
          h("div", { className: "maestro-settings__details" },
            h("div", { className: "maestro-settings__grid" },
              h(Field, { label: "Platform base URL", wide: true }, h("input", { type: "url", value: form.platform_base_url, autoComplete: "url", onChange: event => update("platform_base_url", event.target.value) })),
              secretField("Platform API key", "platform_api_key", "platform_api_key_present", "clear_platform_api_key"),
              h(Field, { label: "Mutation base URL", wide: true }, h("input", { type: "url", value: form.mutation_base_url, autoComplete: "url", onChange: event => update("mutation_base_url", event.target.value) })),
              h(Field, { label: "Mutation model", wide: true }, h("input", { type: "text", value: form.mutation_model, autoComplete: "off", onChange: event => update("mutation_model", event.target.value) })),
              secretField("Mutation API key", "mutation_api_key", "mutation_api_key_present", "clear_mutation_api_key"),
              h(Field, { label: "Validation base URL", wide: true }, h("input", { type: "url", value: form.validation_base_url, autoComplete: "url", onChange: event => update("validation_base_url", event.target.value) })),
              h(Field, { label: "Validation model", wide: true }, h("input", { type: "text", value: form.validation_model, autoComplete: "off", onChange: event => update("validation_model", event.target.value) })),
              secretField("Validation API key", "validation_api_key", "validation_api_key_present", "clear_validation_api_key"),
              secretField("Upload API key", "upload_api_key", "upload_api_key_present", "clear_upload_api_key"),
            ),
          ),
        ),
        h("section", { className: "maestro-settings__card" },
          h("h3", null, "Unlock on launch"),
          h(Field, { label: "App lock" }, h("select", {
            value: form.app_lock_mode,
            disabled: snapshot?.storage?.app_lock_overridden_by_environment === true,
            onChange: event => update("app_lock_mode", event.target.value),
          }, [
            h("option", { key: "disabled", value: "disabled" }, "Off"),
            h("option", { key: "touch-id", value: "touch-id" }, "Touch ID only"),
          ])),
          h("p", { className: "maestro-settings__hint maestro-settings__lock-note" }, snapshot?.storage?.app_lock_overridden_by_environment
            ? "Controlled by MAESTRO_APP_LOCK_MODE for this launch."
            : "The choice is stored locally and takes effect on the next launch. Password fallback is disabled."),
        ),
        phase === "review" && review ? h("section", { className: "maestro-settings__card", "aria-live": "polite" },
          h("h3", null, "Review changes"),
          h("ul", { className: "maestro-settings__changes" }, review.changes.map((change, index) => h("li", { key: index }, change))),
          h("p", { className: "maestro-settings__hint", style: { marginTop: "12px" } }, "Confirming opens a native MAESTRO approval dialog. No browser plugin can save silently."),
        ) : null,
        error ? h("div", { className: "maestro-settings__notice maestro-settings__notice--error", role: "alert" }, error) : null,
        notice ? h("div", { className: "maestro-settings__notice maestro-settings__notice--success", role: "status" }, notice) : null,
        h("p", { className: "maestro-settings__hint", style: { marginTop: "13px" } }, "After any save, fully quit and reopen MAESTRO. The current agent continues using the provider, model, credentials, and runtime settings loaded at startup."),
        h("div", { className: "maestro-settings__actions" },
          phase === "review" ? h("button", { className: "maestro-settings__secondary", type: "button", onClick: () => { setForm(clearPendingSecretHandles); setReview(null); setPhase("editing"); } }, "Back") : null,
          phase === "review"
            ? h("button", { className: "maestro-settings__primary", type: "button", onClick: save }, "Confirm in native dialog")
            : h("button", { className: "maestro-settings__primary", type: "submit", disabled: phase === "previewing" || phase === "saving" }, phase === "previewing" ? "Checking…" : "Review save"),
        ),
      );
    }

    const inject = ["slots"];
    function apply(ctx) {
      ctx.slots.inject("settings.section", () => ctx.slots.register({
        name: "settings.section",
        id: "maestro",
        order: 5,
        label: () => "MAESTRO",
      }, MaestroSettingsSection));
    }

    exports.MaestroSettingsSection = MaestroSettingsSection;
    exports.apply = apply;
    exports.inject = inject;
    return module.exports;
  },
});
