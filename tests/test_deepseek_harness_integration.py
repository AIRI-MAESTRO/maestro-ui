from __future__ import annotations

import json
import os
import selectors
import shutil
import subprocess
import sys
import time
import tomllib
from pathlib import Path

import pytest

from care.harness_mcp import CareMcpAdapter
from care.runtime.deepseek_bundle import (
    DIRECT_BRANDING_MODULE_TOKEN,
    DIRECT_JOBS_MODULE_TOKEN,
    DIRECT_POLICY_MODULE_TOKEN,
    DIRECT_PRESET_ROOT_MODULE_TOKEN,
    DIRECT_SETTINGS_MODULE_TOKEN,
    DeepSeekBundleMaterializationError,
    materialize_direct_patch,
)


ROOT = Path(__file__).resolve().parents[1]
INTEGRATION = ROOT / "integrations" / "deepseek-harness"
PRESET_INTEGRATION = ROOT / "integrations" / "deepseek-harness-maestro-preset"
JOBS_INTEGRATION = ROOT / "integrations" / "deepseek-harness-maestro-jobs"
SETTINGS_INTEGRATION = ROOT / "integrations" / "deepseek-harness-maestro-settings"
BRANDING_INTEGRATION = ROOT / "integrations" / "deepseek-harness-maestro-branding"
DSH_SOURCE = ROOT.parent / "deepseek-harness"
DSH = shutil.which("dsh")


def _manifest() -> dict[str, object]:
    return json.loads((INTEGRATION / "maestro-harness.json").read_text())


def _parse_patch(text: str) -> list[dict[str, object]]:
    yaml = pytest.importorskip("yaml")

    class DshLoader(yaml.SafeLoader):
        pass

    DshLoader.add_constructor(
        "tag:yaml.org,2002:js",
        lambda loader, node: {"__dsh_js__": loader.construct_scalar(node)},
    )
    return yaml.load(text, Loader=DshLoader)


def _bundle_patch() -> list[dict[str, object]]:
    return _parse_patch((INTEGRATION / "cordis.patch.yml").read_text())


def test_package_declares_a_complete_external_dsh_bundle() -> None:
    package = json.loads((INTEGRATION / "package.json").read_text())
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]

    assert package["name"] == "@maestro-care/deepseek-harness-bundle"
    assert package["version"] == project["version"]
    assert package["type"] == "module"
    assert package["dsh"] == {"bundle": {"patch": "./cordis.patch.yml"}}
    assert set(package["files"]) == {
        "cordis.patch.yml",
        "cordis.direct.patch.yml.in",
        "maestro-harness.json",
        "policy.mjs",
        "README.md",
    }
    for relative_path in package["files"]:
        assert (INTEGRATION / relative_path).is_file()


def test_patch_mounts_one_stdio_server_and_the_host_policy_without_secrets() -> None:
    patch = _bundle_patch()
    rows = next(entry["insert"] for entry in patch if "insert" in entry)
    assert [row["id"] for row in rows] == [
        "maestro-care-mcp",
        "maestro-care-tool-policy",
    ]

    mcp = rows[0]
    config = mcp["config"]
    assert mcp["name"] == "@deepseek-ai/dsh-mcp-client"
    assert config["serverName"] == "maestro"
    assert config["transport"] == "stdio"
    assert config["args"] == ["-P", "-s", "-m", "care.harness_mcp"]
    assert config["failOnStartupError"] is True
    assert config["command"] == {
        "__dsh_js__": "process.env.MAESTRO_CARE_PYTHON ?? 'python3'"
    }
    assert config["env"] == {
        "PYTHONHOME": {
            "__dsh_js__": "process.env.MAESTRO_CARE_PYTHONHOME ?? ''"
        },
        "PYTHONPATH": "",
        "PYTHONSTARTUP": "",
        "PYTHONINSPECT": "",
        "PYTHONNOUSERSITE": "1",
        "PYTHONSAFEPATH": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONUNBUFFERED": "1",
    }

    serialized = json.dumps(patch).lower()
    assert "api_key" not in serialized
    assert "password" not in serialized
    assert "secret" not in serialized
    assert "token" not in serialized

    policy = rows[1]
    assert policy["name"] == "@maestro-care/deepseek-harness-bundle"
    assert policy["inject"] == ["tools"]


