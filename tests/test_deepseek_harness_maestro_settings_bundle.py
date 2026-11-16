from __future__ import annotations

import json
import shutil
import subprocess
import tomllib
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
INTEGRATION = ROOT / "integrations" / "deepseek-harness-maestro-settings"


def test_manifest_exports_a_prebuilt_public_settings_slot_client() -> None:
    manifest = json.loads((INTEGRATION / "package.json").read_text())
    version = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"][
        "version"
    ]

    assert manifest["name"] == "@maestro-care/dsh-client-ui-settings"
    assert manifest["version"] == version
    assert manifest["main"] == "./index.mjs"
    assert manifest["exports"]["./client"] == "./lib/client.js"
    assert manifest["dsh"]["client"] == {
        "inject": ["@deepseek-ai/dsh-client-ui-settings"],
        "platform": "web",
        "immediately": True,
    }
    assert (INTEGRATION / "lib" / "client.js").is_file()


def test_client_owns_only_its_section_and_complete_maestro_settings_contract() -> None:
    client = (INTEGRATION / "lib" / "client.js").read_text()

    for required in (
        'ctx.slots.inject("settings.section"',
        'id: "maestro"',
        'label: () => "MAESTRO"',
        'bridgeMethod("maestro_settings_snapshot")',
        'bridgeMethod("maestro_settings_capture_secret")',
        'bridgeMethod("maestro_settings_preview")',
        'bridgeMethod("maestro_settings_save")',
        "provider",
        "base_url",
        "model",
        "memory_base_url",
        "memory_api_key_action",
        "platform_base_url",
        "platform_api_key_action",
        "mutation_base_url",
        "mutation_model",
        "mutation_api_key_action",
        "validation_base_url",
        "validation_model",
        "validation_api_key_action",
        "search_provider",
        "search_api_key_action",
        "upload_api_key_action",
        "enable_web_research",
        "enable_memory_research",
        "app_lock_mode",
        "restart_required",
        "fully quit and reopen MAESTRO",
        "MAESTRO task chains",
    ):
        assert required in client

    for forbidden in (
        "innerHTML",
        "localStorage",
        "sessionStorage",
        "MutationObserver",
        "querySelector",
        'type: "password"',
        'api_key: ""',
        "MAESTRO CARE",
        "CARL chains",
    ):
        assert forbidden not in client


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is not installed")
def test_prebuilt_client_registers_through_public_slot() -> None:
    client_path = INTEGRATION / "lib" / "client.js"
    script = f"""
      globalThis.window = {{
        __ModuleLoader__: {{
          load(descriptor) {{ globalThis.descriptor = descriptor }},
        }},
      }}
      await import({json.dumps(client_path.as_uri())})
      if (globalThis.descriptor.id !== '@maestro-care/dsh-client-ui-settings') {{
        throw new Error('wrong module id')
      }}
      const React = {{
        Fragment: Symbol('Fragment'),
        createElement() {{ return null }},
        useEffect() {{}},
        useState(value) {{ return [value, () => {{}}] }},
      }}
      const plugin = globalThis.descriptor.factory(name => {{
        if (name === 'react') return React
        throw new Error(`unexpected dependency: ${{name}}`)
      }})
      const registrations = []
      const ctx = {{
        slots: {{
          inject(name, callback) {{
            if (name !== 'settings.section') throw new Error('wrong slot')
            callback()
          }},
          register(options, component) {{
            registrations.push({{ options, component }})
            return () => {{}}
          }},
        }},
      }}
      plugin.apply(ctx)
      const entry = registrations[0]
      process.stdout.write(JSON.stringify({{
        inject: plugin.inject,
        count: registrations.length,
        id: entry.options.id,
        name: entry.options.name,
        order: entry.options.order,
        label: entry.options.label(),
        componentType: typeof entry.component,
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
        "inject": ["slots"],
        "count": 1,
        "id": "maestro",
        "name": "settings.section",
        "order": 5,
        "label": "MAESTRO",
        "componentType": "function",
    }
