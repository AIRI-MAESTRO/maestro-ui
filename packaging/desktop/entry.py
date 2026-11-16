"""Frozen desktop/CLI entry and restricted resident MCP helper."""

import os
import sys
from pathlib import Path


def main() -> int:
    resources = Path(sys._MEIPASS)  # set by PyInstaller
    os.environ["RESOURCEPATH"] = str(resources)
    # The trusted helper is invoked with the same flags as CPython by DSH.
    if sys.argv[1:] == ["-P", "-s", "-m", "care.harness_mcp"]:
        from care.harness_mcp import main as mcp_main

        return mcp_main()
    if "--bundle-integration-smoke" in sys.argv or "--bundle-gui-smoke" in sys.argv:
        from care.runtime.desktop_smoke import run

        receipt = None
        if "--smoke-receipt" in sys.argv:
            receipt = Path(sys.argv[sys.argv.index("--smoke-receipt") + 1])
        try:
            run(gui="--bundle-gui-smoke" in sys.argv, receipt=receipt)
        except Exception:
            if receipt is None:
                raise
            # Windowed PyInstaller normally shows a blocking crash dialog.
            # The smoke receipt records the sanitized diagnostic instead.
            return 1
        return 0
    if "--bundle-smoke-test" in sys.argv:
        import webview  # noqa: F401
        from mmar_mage.schemas import GenerationRequest  # noqa: F401

        from care.runtime.deepseek_host import (
            _default_care_python,
            discover_deepseek_harness,
            probe_deepseek_harness_version,
            resolve_maestro_harness_integration,
        )

        command = discover_deepseek_harness()
        status = probe_deepseek_harness_version(command, timeout=60)
        if not status.compatible:
            raise RuntimeError(f"bundled Harness failed version check: {status}")
        resolve_maestro_harness_integration()
        helper, _ = _default_care_python()
        if not helper.is_file():
            raise RuntimeError("missing resident MCP helper")
        print("Bundled dependencies, integrations, helper and Harness verified.")
        return 0
    from care.desktop import prepare_desktop_working_directory

    prepare_desktop_working_directory()
    from care.cli import main as cli_main

    return cli_main(sys.argv[1:])


if __name__ == "__main__":
    raise SystemExit(main())
