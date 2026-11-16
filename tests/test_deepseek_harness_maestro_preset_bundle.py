from __future__ import annotations

import copy
import hashlib
import json
import shutil
import subprocess
import tomllib
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
BUNDLE = ROOT / "integrations" / "deepseek-harness-maestro-preset"
WORKSPACE_DSH = ROOT.parent / "deepseek-harness"


def _parse_yaml(path: Path) -> object:
    yaml = pytest.importorskip("yaml")

    class DshLoader(yaml.SafeLoader):
        pass

    DshLoader.add_constructor(
        "tag:yaml.org,2002:js",
        lambda loader, node: {"__dsh_js__": loader.construct_scalar(node)},
    )
    return yaml.load(path.read_text(), Loader=DshLoader)


def test_maestro_preset_is_a_self_contained_dsh_bundle() -> None:
    package = json.loads((BUNDLE / "package.json").read_text())
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]

    assert package["name"] == "@maestro-care/deepseek-harness-maestro-preset"
    assert package["version"] == project["version"]
    assert package["type"] == "module"
    assert package["main"] == "./preset-root.mjs"
    assert package["dsh"] == {"bundle": {"patch": "./cordis.patch.yml"}}
    assert set(package["files"]) == {
        "cordis.patch.yml",
        "preset-root.mjs",
        "agent-presets",
        "upstream-standard.json",
        "README.md",
    }
    for relative_path in (
        "cordis.patch.yml",
        "preset-root.mjs",
        "upstream-standard.json",
        "README.md",
        "agent-presets/maestro/agent.cordis.yml",
        "agent-presets/maestro/preset.yml",
    ):
        assert (BUNDLE / relative_path).is_file()


def test_patch_adds_a_package_relative_system_root_and_selects_maestro() -> None:
    patch = _parse_yaml(BUNDLE / "cordis.patch.yml")
    assert isinstance(patch, list)

    inserted = next(entry["insert"] for entry in patch if "insert" in entry)
    assert inserted == [
        {"id": "maestro-agent-preset-root", "name": "./preset-root.mjs"}
    ]

    roster = next(entry for entry in patch if entry.get("id") == "agent-presets")
    assert roster["inject"] == ["maestroAgentPresetRoot"]
    assert roster["config"] == {
        "default": "maestro",
        "roots": [
            {
                "path": {"__dsh_js__": "ctx.maestroAgentPresetRoot"},
                "trust": "system",
            }
        ],
    }


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is not installed")
def test_root_provider_resolves_the_bundled_agent_preset_directory() -> None:
    module_url = (BUNDLE / "preset-root.mjs").as_uri()
    script = f"""
      import {{ apply }} from {json.dumps(module_url)};
      let provided;
      apply({{ provide(name, value) {{ provided = {{ name, value }}; }} }});
      process.stdout.write(JSON.stringify(provided));
    """
    completed = subprocess.run(
        [shutil.which("node"), "--input-type=module", "--eval", script],
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )
    provided = json.loads(completed.stdout)
    assert provided["name"] == "maestroAgentPresetRoot"
    assert Path(provided["value"]).resolve() == (BUNDLE / "agent-presets").resolve()


def test_maestro_preset_changes_only_the_pinned_standard_persona() -> None:
    provenance = json.loads((BUNDLE / "upstream-standard.json").read_text())
    assert provenance == {
        "deepSeekHarnessCommit": "dd6322d604e00eec1ba5e0c8541159906a21094a",
        "source": "packages/preset/agent-presets/presets/standard/agent.cordis.yml",
        "sha256": "f04fbc6ec6d38aab78f18690c293ddcb76293107f7e6cd157904b7c0e83094bd",
        "allowedDifference": "persona.config.text",
    }

    upstream_path = WORKSPACE_DSH / provenance["source"]
    if not upstream_path.is_file():
        pytest.skip("the pinned DeepSeek Harness checkout is not present")
    assert hashlib.sha256(upstream_path.read_bytes()).hexdigest() == provenance["sha256"]

    upstream = _parse_yaml(upstream_path)
    maestro = _parse_yaml(BUNDLE / "agent-presets" / "maestro" / "agent.cordis.yml")
    assert isinstance(upstream, list)
    assert isinstance(maestro, list)

    normalized = copy.deepcopy(maestro)
    upstream_persona = next(row for row in upstream if row.get("id") == "persona")
    maestro_persona = next(row for row in normalized if row.get("id") == "persona")
    assert maestro_persona["name"] == "@deepseek-ai/dsh-persona"
    maestro_text = maestro_persona["config"]["text"]
    for required in (
        "You are MAESTRO",
        "{{model}}",
        "{{cwd}}",
        "CARL chain contracts",
        "MAGE chain generation",
        "MAESTRO Memory",
        "lossless adaptation preview",
        "successful tool receipt",
        "host approval",
        "command catalog",
        "public review challenge id",
        "maestro_command_status",
        "API key into chat",
        "maestro_background_run",
    ):
        assert required in maestro_text
    maestro_persona["config"]["text"] = upstream_persona["config"]["text"]
    assert normalized == upstream
