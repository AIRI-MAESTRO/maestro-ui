from __future__ import annotations

import json
import shutil
import subprocess
import tomllib
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
INTEGRATION = ROOT / "integrations" / "deepseek-harness-maestro-branding"
CLIENT = INTEGRATION / "lib" / "client.js"


def test_manifest_exports_a_public_branding_client() -> None:
    manifest = json.loads((INTEGRATION / "package.json").read_text())
    version = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"][
        "version"
    ]

    assert manifest["name"] == "@maestro-care/dsh-client-ui-branding"
    assert manifest["version"] == version
    assert manifest["main"] == "./index.mjs"
    assert manifest["exports"]["./client"] == "./lib/client.js"
    assert manifest["dsh"]["client"] == {
        "inject": [
            "@deepseek-ai/dsh-client-ui-renderer",
            "@deepseek-ai/dsh-client-ui-sidebar",
            "@deepseek-ai/dsh-client-ui-conversation",
            "@deepseek-ai/dsh-client-ui-theme",
        ],
        "platform": "web",
    }
    assert CLIENT.is_file()


def test_client_uses_public_slots_and_paired_maestro_theme_tokens() -> None:
    client = CLIENT.read_text()

    for required in (
        'ctx.slots.inject("sidebar.brand.mark"',
        'ctx.slots.inject("sidebar.brand.name"',
        'ctx.slots.inject("conversation.hero.brand.mark"',
        "priority: -100",
        "ctx.theme.overrideTokens(PACKAGE_ID, THEME_TOKENS)",
        'light: "#faf6ef", dark: "#0f1115"',
        'light: "#2a7d6f", dark: "#33d9b2"',
        'title.textContent = "MAESTRO"',
        'badge.textContent = "AGENTIC HARNESS"',
        "new MutationObserver(schedule)",
        "pendingRoots",
        "setTimeout(flush, 0)",
        "filter:blur(32px)",
        "height:86px",
        "width:min(440px,64vw)",
    ):
        assert required in client

    for forbidden in (
        "FishLogo",
        "FISH_LOGO_PATH",
        "innerHTML",
        "pywebview",
        "api.deepseek.com",
        "height:180px",
        "width:min(610px,78vw)",
    ):
        assert forbidden not in client


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is not installed")
def test_prebuilt_client_registers_brand_slots_theme_and_copy_rewrite() -> None:
    script = f"""
      globalThis.window = {{
        __ModuleLoader__: {{
          load(descriptor) {{ globalThis.descriptor = descriptor }},
        }},
      }}

      function text(value) {{
        return {{ nodeType: 3, nodeValue: value, childNodes: [], isConnected: true }}
      }}
      function element(tagName, value = '') {{
        const node = {{
          nodeType: 1,
          tagName,
          childNodes: value === '' ? [] : [text(value)],
          dataset: {{}},
          parentElement: null,
          parentNode: null,
          nextElementSibling: null,
          attributes: {{}},
          setAttribute(name, value) {{ this.attributes[name] = value }},
          remove() {{ this.removed = true }},
          closest() {{ return this.dialog ?? null }},
        }}
        Object.defineProperty(node, 'textContent', {{
          get() {{ return this.childNodes.map(child => child.nodeValue ?? child.textContent ?? '').join('') }},
          set(value) {{ this.childNodes = [text(value)] }},
        }})
        return node
      }}

      const slot = element('SPAN')
      const mark = element('SPAN')
      const hero = element('DIV')
      const title = element('SPAN', 'Into the Unknown')
      const badge = element('SPAN', 'Preview')
      slot.parentElement = mark
      mark.parentElement = hero
      mark.nextElementSibling = title
      title.nextElementSibling = badge
      const welcomeTitle = element('H2', 'Internal Testing Notice')
      const dialog = element('DIV')
      welcomeTitle.dialog = dialog
      const bodyCopy = element('P', 'Built on DeepSeek Harness for agents.')
      const providerCopy = element('P', 'DeepSeek API provider')
      const onboardingCopy = element('P', 'Configure the official DeepSeek provider to start building.')
      const codeCopy = element('CODE', '@deepseek-ai/dsh — DeepSeek Harness')
      const body = element('BODY')
      body.childNodes = [welcomeTitle, bodyCopy, providerCopy, onboardingCopy, codeCopy]
      const head = element('HEAD')
      head.appendChild = node => {{ node.parentNode = head }}
      const root = element('HTML')
      const created = []
      globalThis.document = {{
        body,
        head,
        documentElement: root,
        title: 'DeepSeek Harness',
        createElement(tagName) {{ const node = element(tagName.toUpperCase()); created.push(node); return node }},
        getElementById() {{ return null }},
        querySelector(selector) {{
          return selector === '[data-slot="conversation.hero.brand.mark"]' ? slot : null
        }},
        querySelectorAll(selector) {{
          return selector === 'h1,h2,h3' ? [welcomeTitle] : []
        }},
      }}
      globalThis.MutationObserver = class {{
        constructor(callback) {{ this.callback = callback }}
        observe(target, options) {{ this.target = target; this.options = options }}
        disconnect() {{ this.disconnected = true }}
      }}

      await import({json.dumps(CLIENT.as_uri())})
      const React = {{ createElement(type, props, ...children) {{ return {{ type, props: props ?? {{}}, children }} }} }}
      const plugin = globalThis.descriptor.factory(name => {{
        if (name === 'react') return React
        throw new Error(`unexpected dependency: ${{name}}`)
      }})
      const registrations = []
      const effects = []
      let themeSource
      let themeTokens
      const ctx = {{
        effect(callback) {{ effects.push(callback()); return () => {{}} }},
        theme: {{
          overrideTokens(source, tokens) {{ themeSource = source; themeTokens = tokens; return () => {{}} }},
        }},
        slots: {{
          inject(name, callback) {{ callback() }},
          register(options, component) {{ registrations.push({{ options, component }}); return () => {{}} }},
        }},
      }}
      plugin.apply(ctx)
      const rendered = registrations.map(entry => entry.component({{ size: 24 }}))
      process.stdout.write(JSON.stringify({{
        id: globalThis.descriptor.id,
        inject: plugin.inject,
        slots: registrations.map(entry => entry.options.name),
        priorities: registrations.map(entry => entry.options.priority),
        renderedTypes: rendered.map(node => node.type),
        sidebarName: rendered[1].children[0],
        themeSource,
        lightBg: themeTokens['--dsw-alias-bg-base'].light,
        darkBg: themeTokens['--dsw-alias-bg-base'].dark,
        lightAccent: themeTokens['--dsw-alias-brand-primary'].light,
        darkAccent: themeTokens['--dsw-alias-brand-primary'].dark,
        title: title.textContent,
        badge: badge.textContent,
        pageTitle: document.title,
        welcomeTitle: welcomeTitle.textContent,
        bodyCopy: bodyCopy.textContent,
        providerCopy: providerCopy.textContent,
        onboardingCopy: onboardingCopy.textContent,
        codeCopy: codeCopy.textContent,
        heroLabel: hero.attributes['aria-label'],
        welcomeDecorated: dialog.dataset.maestroWelcome,
        styleInstalled: created.some(node => node.id === 'maestro-client-branding'),
      }}))
    """
    completed = subprocess.run(
        ["node", "--input-type=module", "--eval", script],
        check=True,
        capture_output=True,
        text=True,
        timeout=20,
    )

    assert json.loads(completed.stdout) == {
        "id": "@maestro-care/dsh-client-ui-branding",
        "inject": ["slots", "theme"],
        "slots": [
            "sidebar.brand.mark",
            "sidebar.brand.name",
            "conversation.hero.brand.mark",
        ],
        "priorities": [-100, -100, -100],
        "renderedTypes": ["svg", "span", "svg"],
        "sidebarName": "MAESTRO",
        "themeSource": "@maestro-care/dsh-client-ui-branding",
        "lightBg": "#faf6ef",
        "darkBg": "#0f1115",
        "lightAccent": "#2a7d6f",
        "darkAccent": "#33d9b2",
        "title": "MAESTRO",
        "badge": "AGENTIC HARNESS",
        "pageTitle": "MAESTRO",
        "welcomeTitle": "Welcome to MAESTRO",
        "bodyCopy": "Built on MAESTRO for agents.",
        "providerCopy": "DeepSeek API provider",
        "onboardingCopy": "Configure a chat provider now, or continue and choose one later in Settings.",
        "codeCopy": "@deepseek-ai/dsh — DeepSeek Harness",
        "heroLabel": "MAESTRO — Agentic Harness",
        "welcomeDecorated": "true",
        "styleInstalled": True,
    }
