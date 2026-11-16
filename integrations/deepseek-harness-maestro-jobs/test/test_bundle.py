from __future__ import annotations

import json
from pathlib import Path
import subprocess


BUNDLE_ROOT = Path(__file__).resolve().parents[1]
WORKSPACE_ROOT = BUNDLE_ROOT.parents[2]
DSH_ROOT = WORKSPACE_ROOT / "deepseek-harness"
PINNED_COMMIT = "dd6322d604e00eec1ba5e0c8541159906a21094a"


def test_bundle_manifest_and_patch_are_self_contained() -> None:
    manifest = json.loads((BUNDLE_ROOT / "package.json").read_text())
    assert manifest["name"] == "@maestro-care/deepseek-harness-maestro-jobs"
    assert manifest["dsh"]["bundle"]["patch"] == "./cordis.patch.yml"
    assert {"index.mjs", "cordis.patch.yml", "README.md"}.issubset(
        manifest["files"]
    )

    patch = (BUNDLE_ROOT / "cordis.patch.yml").read_text()
    assert "id: maestro-care-background-jobs" in patch
    assert "name: '@maestro-care/deepseek-harness-maestro-jobs'" in patch
    assert "inject: [tools, jobs]" in patch


def test_runtime_is_pinned_and_has_no_private_session_dependency() -> None:
    revision = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=DSH_ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert revision == PINNED_COMMIT

    runtime = (BUNDLE_ROOT / "index.mjs").read_text()
    assert f"TESTED_DSH_COMMIT = '{PINNED_COMMIT}'" in runtime
    assert "ctx.sessions" not in runtime
    assert "exec.agent.session" not in runtime
    assert "ctx.jobs.start" in runtime
    assert "ctx.tools.execute" in runtime
    assert "const MAESTRO_PREFIX = 'mcp__maestro__'" in runtime
    assert "maestro_generate_chain" in runtime
    assert "maestro_execute_chain" in runtime
    assert "maestro_evolution_start" in runtime
    assert "maestro_evolution_status" in runtime
    assert "maestro_evolution_cancel" in runtime
