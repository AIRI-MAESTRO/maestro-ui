"""Finalize local ``Maestro.app`` metadata after py2app signs the bundle."""

from __future__ import annotations

import os
from pathlib import Path
import plistlib
import subprocess
import tempfile
from typing import Callable, Sequence


CommandRunner = Callable[..., subprocess.CompletedProcess[bytes]]
MACOS_MINIMUM_VERSION = "26.0"


def _write_plist_atomically(path: Path, payload: dict[str, object]) -> None:
    mode = path.stat().st_mode & 0o777
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            dir=path.parent,
            prefix=".Info.",
            suffix=".plist",
            delete=False,
        ) as stream:
            temporary_path = Path(stream.name)
            plistlib.dump(payload, stream, sort_keys=False)
            stream.flush()
            os.fsync(stream.fileno())
        temporary_path.chmod(mode)
        os.replace(temporary_path, path)
        temporary_path = None
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def finalize_local_bundle(
    bundle: str | os.PathLike[str],
    *,
    runner: CommandRunner = subprocess.run,
) -> bool:
    """Remove builder-local metadata and refresh the bundle's ad-hoc signature."""

    app = Path(bundle).expanduser().resolve()
    plist_path = app / "Contents" / "Info.plist"
    with plist_path.open("rb") as stream:
        payload = plistlib.load(stream)
    changed = payload.get("LSMinimumSystemVersion") != MACOS_MINIMUM_VERSION
    payload["LSMinimumSystemVersion"] = MACOS_MINIMUM_VERSION
    python_info = payload.get("PythonInfoDict")
    if isinstance(python_info, dict):
        changed = bool(python_info.pop("PythonExecutable", None)) or changed
    if not changed:
        return False

    _write_plist_atomically(plist_path, payload)
    command: Sequence[str] = (
        "codesign",
        "--force",
        "--deep",
        "--sign",
        "-",
        os.fspath(app),
    )
    runner(command, check=True)
    return True


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("bundle")
    arguments = parser.parse_args()
    finalize_local_bundle(arguments.bundle)
