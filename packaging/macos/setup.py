"""Build ``Maestro.app`` with py2app from the repository root."""

from __future__ import annotations

import os
from pathlib import Path
import sys

from setuptools import setup


MACOS_DIR = Path(__file__).resolve().parent
REPOSITORY_ROOT = MACOS_DIR.parents[1]
sys.path.insert(0, str(MACOS_DIR))
sys.path.insert(0, str(REPOSITORY_ROOT))

from py2app_config import (  # noqa: E402
    SETUP_KWARGS,
    validate_deepseek_runtime_resources,
)
from bundle_finalize import finalize_local_bundle  # noqa: E402


# setuptools automatically reads ``pyproject.toml`` from the current working
# directory.  The repository metadata contains CARE's normal runtime
# dependencies, which py2app explicitly rejects as ``install_requires``. Run
# the dedicated packaging distribution from its own metadata-free directory;
# absolute app/resource/output paths in ``SETUP_KWARGS`` keep the documented
# repository-root invocation and artifact location unchanged.
previous_cwd = Path.cwd()
try:
    validate_deepseek_runtime_resources()
    os.chdir(MACOS_DIR)
    setup(**SETUP_KWARGS)
finally:
    os.chdir(previous_cwd)

if "py2app" in sys.argv:
    finalize_local_bundle(REPOSITORY_ROOT / "dist" / "Maestro.app")