def test_installed_and_direct_patches_share_the_exact_maestro_persona() -> None:
    installed = _bundle_patch()
    template = (INTEGRATION / "cordis.direct.patch.yml.in").read_text()
    direct = _parse_patch(
        materialize_direct_patch(
            template,
            (INTEGRATION / "policy.mjs").resolve(),
            (PRESET_INTEGRATION / "preset-root.mjs").resolve(),
            (JOBS_INTEGRATION / "index.mjs").resolve(),
            (SETTINGS_INTEGRATION / "index.mjs").resolve(),
            (BRANDING_INTEGRATION / "index.mjs").resolve(),
        )
    )

    def persona(patch: list[dict[str, object]]) -> str:
        row = next(entry for entry in patch if entry.get("id") == "system-prompt")
        assert row["name"] == "@deepseek-ai/dsh-system-prompt"
        return row["config"]["persona"]

    installed_persona = persona(installed)
    assert persona(direct) == installed_persona
    normalized = " ".join(installed_persona.lower().split())
    for required in (
        "resident agent",
        "web search",
        "carl chain contracts",
        "mage chain generation",
        "maestro memory",
        "evolution workflows",
        "tools actually published in the current session",
        "successful tool receipt",
        "explicit input contract",
        "lossless adaptation preview",
        "do not coerce types",
        "drop fields",
        "host approval",
        "public review challenge id",
        "maestro_command_status",
    ):
        assert required in normalized
    for forbidden in ("api key", "password", "secret", "access token"):
        assert forbidden not in normalized


def test_direct_patch_materializer_quotes_one_absolute_posix_policy_path(
    tmp_path: Path,
) -> None:
    template = (INTEGRATION / "cordis.direct.patch.yml.in").read_text()
    policy = tmp_path / 'policy "quoted" module.mjs'
    preset = tmp_path / 'preset "quoted" root.mjs'
    jobs = tmp_path / 'jobs "quoted" module.mjs'
    settings = tmp_path / 'settings "quoted" module.mjs'
    branding = tmp_path / 'branding "quoted" module.mjs'
    rendered = materialize_direct_patch(
        template,
        policy,
        preset,
        jobs,
        settings,
        branding,
    )

    assert DIRECT_POLICY_MODULE_TOKEN not in rendered
    assert DIRECT_PRESET_ROOT_MODULE_TOKEN not in rendered
    assert DIRECT_JOBS_MODULE_TOKEN not in rendered
    assert DIRECT_SETTINGS_MODULE_TOKEN not in rendered
    assert DIRECT_BRANDING_MODULE_TOKEN not in rendered
    assert json.dumps(policy.resolve().as_posix(), ensure_ascii=False) in rendered
    assert json.dumps(preset.resolve().as_posix(), ensure_ascii=False) in rendered
    assert json.dumps(jobs.resolve().as_posix(), ensure_ascii=False) in rendered
    assert json.dumps(settings.resolve().as_posix(), ensure_ascii=False) in rendered
    assert json.dumps(branding.resolve().as_posix(), ensure_ascii=False) in rendered
    patch = _parse_patch(rendered)
    rows = [
        row
        for entry in patch
        for row in entry.get("insert", [])
    ]
    policy_row = next(row for row in rows if row["id"] == "maestro-care-tool-policy")
    assert policy_row["name"] == policy.resolve().as_posix()
    preset_row = next(row for row in rows if row["id"] == "maestro-agent-preset-root")
    assert preset_row["name"] == preset.resolve().as_posix()
    jobs_row = next(row for row in rows if row["id"] == "maestro-care-background-jobs")
    assert jobs_row["name"] == jobs.resolve().as_posix()
    settings_row = next(row for row in rows if row["id"] == "maestro-native-settings")
    assert settings_row["name"] == settings.resolve().as_posix()
    branding_row = next(row for row in rows if row["id"] == "maestro-client-branding")
    assert branding_row["name"] == branding.resolve().as_posix()


