window.__ModuleLoader__.load({
  id: "@maestro-care/dsh-client-ui-branding",
  factory: (require) => {
    var module = { exports: {} };
    var exports = module.exports;
    Object.defineProperty(exports, Symbol.toStringTag, { value: "Module" });

    const React = require("react");
    const { createElement: h } = React;
    const PACKAGE_ID = "@maestro-care/dsh-client-ui-branding";
    const STYLE_ID = "maestro-client-branding";

    const THEME_TOKENS = Object.freeze({
      "--maestro-bg": { light: "#faf6ef", dark: "#0f1115" },
      "--maestro-surface": { light: "#fffdf8", dark: "#1f242c" },
      "--maestro-surface-2": { light: "#fff9f0", dark: "#252b35" },
      "--maestro-text": { light: "#2a2520", dark: "#e6e6e6" },
      "--maestro-muted": { light: "#6b5f52", dark: "#a0a8b4" },
      "--maestro-accent": { light: "#2a7d6f", dark: "#33d9b2" },
      "--maestro-crystal": { light: "#4db8a4", dark: "#7ef0dd" },
      "--maestro-cream": { light: "#e8c89a", dark: "#c8e8e0" },
      "--maestro-glow": { light: "rgba(42,125,111,.24)", dark: "rgba(51,217,178,.30)" },

      "--dsw-static-deepseek-50": { light: "#eff7f3", dark: "#e7fff9" },
      "--dsw-static-deepseek-100": { light: "#dceee9", dark: "#c8f8ec" },
      "--dsw-static-deepseek-200": { light: "#bfe3da", dark: "#98ead7" },
      "--dsw-static-deepseek-300": { light: "#88d2c2", dark: "#69ddc2" },
      "--dsw-static-deepseek-400": { light: "#4db8a4", dark: "#33d9b2" },
      "--dsw-static-deepseek-450": { light: "#36a892", dark: "#33d9b2" },
      "--dsw-static-deepseek-500": { light: "#2a7d6f", dark: "#2ebfae" },
      "--dsw-static-deepseek-600": { light: "#1f6358", dark: "#259b8e" },
      "--dsw-static-deepseek-800": { light: "#23423e", dark: "#173c38" },
      "--dsw-static-deepseek-900": { light: "#17312e", dark: "#142c2a" },

      "--dsw-alias-bg-base": { light: "#faf6ef", dark: "#0f1115" },
      "--dsw-alias-bg-layer-1": { light: "#fffdf8", dark: "#1f242c" },
      "--dsw-alias-bg-layer-2": { light: "#fff9f0", dark: "#252b35" },
      "--dsw-alias-bg-layer-3": { light: "#f4ebe0", dark: "#2d3540" },
      "--dsw-alias-bg-module-platform": { light: "#f4ebe0", dark: "#252b35" },
      "--dsw-alias-bg-multi-select": { light: "#f4ebe0", dark: "#252b35" },
      "--dsw-alias-bg-overlay": { light: "#eadcc9", dark: "#3d4653" },
      "--dsw-alias-border-l1": { light: "rgba(42,37,32,.06)", dark: "rgba(126,240,221,.07)" },
      "--dsw-alias-border-l2": { light: "rgba(42,37,32,.10)", dark: "rgba(126,240,221,.12)" },
      "--dsw-alias-border-l2-darkmode-thin": { light: "rgba(42,37,32,.10)", dark: "rgba(126,240,221,.12)" },
      "--dsw-alias-border-l3": { light: "rgba(42,37,32,.14)", dark: "rgba(126,240,221,.17)" },
      "--dsw-alias-border-l4": { light: "rgba(42,37,32,.20)", dark: "rgba(126,240,221,.24)" },
      "--dsw-alias-brand-primary": { light: "#2a7d6f", dark: "#33d9b2" },
      "--dsw-alias-brand-primary-new-colorprimary-new-color": { light: "#2a7d6f", dark: "#33d9b2" },
      "--dsw-alias-brand-text": { light: "#2a2520", dark: "#e6e6e6" },
      "--dsw-alias-button-info-fill": { light: "#2a7d6f", dark: "#33d9b2" },
      "--dsw-alias-button-info-hover": { light: "#1f6358", dark: "#2ebfae" },
      "--dsw-alias-button-primary-fill": { light: "#2a7d6f", dark: "#33d9b2" },
      "--dsw-alias-button-primary-hover": { light: "#1f6358", dark: "#2ebfae" },
      "--dsw-alias-button-primary-dimmed": { light: "#dceee9", dark: "#23423e" },
      "--dsw-alias-interactive-bg-active": { light: "rgba(42,125,111,.14)", dark: "rgba(51,217,178,.16)" },
      "--dsw-alias-interactive-bg-hover": { light: "rgba(42,125,111,.08)", dark: "rgba(51,217,178,.10)" },
      "--dsw-alias-interactive-bg-hover-accent": { light: "rgba(42,125,111,.16)", dark: "rgba(51,217,178,.22)" },
      "--dsw-alias-interactive-bg-hover-solid": { light: "#f4ebe0", dark: "#252b35" },
      "--dsw-alias-label-caption": { light: "#9a8d7e", dark: "#707a88" },
      "--dsw-alias-label-dimmed": { light: "#d2c4b3", dark: "#48515e" },
      "--dsw-alias-label-primary": { light: "#2a2520", dark: "#e6e6e6" },
      "--dsw-alias-label-primary-bluish": { light: "#1f6358", dark: "#c8f5ec" },
      "--dsw-alias-label-primary-dimmed": { light: "#413a33", dark: "#c8d0d8" },
      "--dsw-alias-label-secondary": { light: "#5b5147", dark: "#c5cbd3" },
      "--dsw-alias-label-tertiary": { light: "#6b5f52", dark: "#a0a8b4" },
      "--dsw-alias-state-business-primary": { light: "#2a7d6f", dark: "#33d9b2" },
      "--dsw-alias-state-business-tertiary": { light: "#dceee9", dark: "#173c38" },
      "--dsw-alias-state-success-primary": { light: "#2a7d6f", dark: "#33d9b2" },
      "--dsw-alias-markdown-code-block-banner": { light: "#f4ebe0", dark: "#1f242c" },
      "--dsw-alias-markdown-code-block": { light: "#fff9f0", dark: "#181c22" },
      "--dsw-alias-markdown-inline-code": { light: "#f4ebe0", dark: "#252b35" },
      "--dsw-alias-markdown-placeholder": { light: "#fff9f0", dark: "#181c22" },
      "--dsw-alias-markdown-tag": { light: "#f4ebe0", dark: "#252b35" },
      "--dsw-specific-bubble": { light: "#eff7f3", dark: "#18312f" },
      "--dsw-specific-bubble-highlight": { light: "#dceee9", dark: "#244d47" },
      "--dsw-specific-input-major": { light: "#fffdf8", dark: "#1f242c" },
      "--dsw-specific-login-input": { light: "#fff9f0", dark: "#181c22" },
      "--dsw-specific-menu": { light: "#fffdf8", dark: "#252b35" },
      "--dsw-specific-selector": { light: "#f4ebe0", dark: "#252b35" },
      "--dsw-specific-sidebar-fill": { light: "#f4ebe0", dark: "#181c22" },
      "--dsw-specific-sidebar-nav-item-active-accent": { light: "#dceee9", dark: "#173c38" },
      "--dsw-specific-sidebar-nav-item-active": { light: "#eadcc9", dark: "#2d3540" },
      "--dsw-specific-sidebar-nav-item-hover": { light: "#eee3d5", dark: "#1f242c" },
      "--dsw-specific-tip": { light: "#fff9f0", dark: "#252b35" }
    });

    const WELCOME_COPY = Object.freeze(new Map([
      ["Internal Testing Notice", "Welcome to MAESTRO"],
      ["DeepSeek Harness 0.1 remains in testing for Harness developers. Many areas need further improvement, and we welcome feedback from the developer community. DeepSeek Harness's core plugins and foundational APIs will continue to evolve rapidly over the coming months.", "MAESTRO is ready for everyday conversations, tool use, chain generation, execution, memory, and evolution. This native build remains under active development, and its interfaces and plugin contracts will continue to improve."],
      ["We look forward to exploring the limits of intelligence with developers around the world, building on open-source, open, reusable, and composable infrastructure. We welcome Harness developers everywhere to join the DSH plugin ecosystem.", "Tell MAESTRO what you need. It can chat directly, adapt inputs to chain contracts, use enabled search tools, and run approved workflows while keeping work visible and cancellable."],
      ["Configure the official DeepSeek provider to start building.", "Configure a chat provider now, or continue and choose one later in Settings."],
      ["内测声明", "欢迎使用 MAESTRO"],
      ["DeepSeek Harness 目前的 0.1 版本仍处在面向 Harness 开发者进行测试的阶段，还有许多地方需要持续改进和打磨，希望听取广大开发者的反馈建议。预计 DeepSeek Harness 的核心插件以及基础 API 都会在接下来的一段时间内快速迭代、持续演化。", "MAESTRO 可用于日常对话、工具调用、任务链生成与执行、记忆和进化。此原生版本仍在持续开发，界面与插件契约会继续完善。"],
      ["我们期待与全球开发者一起，在开源、开放、可复用、可组合的基础设施之上，共同探索智能上限。欢迎全球 Harness 开发者加入 DSH 插件生态。", "直接告诉 MAESTRO 你的目标。它可以对话、将输入适配到任务链契约、使用已启用的搜索工具，并运行经过批准且可取消的工作流。"]
    ]));

    const SKIPPED_COPY_TAGS = new Set(["CODE", "PRE", "SCRIPT", "STYLE", "OPTION", "INPUT", "TEXTAREA"]);

    const css = `
      body{isolation:isolate;background:var(--dsw-alias-bg-base)}
      body:before{background:radial-gradient(58rem 35rem at 82% -8%,color-mix(in srgb,var(--maestro-crystal) 11%,transparent),transparent 70%),radial-gradient(42rem 30rem at 4% 104%,color-mix(in srgb,var(--maestro-cream) 11%,transparent),transparent 72%);content:"";inset:0;pointer-events:none;position:fixed;z-index:-1}
      .maestro-brand-mark{display:block;flex:none;filter:drop-shadow(0 5px 13px var(--maestro-glow));overflow:visible}
      .maestro-brand-mark__tile{fill:var(--maestro-accent)}
      .maestro-brand-name{align-items:center;color:var(--dsw-alias-label-primary);display:inline-flex;font-size:15px;font-weight:780;letter-spacing:.12em;line-height:24px;white-space:nowrap}
      [data-slot="sidebar.brand.mark"] .maestro-brand-mark{border-radius:8px;height:24px;width:24px}
      [data-slot="conversation.hero.brand.mark"] .maestro-brand-mark{border-radius:11px;height:34px;width:34px}
      [data-maestro-hero]{column-gap:12px!important;isolation:isolate;position:relative}
      [data-maestro-hero]:before{background:radial-gradient(circle at 28% 54%,color-mix(in srgb,var(--maestro-accent) 14%,transparent) 0,color-mix(in srgb,var(--maestro-accent) 6%,transparent) 24%,transparent 62%),radial-gradient(circle at 74% 46%,color-mix(in srgb,var(--maestro-cream) 10%,transparent) 0,color-mix(in srgb,var(--maestro-cream) 4%,transparent) 20%,transparent 58%);border-radius:50%;content:"";filter:blur(32px);height:86px;left:50%;opacity:.72;pointer-events:none;position:absolute;top:50%;transform:translate(-50%,-50%);width:min(440px,64vw);z-index:-1}
      [data-maestro-hero-title]{font-weight:760!important;letter-spacing:.09em}
      [data-maestro-hero-badge]{background:color-mix(in srgb,var(--maestro-accent) 11%,var(--maestro-surface))!important;border-color:color-mix(in srgb,var(--maestro-crystal) 50%,transparent)!important;box-shadow:0 7px 22px color-mix(in srgb,var(--maestro-accent) 10%,transparent);color:var(--maestro-accent)!important;font-size:10px!important;font-weight:700!important;letter-spacing:.09em!important;padding:2px 8px 1px!important}
      [data-maestro-welcome]{border:1px solid color-mix(in srgb,var(--maestro-accent) 25%,transparent)!important;box-shadow:0 28px 80px color-mix(in srgb,var(--maestro-accent) 14%,transparent)!important}
      @media(prefers-reduced-motion:no-preference){[data-slot="conversation.hero.brand.mark"] .maestro-brand-mark{animation:maestro-mark-float 3.6s ease-in-out infinite}}
      @keyframes maestro-mark-float{0%,100%{transform:translateY(0)}50%{transform:translateY(-3px)}}
    `;

    function MaestroMark({ size = 24, className = "" }) {
      const edge = Number.isFinite(Number(size)) ? Number(size) : 24;
      return h("svg", {
        "aria-hidden": "true",
        className: `maestro-brand-mark ${className}`.trim(),
        focusable: "false",
        height: edge,
        viewBox: "0 0 48 48",
        width: edge,
      },
      h("rect", { className: "maestro-brand-mark__tile", height: 48, rx: 14, width: 48 }),
      h("path", {
        d: "M11.5 32.5V15.5L24 27.75 36.5 15.5V32.5",
        fill: "none",
        stroke: "#fff",
        strokeLinecap: "round",
        strokeLinejoin: "round",
        strokeWidth: 3.25,
      }),
      h("circle", { cx: 24, cy: 35.5, fill: "#fff", r: 2 }));
    }

    function MaestroName() {
      return h("span", { className: "maestro-brand-name" }, "MAESTRO");
    }

    function preserveWhitespace(raw, replacement) {
      const match = /^(\s*)([\s\S]*?)(\s*)$/.exec(raw);
      return match === null ? replacement : `${match[1]}${replacement}${match[3]}`;
    }

    function productCopy(raw) {
      const trimmed = raw.trim();
      const exact = WELCOME_COPY.get(trimmed);
      if (exact !== undefined) return preserveWhitespace(raw, exact);
      if (!raw.includes("DeepSeek Harness") && !raw.includes("DSH Local Build")) return raw;
      return raw.replaceAll("DeepSeek Harness", "MAESTRO").replaceAll("DSH Local Build", "MAESTRO");
    }

    function rewriteVisibleCopy(node, changed) {
      if (node === null || node === undefined) return;
      if (node.nodeType === 3) {
        const before = typeof node.nodeValue === "string" ? node.nodeValue : "";
        const after = productCopy(before);
        if (after !== before) {
          if (!changed.has(node)) changed.set(node, before);
          node.nodeValue = after;
        }
        return;
      }
      if (node.nodeType === 1 && SKIPPED_COPY_TAGS.has(String(node.tagName).toUpperCase())) return;
      for (const child of Array.from(node.childNodes ?? [])) rewriteVisibleCopy(child, changed);
    }

    function installPresentation(ctx) {
      if (typeof document === "undefined") return () => {};
      const changed = new Map();
      const changedElementText = new Map();
      const decoratedHeroes = new Map();
      const decoratedDialogs = new Set();
      const originalTitle = document.title;
      let style = document.getElementById(STYLE_ID);
      if (style === null) {
        style = document.createElement("style");
        style.id = STYLE_ID;
        (document.head ?? document.documentElement).appendChild(style);
      }
      style.textContent = css;

      const decorate = (roots = [document.body]) => {
        if (typeof document.title === "string") document.title = productCopy(document.title);
        const slot = document.querySelector?.('[data-slot="conversation.hero.brand.mark"]');
        const mark = slot?.parentElement;
        const hero = mark?.parentElement;
        const title = mark?.nextElementSibling;
        const badge = title?.nextElementSibling;
        if (mark !== undefined && mark !== null && hero !== undefined && hero !== null) {
          decoratedHeroes.set(hero, { hero, mark, title, badge });
          hero.dataset.maestroHero = "true";
          hero.setAttribute?.("role", "heading");
          hero.setAttribute?.("aria-level", "1");
          hero.setAttribute?.("aria-label", "MAESTRO — Agentic Harness");
          mark.dataset.maestroHeroMark = "true";
          if (title !== undefined && title !== null) {
            if (title.textContent?.trim() === "Into the Unknown") {
              if (!changedElementText.has(title)) changedElementText.set(title, title.textContent);
              title.textContent = "MAESTRO";
            }
            title.dataset.maestroHeroTitle = "true";
          }
          if (badge !== undefined && badge !== null) {
            if (badge.textContent?.trim() === "Preview") {
              if (!changedElementText.has(badge)) changedElementText.set(badge, badge.textContent);
              badge.textContent = "AGENTIC HARNESS";
            }
            badge.dataset.maestroHeroBadge = "true";
          }
        }
        for (const root of roots) rewriteVisibleCopy(root, changed);
        const welcomeTitle = Array.from(document.querySelectorAll?.("h1,h2,h3") ?? []).find(element => element.textContent?.trim() === "Welcome to MAESTRO" || element.textContent?.trim() === "欢迎使用 MAESTRO");
        const dialog = welcomeTitle?.closest?.('[role="dialog"]');
        if (dialog !== undefined && dialog !== null) {
          decoratedDialogs.add(dialog);
          dialog.dataset.maestroWelcome = "true";
        }
      };

      decorate();
      const pendingRoots = new Set();
      let scheduled = false;
      let timer = null;
      const flush = () => {
        scheduled = false;
        timer = null;
        const roots = Array.from(pendingRoots);
        pendingRoots.clear();
        decorate(roots.length > 0 ? roots : [document.body]);
      };
      const schedule = records => {
        for (const record of records ?? []) {
          if (record?.type === "characterData" && record.target != null) {
            pendingRoots.add(record.target);
          }
          for (const node of Array.from(record?.addedNodes ?? [])) pendingRoots.add(node);
        }
        if (scheduled) return;
        scheduled = true;
        timer = setTimeout(flush, 0);
      };
      const observer = typeof MutationObserver === "function" ? new MutationObserver(schedule) : null;
      observer?.observe(document.documentElement, { characterData: true, childList: true, subtree: true });
      return () => {
        observer?.disconnect();
        if (timer !== null) clearTimeout(timer);
        if (style?.parentNode !== null && style?.parentNode !== undefined) style.remove();
        for (const [node, value] of changed) {
          if (node?.isConnected !== false) node.nodeValue = value;
        }
        for (const [element, value] of changedElementText) {
          if (element?.isConnected !== false) element.textContent = value;
        }
        for (const { hero, mark, title, badge } of decoratedHeroes.values()) {
          delete hero?.dataset?.maestroHero;
          delete mark?.dataset?.maestroHeroMark;
          delete title?.dataset?.maestroHeroTitle;
          delete badge?.dataset?.maestroHeroBadge;
          if (hero?.getAttribute?.("aria-label") === "MAESTRO — Agentic Harness") {
            hero.removeAttribute?.("role");
            hero.removeAttribute?.("aria-level");
            hero.removeAttribute?.("aria-label");
          }
        }
        for (const dialog of decoratedDialogs) delete dialog?.dataset?.maestroWelcome;
        if (document.title === "MAESTRO" || document.title.includes("MAESTRO")) document.title = originalTitle;
      };
    }

    const inject = ["slots", "theme"];

    function apply(ctx) {
      ctx.effect(() => ctx.theme.overrideTokens(PACKAGE_ID, THEME_TOKENS), "maestro-branding: theme tokens");
      ctx.slots.inject("sidebar.brand.mark", () => ctx.slots.register({ name: "sidebar.brand.mark", priority: -100 }, MaestroMark));
      ctx.slots.inject("sidebar.brand.name", () => ctx.slots.register({ name: "sidebar.brand.name", priority: -100 }, MaestroName));
      ctx.slots.inject("conversation.hero.brand.mark", () => ctx.slots.register({ name: "conversation.hero.brand.mark", priority: -100 }, MaestroMark));
      ctx.effect(() => installPresentation(ctx), "maestro-branding: product copy and presentation");
    }

    exports.apply = apply;
    exports.inject = inject;
    return module.exports;
  }
});
