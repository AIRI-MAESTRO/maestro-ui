# MAESTRO macOS Keychain provider for DeepSeek Harness

This external profile bundle replaces DeepSeek Harness's file-backed `credentials` row with a provider that stores references and durable plugin credential records as generic-password items in macOS Keychain. It calls `SecItemCopyMatching`, `SecItemUpdate`, `SecItemAdd`, and `SecItemDelete` through a dependency-free N-API addon linked to `Security.framework`; it never invokes `security`, and no secret is passed in process arguments, standard input, or standard output.

The implementation is audited against DeepSeek Harness commit `dd6322d604e00eec1ba5e0c8541159906a21094a` (`0.1.2-alpha.3`). That pre-release API can change, so re-run the focused tests and profile dump after updating Harness.

## Build and install

Build with the same Node major/architecture that launches DSH. The build needs Apple clang and Node headers; `MAESTRO_NODE_HEADERS` may name the directory containing `node_api.h` when the headers are not beside the Node installation. Keep architecture-specific output outside the source tree and give the host its absolute path:

```bash
cd /ABSOLUTE/PATH/TO/care/integrations/deepseek-harness-maestro-keychain
MAESTRO_KEYCHAIN_OUTPUT=/ABSOLUTE/PRIVATE/CACHE/maestro_keychain.node \
  npm run build:native

dsh plugin --profile maestro-care add \
  file:/ABSOLUTE/PATH/TO/care/integrations/deepseek-harness-maestro-keychain

MAESTRO_KEYCHAIN_NATIVE_MODULE=/ABSOLUTE/PRIVATE/CACHE/maestro_keychain.node \
  dsh --profile maestro-care --dump-config
```

The dump must show `id: credentials` / `@deepseek-ai/dsh-credentials-local` as disabled and one active `id: credentials-maestro-keychain` row named `@maestro-care/dsh-credentials-keychain`. DeepSeek Harness patch `name` fields guard the expected old module; they do not rename a row. The bundle must follow `@deepseek-ai/dsh-base` in the profile layer list so it disables the row after the base inserts it. Restart the profile after adding or removing it.

For the native MAESTRO host, install or provision this package once as a persistent dependency of `$DSH_HOME/profiles/maestro-care/package.json` and list its bundle after `@deepseek-ai/dsh-base` / `@deepseek-ai/dsh-web-app`. Do not point a direct patch row at the source checkout's absolute `index.mjs`: its `@deepseek-ai/*` ESM imports must resolve from the profile dependency tree and DSH installation fallback. An ordered `--patch` may configure the already-installed row, but it is not a package loader. Development may use the one-time `dsh plugin ... add`; production packaging should pre-provision the package and its dependency closure so launch performs no `pnpm` or network work.

Build the addon once into an owner-controlled cache keyed by DSH version, Node N-API version, `process.platform`, and `process.arch`; validate-load it with the exact Node executable DSH will use; then set `MAESTRO_KEYCHAIN_NATIVE_MODULE` to that absolute file. The host must never search writable working directories or silently compile during application startup. The current host creates its private profile itself, so merely adding these files does not activate the provider there.

For a distributable `.app`, ship a `.node` artifact built for every supported macOS architecture and validated with the bundled Node runtime. N-API version 8 avoids a V8 ABI dependency, but architecture and minimum macOS compatibility still matter. A source-checkout build on one machine is not a universal application artifact. Package verification should fail when the native file is absent, unloadable, or has the wrong architecture; it must not silently fall back to `.credentials.yaml`.

## DSH behavior preserved

- `resolve()` reads on every operation, so a rotated item reaches the next provider call.
- The inherited process environment remains the read-only top layer; project and user `.env` files remain fallbacks below Keychain.
- `describe()`, `describeRecord()`, and `listRecords()` return only configured/source/writable, key address, and record kind. They have no field for a value, matching the DSH settings and credentials API redaction contract.
- Empty reference values are rejected; `unset()` removes an item. Durable `api-key` and `grant` records are admitted at the storage boundary and malformed stored data fails with value-free diagnostics.
- Committed provider writes emit the existing DSH credential update events.
- Non-macOS load fails explicitly before attempting to load a native artifact. Missing or ABI-incompatible addons also fail explicitly.

## Limitations

- `modifyRecord()` is serialized inside one DSH process. Keychain's generic-password API exposes no compare-and-swap transaction around an async mutation, so two DSH processes mutating the same record can still race with last-writer-wins behavior.
- Changes made by another Keychain client are observed on the next read, but do not emit DSH update events because Security.framework supplies no item-change watcher on this API.
- Keychain improves storage-at-rest and avoids a discoverable plaintext file. It is not a promise that every process running as the same logged-in macOS user is unable to request access; actual prompts and access decisions remain macOS Keychain policy.
- There is no automatic migration from `$DSH_HOME/.credentials.yaml`. Migration must be an explicit user-authorized operation that reads the old values once, writes Keychain items, verifies configured status, and only then removes the old file.
- Tests use a memory fake and never create, update, read, or delete a real Keychain item. The native build check proves linking and module load only; an end-to-end Keychain mutation requires an explicit manual test.

## Focused verification

```bash
npm test
MAESTRO_KEYCHAIN_OUTPUT=/private/tmp/maestro_keychain.node npm run build:native
node -e "const a=require('/private/tmp/maestro_keychain.node'); console.log(Object.keys(a).sort())"
```

The final command inspects addon exports only. Do not call `set` or `delete` in automated tests.