@pytest.mark.parametrize(
    "template",
    [
        "[]\n",
        f"name: {DIRECT_POLICY_MODULE_TOKEN}\ncopy: {DIRECT_POLICY_MODULE_TOKEN}\n",
        "name: __MAESTRO_UNKNOWN_TOKEN__\n",
    ],
)
def test_direct_patch_materializer_rejects_missing_multiple_or_unknown_tokens(
    template: str,
    tmp_path: Path,
) -> None:
    with pytest.raises(DeepSeekBundleMaterializationError, match="exactly one"):
        materialize_direct_patch(
            template,
            tmp_path / "policy.mjs",
            tmp_path / "preset-root.mjs",
            tmp_path / "jobs.mjs",
            tmp_path / "settings.mjs",
            tmp_path / "branding.mjs",
        )


def test_direct_patch_materializer_rejects_relative_policy_path() -> None:
    template = (INTEGRATION / "cordis.direct.patch.yml.in").read_text()

    with pytest.raises(DeepSeekBundleMaterializationError, match="absolute POSIX"):
        materialize_direct_patch(
            template,
            Path("policy.mjs"),
            (PRESET_INTEGRATION / "preset-root.mjs").resolve(),
            (JOBS_INTEGRATION / "index.mjs").resolve(),
            (SETTINGS_INTEGRATION / "index.mjs").resolve(),
            (BRANDING_INTEGRATION / "index.mjs").resolve(),
        )


def test_direct_patch_materializer_rejects_relative_preset_root() -> None:
    template = (INTEGRATION / "cordis.direct.patch.yml.in").read_text()

    with pytest.raises(DeepSeekBundleMaterializationError, match="absolute POSIX"):
        materialize_direct_patch(
            template,
            (INTEGRATION / "policy.mjs").resolve(),
            Path("preset-root.mjs"),
            (JOBS_INTEGRATION / "index.mjs").resolve(),
            (SETTINGS_INTEGRATION / "index.mjs").resolve(),
            (BRANDING_INTEGRATION / "index.mjs").resolve(),
        )


def test_direct_patch_materializer_rejects_relative_jobs_module() -> None:
    template = (INTEGRATION / "cordis.direct.patch.yml.in").read_text()

    with pytest.raises(DeepSeekBundleMaterializationError, match="absolute POSIX"):
        materialize_direct_patch(
            template,
            (INTEGRATION / "policy.mjs").resolve(),
            (PRESET_INTEGRATION / "preset-root.mjs").resolve(),
            Path("jobs.mjs"),
            (SETTINGS_INTEGRATION / "index.mjs").resolve(),
            (BRANDING_INTEGRATION / "index.mjs").resolve(),
        )


def test_direct_patch_materializer_rejects_relative_settings_module() -> None:
    template = (INTEGRATION / "cordis.direct.patch.yml.in").read_text()

    with pytest.raises(DeepSeekBundleMaterializationError, match="absolute POSIX"):
        materialize_direct_patch(
            template,
            (INTEGRATION / "policy.mjs").resolve(),
            (PRESET_INTEGRATION / "preset-root.mjs").resolve(),
            (JOBS_INTEGRATION / "index.mjs").resolve(),
            Path("settings.mjs"),
            (BRANDING_INTEGRATION / "index.mjs").resolve(),
        )


def test_direct_patch_materializer_rejects_relative_branding_module() -> None:
    template = (INTEGRATION / "cordis.direct.patch.yml.in").read_text()

    with pytest.raises(DeepSeekBundleMaterializationError, match="absolute POSIX"):
        materialize_direct_patch(
            template,
            (INTEGRATION / "policy.mjs").resolve(),
            (PRESET_INTEGRATION / "preset-root.mjs").resolve(),
            (JOBS_INTEGRATION / "index.mjs").resolve(),
            (SETTINGS_INTEGRATION / "index.mjs").resolve(),
            Path("branding.mjs"),
        )


