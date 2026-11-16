# Desktop CI and releases

Start **Actions → Desktop builds → Run workflow** to build verified installers.
The default run uploads installers as Actions artifacts for manual publication.
Select `main` and enable `publish_release` to publish after all five builds pass.
To publish a previous successful `main` build, enable `publish_release` and
enter its run ID in `artifact_run_id`.


Each target runs on its own native GitHub runner.

| Target | Architecture | Artifact |
| --- | --- | --- |
| macOS | ARM64, x64 | DMG containing Maestro.app |
| Linux | x64, ARM64 | tar.gz containing Maestro/ |
| Windows | x64 | EXE installer with app, runtime and uninstaller |

GitHub Actions retains the archives and checksums for 14 days. A run with
`publish_release` publishes a new versioned release and updates the
`main-build` prerelease only after all five archives pass verification.
The workflow refuses to attach binaries to a version tag on a different commit.

Builds include Python, runtime dependencies, integrations, and the official
DeepSeek Harness executable and sidecars from pinned commit
`dd6322d604e00eec1ba5e0c8541159906a21094a`, plus the small reviewed
`harness-stderr.patch` needed to forward the Unix credential descriptor to MCP
and include the application plugins' required workspace peers in the frozen
dependency closure. It also makes the upstream closure checker traverse apps.
Preset discovery enumerates names and checks their file types explicitly to
work around the pinned SEA carrier's unsupported Dirent enumeration.
The runtime cache is keyed by the pin, platform and patch hash.
The desktop builds install the integration versions declared in
`pyproject.toml` from PyPI. The release job uses the repository's
`GITHUB_TOKEN` with `contents: write` to publish installers to GitHub Releases.

macOS bundles have an ad-hoc signature, without Developer ID notarization.
Windows installers are unsigned and install per user without elevation. They
include Microsoft's Authenticode-verified WebView2 bootstrapper, which installs
the runtime if absent (an internet connection is needed for that first install).
Linux uses the bundled Qt WebEngine backend and needs the host's display and
system graphics libraries. These are desktop builds; mobile OSes and Windows
ARM64 are outside the current target matrix.

CI checks the mounted DMG and silently installs/uninstalls the Windows EXE.
The actual frozen host starts its pinned Harness and resident MCP child, which
hydrates every credential slot from the parent. Windows additionally creates
a native WebView2 window, enters a synthetic key through the masked native
Windows dialog, verifies only an opaque handle crosses the web bridge, and
checks the rendered MAESTRO UI and child shutdown.
These checks use disposable synthetic credentials and local endpoints; they
do not call an external model or prove every application workflow.

Windows uses current-user Credential Manager. The handoff's loopback connection
is authenticated using the kernel's TCP owner-PID table, the live Harness
process identity, and the helper's exact executable and MCP arguments. The
child validates the native server against its Harness grandparent. Only public
port/PID coordinates enter the environment; capabilities and secrets stay in
authenticated process memory and streams. Unix keeps its owner-only socket
and inherited descriptor. Session checks, allow-listed slots and fail-closed
handling remain shared across both transports.

To build locally, install Python 3.12, uv, Node 24 and pnpm 11.7.0; build the
official Harness for the host target using its
`scripts/build-exe-for-python-sdk.ts`, then run:

```bash
uv venv --python 3.12
uv pip install '.[desktop,carl,openai,anthropic]' 'pyinstaller>=6.16,<7'
# Linux also needs PySide6 and qtpy.
# Apply packaging/desktop/harness-stderr.patch in the pinned Harness checkout
# before building its executable. Windows packaging also needs Inno Setup 6
# and build/WebView2Setup.exe, verified against Microsoft's Authenticode signature.
uv run --no-sync python scripts/desktop_release.py prepare --target macos --arch arm64 --runtime-source ../deepseek-harness/python/sdk-runtime/src/deepseek_harness_runtime/runtime
uv run --no-sync pyinstaller --clean --noconfirm packaging/desktop/maestro.spec
uv run --no-sync python scripts/desktop_release.py archive --target macos --arch arm64
```
