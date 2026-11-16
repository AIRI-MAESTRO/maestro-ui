from __future__ import annotations

import json
from pathlib import Path
import subprocess


CARE_ROOT = Path(__file__).resolve().parents[1]
BUNDLE_ROOT = CARE_ROOT / "integrations" / "deepseek-harness-maestro-jobs"
DSH_ROOT = CARE_ROOT.parent / "deepseek-harness"
PINNED_COMMIT = "dd6322d604e00eec1ba5e0c8541159906a21094a"


def test_maestro_jobs_bundle_manifest_and_patch_are_self_contained() -> None:
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


def test_maestro_jobs_runtime_is_pinned_and_uses_only_public_seams() -> None:
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
    for raw_name in (
        "maestro_generate_chain",
        "maestro_execute_chain",
        "maestro_evolution_start",
        "maestro_evolution_status",
        "maestro_evolution_cancel",
    ):
        assert raw_name in runtime