def test_policy_allow_list_excludes_every_exported_effectful_mcp_tool() -> None:
    manifest = _manifest()
    raw_tools = manifest["readOnlyRawTools"]

    assert manifest["mcpServerName"] == "maestro"
    assert manifest["publicToolPrefix"] == "mcp__maestro__"
    assert manifest["maestroToolDefaultDecision"] == "ask"
    assert (
        manifest["testedDeepSeekHarnessCommit"]
        == "dd6322d604e00eec1ba5e0c8541159906a21094a"
    )
    assert len(raw_tools) == len(set(raw_tools))
    exported = {spec.name for spec in CareMcpAdapter().list_tools()}
    assert set(raw_tools) <= exported
    assert exported - set(raw_tools) == {
        "maestro_command_confirm",
        "maestro_execute_chain",
        "maestro_evolution_cancel",
        "maestro_evolution_start",
        "maestro_generate_chain",
        "maestro_memory_write_chain",
        "maestro_stage_artifact",
    }


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is not installed")
def test_policy_delegates_reads_and_asks_for_unknown_effectful_tools() -> None:
    module_url = (INTEGRATION / "policy.mjs").as_uri()
    script = f"""
      import {{ apply, classifyMaestroTool }} from {json.dumps(module_url)};
      let hook;
      let hookOptions;
      const ctx = {{
        on(event, listener, options) {{
          if (event !== 'tools/pre-execute') throw new Error(event);
          hook = listener;
          hookOptions = options;
        }},
      }};
      apply(ctx);
      const next = async () => ({{ kind: 'deny', reason: 'downstream policy' }});
      const read = await hook({{ name: 'mcp__maestro__maestro_validate_chain' }}, next);
      const write = await hook({{ name: 'mcp__maestro__maestro_evolution_start' }}, next);
      const foreign = await hook({{ name: 'mcp__github__create_issue' }}, next);
      process.stdout.write(JSON.stringify({{
        read,
        write,
        foreign,
        hookOptions,
        classes: [
          classifyMaestroTool('mcp__maestro__maestro_memory_search'),
          classifyMaestroTool('mcp__maestro__future_tool'),
          classifyMaestroTool('bash'),
        ],
      }}));
    """
    completed = subprocess.run(
        [shutil.which("node"), "--input-type=module", "--eval", script],
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )
    result = json.loads(completed.stdout)

    assert result["read"] == {"kind": "deny", "reason": "downstream policy"}
    assert result["foreign"] == {"kind": "deny", "reason": "downstream policy"}
    assert result["write"]["kind"] == "ask"
    assert "may change state or start paid work" in result["write"]["reason"]
    assert result["hookOptions"] == {"prepend": True}
    assert result["classes"] == ["delegate", "ask", "delegate"]


