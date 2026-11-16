# macOS application bundle

`packaging/macos/setup.py` builds the native `dist/Maestro.app` with py2app.
The bundle includes CARE's Python runtime and resources plus the official
DeepSeek Harness single-executable runtime pinned by CARE. For the current
macOS arm64 target the required Harness resource set is exactly:

- `deepseek-harness-sdk-runtime-macos-arm64`
- `deepseek-harness-sdk-runtime-macos-arm64-rg`
- `deepseek-harness-sdk-runtime-macos-arm64-spawn-helper`

The main executable contains the closed Node dependency tree. Consequently an
ordinary launch of the completed application does not require system Node.js,
a separately installed `dsh`, or a DeepSeek Harness checkout. Installing or
updating external DeepSeek plugin packages is a separate management operation
and can still require `pnpm`.

## Build

Prepare the three runtime artifacts from the pinned sibling DeepSeek Harness
checkout with its official builder:

```bash
pnpm -C ../deepseek-harness install --frozen-lockfile
pnpm -C ../deepseek-harness exec tsx scripts/build-exe-for-python-sdk.ts \
  --targets=node24-macos-arm64
```

The upstream builder copies the selected artifacts into
`../deepseek-harness/python/sdk-runtime/src/deepseek_harness_runtime/runtime/`.
The MAESTRO setup fails closed if any expected file is absent.

Build and verify the bundle from the CARE repository root:

```bash
uv sync --extra macos-app
.venv/bin/python packaging/macos/setup.py py2app
.venv/bin/python packaging/macos/verify_bundle.py
codesign --verify --deep --strict --verbose=2 dist/Maestro.app
```

The verifier checks the bundle metadata, Python sources, UI and integration
resources, and byte-compares the three executable Harness resources with the
prepared pinned source artifacts. It also rejects missing execute permissions.

This is currently an arm64 packaging target with an ad-hoc signature. A public
release still requires Developer ID signing and Apple notarisation. The
optional MAESTRO Keychain provider is bundled as auditable source only; the app
does not ship or activate a universal `.node` addon.
