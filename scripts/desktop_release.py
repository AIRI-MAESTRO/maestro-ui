"""Prepare native runtime resources and archive verified desktop builds."""

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def project_version() -> str:
    return tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))[
        "project"
    ]["version"]


def runtime_files(target: str, arch: str) -> tuple[str, ...]:
    base = f"deepseek-harness-sdk-runtime-{target}-{arch}"
    if target == "win":
        return base + ".exe", base + "-rg.exe"
    files = (base, base + "-rg")
    return (*files, base + "-spawn-helper") if target == "macos" else files


def prepare(source: Path, target: str, arch: str) -> None:
    stage = ROOT / "build" / "desktop-resources"
    destination = stage / "deepseek-harness-runtime"
    if destination.exists():
        shutil.rmtree(destination)
    destination.mkdir(parents=True)
    files = runtime_files(target, arch)
    for name in files:
        artifact = source / name
        if not artifact.is_file():
            raise FileNotFoundError(f"missing official Harness artifact: {artifact}")
        shutil.copy2(artifact, destination / name)
        (destination / name).chmod(0o755)
    (stage / "desktop-runtime.json").write_text(
        json.dumps({"executable": files[0], "files": files}) + "\n",
        encoding="utf-8",
    )


def archive(target: str, arch: str) -> None:
    out = ROOT / "release-assets"
    out.mkdir(exist_ok=True)
    stem = out / f"Maestro-{project_version()}-{target}-{arch}"
    if target == "macos":
        product = ROOT / "dist" / "Maestro.app"
        subprocess.run(
            ["codesign", "--force", "--deep", "--sign", "-", str(product)], check=True
        )
        subprocess.run(
            ["codesign", "--verify", "--deep", "--strict", str(product)], check=True
        )
        stage = ROOT / "build" / "dmg"
        if stage.exists():
            shutil.rmtree(stage)
        stage.mkdir(parents=True)
        shutil.copytree(product, stage / "Maestro.app", symlinks=True)
        (stage / "Applications").symlink_to("/Applications")
        artifact = Path(str(stem) + ".dmg")
        subprocess.run(
            [
                "hdiutil",
                "create",
                "-ov",
                "-volname",
                "Maestro",
                "-srcfolder",
                str(stage),
                "-format",
                "UDZO",
                str(artifact),
            ],
            check=True,
        )
        subprocess.run(["hdiutil", "verify", str(artifact)], check=True)
    elif target == "win":
        compiler = shutil.which("ISCC") or str(
            Path(os.environ.get("ProgramFiles(x86)", "C:/Program Files (x86)"))
            / "Inno Setup 6"
            / "ISCC.exe"
        )
        bootstrapper = ROOT / "build" / "WebView2Setup.exe"
        if not bootstrapper.is_file():
            raise FileNotFoundError(
                "prepare the signed Microsoft WebView2 bootstrapper first"
            )
        subprocess.run(
            [
                compiler,
                f"/DAppVersion={project_version()}",
                f"/DBundlePath={ROOT / 'dist' / 'Maestro'}",
                f"/DOutputPath={out}",
                f"/DOutputName={stem.name}",
                f"/DWebViewSetup={bootstrapper}",
                str(ROOT / "packaging/desktop/windows.iss"),
            ],
            check=True,
        )
        artifact = Path(str(stem) + ".exe")
    else:
        artifact = Path(
            shutil.make_archive(
                str(stem),
                "gztar",
                ROOT / "dist",
                "Maestro",
            )
        )
    with artifact.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    artifact.with_name(artifact.name + ".sha256").write_text(
        f"{digest}  {artifact.name}\n", encoding="utf-8"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("prepare", "archive", "version"))
    parser.add_argument("--target", choices=("macos", "linux", "win"))
    parser.add_argument("--arch", choices=("x64", "arm64"))
    parser.add_argument("--runtime-source", type=Path)
    args = parser.parse_args()
    if args.operation == "version":
        print(project_version())
    elif args.operation == "prepare":
        if not all((args.runtime_source, args.target, args.arch)):
            parser.error("prepare requires --runtime-source, --target and --arch")
        prepare(args.runtime_source, args.target, args.arch)
    else:
        if not all((args.target, args.arch)):
            parser.error("archive requires --target and --arch")
        archive(args.target, args.arch)


if __name__ == "__main__":
    main()