@pytest.mark.skipif(
    not (DSH_SOURCE / "node_modules/.bin/tsx").is_file()
    or not (DSH_SOURCE / "packages/core/tools/src/index.ts").is_file(),
    reason="the pinned DeepSeek Harness source checkout and tsx are required",
)
def test_pinned_dsh_startup_policy_outvotes_an_earlier_allow_listener() -> None:
    """Exercise the supported fixed startup order in DSH's real ToolRuntime.

    Cordis prepends are LIFO.  The competing listener is therefore mounted
    before the direct MAESTRO policy, exactly as the frozen host composition
    promises.  A dynamically mounted *later* ``prepend`` listener would sit
    outside this policy and must be treated as trusted deployment code rather
    than as an isolated model plugin.
    """

    revision = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=DSH_SOURCE,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert revision == _manifest()["testedDeepSeekHarnessCommit"]

    policy_url = (INTEGRATION / "policy.mjs").as_uri()
    script = f"""
      import {{ Context }} from './vendor/cordis/src/index.ts'
      import SystemPrompt from './packages/core/system-prompt/src/index.ts'
      import ToolRuntime from './packages/core/tools/src/index.ts'
      import {{ apply as applyMaestroPolicy }} from {json.dumps(policy_url)}

      async function main() {{
        const target = 'mcp__maestro__maestro_memory_write_chain'
        const ctx = new Context()
        await ctx.plugin(SystemPrompt, {{}})
        await ctx.plugin(ToolRuntime, {{ mode: 'native' }})

        let competingCalls = 0
        let bodyCalls = 0
        ctx.on('tools/pre-execute', (exec, next) => {{
          if (exec.name !== target) return next()
          competingCalls += 1
          return Promise.resolve({{ kind: 'allow' }})
        }})
        applyMaestroPolicy(ctx)

        ctx.tools.register({{
          name: target,
          description: 'provider-free direct MCP write fixture',
          parameters: {{ type: 'object', properties: {{}}, additionalProperties: false }},
          output: {{
            schema: {{ type: 'object', properties: {{}}, additionalProperties: false }},
            render: () => [{{ type: 'text', text: 'executed' }}],
          }},
          execute: async () => {{
            bodyCalls += 1
            return {{}}
          }},
        }})

        const result = await ctx.tools.execute({{
          callId: 'maestro-policy-regression',
          name: target,
          arguments: {{}},
          signal: new AbortController().signal,
        }})
        process.stdout.write(JSON.stringify({{
          isError: result.isError,
          bodyCalls,
          competingCalls,
          text: result.content[0]?.type === 'text' ? result.content[0].text : '',
        }}))
        await ctx.fiber.dispose()
      }}

      main().catch(error => {{
        console.error(error)
        process.exitCode = 1
      }})
    """
    completed = subprocess.run(
        [str(DSH_SOURCE / "node_modules/.bin/tsx"), "--eval", script],
        cwd=DSH_SOURCE,
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    result = json.loads(completed.stdout)

    assert result["isError"] is True
    assert result["bodyCalls"] == 0
    assert result["competingCalls"] == 0
    assert "may change state or start paid work" in result["text"]


@pytest.mark.skipif(
    DSH is None, reason="DeepSeek Harness dsh executable is not installed"
)
def test_real_dsh_runtime_resolves_direct_patch_policy_from_absolute_env(
    tmp_path: Path,
) -> None:
    """Boot to Web readiness without a model/provider request, then stop."""

    assert DSH is not None
    policy_module = (INTEGRATION / "policy.mjs").resolve()
    direct_patch = tmp_path / "maestro-direct.patch.yml"
    direct_patch.write_text(
        materialize_direct_patch(
            (INTEGRATION / "cordis.direct.patch.yml.in").read_text(),
            policy_module,
            (PRESET_INTEGRATION / "preset-root.mjs").resolve(),
            (JOBS_INTEGRATION / "index.mjs").resolve(),
            (SETTINGS_INTEGRATION / "index.mjs").resolve(),
            (BRANDING_INTEGRATION / "index.mjs").resolve(),
        )
    )
    direct_patch.chmod(0o600)
    env = os.environ.copy()
    env.update(
        {
            "DSH_HOME": str(tmp_path / "dsh-home"),
            "MAESTRO_CARE_PYTHON": sys.executable,
            "MAESTRO_CARE_CWD": str(ROOT),
        }
    )
    process = subprocess.Popen(
        [
            DSH,
            "--profile",
            "web",
            "--patch",
            str(direct_patch),
            "--port",
            "0",
            "--no-open",
        ],
        cwd=ROOT,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    assert process.stdout is not None

    output = bytearray()
    ready = False
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ)
    deadline = time.monotonic() + 30
    try:
        while time.monotonic() < deadline:
            for key, _ in selector.select(timeout=0.25):
                chunk = key.fileobj.read1(16_384)
                if chunk:
                    output.extend(chunk)
                    ready = b"dsh web: http://" in output
            if ready or process.poll() is not None:
                break
    finally:
        selector.close()
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        output.extend(process.stdout.read())
        process.stdout.close()

    rendered = output.decode("utf-8", errors="replace")
    assert "ERR_MODULE_NOT_FOUND" not in rendered
    assert ready, (
        f"dsh did not reach Web readiness (exit={process.returncode}):\n"
        f"{rendered[-12_000:]}"
    )
