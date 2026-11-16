# MAESTRO UI

MAESTRO UI provides the native desktop app, Textual TUI, and CLI for
generating, running, and evolving agent chains. It connects
[MAGE](https://github.com/AIRI-MAESTRO/mage) for generation,
[CARL](https://github.com/AIRI-MAESTRO/carl) for execution,
[IdeaL](https://github.com/AIRI-MAESTRO/ideal) for persistence, and
[GigaEvo Platform](https://github.com/AIRI-MAESTRO/gigaevo-platform) for evolution.

[CARE](https://github.com/AIRI-MAESTRO/care) is a separate MAESTRO module.
The `care` command alias, `CARE_*` settings, and legacy workbench
documented below are compatibility interfaces in MAESTRO UI.

Start desktop builds for macOS, Windows, and Linux manually from
**Actions → Desktop builds** in the public repository. Installers are published
to [MAESTRO UI GitHub Releases](https://github.com/AIRI-MAESTRO/maestro-ui/releases).
See [desktop CI and release instructions](packaging/desktop/README.md) for the
target matrix, version bumps, runtime requirements and signing status.

## Run

```bash
uv sync --extra desktop
uv run maestro init       # one-shot: write a minimal .env with generator creds
uv run maestro            # launch the native desktop application
uv run maestro app        # explicit native-app form
uv run maestro tui        # Textual compatibility interface
uv run maestro --help     # list CLI subcommands
```

Source and desktop builds use the same published integration versions:
`mmar-mage==0.2.0`, `mmar-carl==0.5.2`, and `gigaevo-client==0.3.1`.

The resident agent is pinned to DeepSeek Harness `0.1.2-alpha.3` at commit
`dd6322d604e00eec1ba5e0c8541159906a21094a`. A source launch auto-discovers a
built `../deepseek-harness` sibling checkout before a global `dsh`, or accepts
an explicit path:

```bash
git clone https://github.com/deepseek-ai/deepseek-harness ../deepseek-harness
git -C ../deepseek-harness checkout dd6322d604e00eec1ba5e0c8541159906a21094a
pnpm -C ../deepseek-harness install --frozen-lockfile
pnpm -C ../deepseek-harness run build
uv run maestro app --dsh-source ../deepseek-harness
```

The packaged macOS application instead carries the official DeepSeek Harness
single-executable runtime built from that pin, together with its `rg` and
`spawn-helper` sidecars. Running `Maestro.app` normally therefore requires
neither a system Node.js installation, a separate `dsh`, nor a Harness source
checkout. The source workflow above remains useful for development and
explicit runtime overrides.

`--allow-dsh-version-drift` is a developer escape hatch, not a supported
release configuration.

`maestro init` walks the minimum agent chain generator credentials (base URL, API key,
model) and writes a `./.env` so a fresh checkout can boot. Use
`--non-interactive` with explicit flags for CI / scripted setup:
`maestro init --non-interactive --api-key sk-... --base-url ... --model ...`.

### Native desktop app

`maestro app` opens a native Cocoa/WebKit window on macOS (and the platform
WebView on other supported systems). The default application is a resident
[DeepSeek Harness](https://github.com/deepseek-ai/deepseek-harness) agent,
not the earlier CARE form-based workbench. DeepSeek Harness owns sessions,
ordinary chat, model routing, web/filesystem tools, skills/plugins, approvals,
queued follow-ups, mid-turn steering, cancellation, and history. CARE is
mounted as a typed MCP capability provider for CARL/MAGE validation,
generation, exact contract-aware input adaptation, execution, Memory reads and
writes, and Platform evolution.

The window is still a native application: Harness listens only on an
ephemeral `127.0.0.1` port and is embedded in WebKit; it does not open a normal
browser tab. A one-time bootstrap token is exchanged for the Harness session
cookie, token-bearing output is redacted, telemetry is disabled by the native
host, and closing the window terminates Harness and its MCP child. The private
profile is frozen at startup to avoid macOS file-watcher exhaustion. It uses a
pinned `maestro` Agent preset with the standard DeepSeek tool surface plus a
MAESTRO persona; existing sessions retain the preset they were created with.

The MAESTRO MCP boundary never accepts a model-supplied `approved` flag.
Read-only tools pass through the Harness policy waterfall; provider calls,
local staging, execution, Memory writes, evolution start/cancel, and other
effects require the Harness one-shot approval channel. Generated chains can
carry an explicit Draft 2020-12 object-root runtime input contract. Adaptation
P0 is deliberately lossless: explicit JSON Pointer moves only, with no type
coercion, defaults, copying, or dropped fields.

Long generation, execution, and evolution calls can be launched through
`maestro_background_run`. They become real Harness jobs, so the standard jobs
panel and `job_list` / `job_output` / `job_kill` controls work. Generation and
execution expose truthful dispatch/final output; evolution additionally polls
typed status receipts. Cancelling evolution waits for its remote handle and
then calls the typed evolution-cancel operation instead of knowingly orphaning
the run.

The reviewed MAESTRO UI command tree is also available to the agent through four
shell-free tools: catalog, review, status, and confirm. Review executes only commands
classified as read-only. An effectful form returns a public `challenge_id`;
the actual one-use CommandBridge token never leaves the MCP process, and the
separate confirm call still goes through Harness approval. If that call is
cancelled after a synchronous handler has started, MAESTRO blocks an identical
retry until `maestro_command_status` reports the retained final receipt. The 19
legacy desktop-local slash forms are not falsely advertised as executable;
their catalog rows point to the corresponding Harness UI control where one
exists.

DeepSeek plugins installed into the `maestro-care` profile remain available:

```bash
DSH_HOME="$HOME/.config/care/deepseek-harness" \
  node ../deepseek-harness/apps/cli/lib/bin.js \
  plugin --profile maestro-care add <plugin-package>
```

Restart the app after changing profile bundles (`patchReload` is `startup`).
Use the same `DSH_HOME` value when the app is launched with `--dsh-home`.
The current Harness Settings inventory is read-only for plugin installation;
install/remove/update remains a privileged management action. Resolving an
external plugin package can still require `pnpm`; this is separate from
ordinary packaged-app startup, which does not require Node.js or `pnpm`.

#### Models, credentials, and Settings

Use the gear icon in the resident app for the **chat agent's** DeepSeek Harness
provider, model, and credential records. Do not paste credentials into chat.
CARE capability providers (chain generation, Memory, and evolution) still read
CARE configuration, so configure those with `maestro init`, `config.toml` /
`care.toml`, or `CARE_*` variables. The old seven-control CARE editor remains
available under `maestro app --legacy-workbench`; it has not yet been embedded
as a custom panel in Harness Settings.

By default, the pinned Harness profile uses its stock credential provider. An
audited macOS Keychain provider is included as source, but is deliberately not
auto-activated until a matching native addon has been built for the Node
runtime and architecture that launch Harness:

```bash
cd integrations/deepseek-harness-maestro-keychain
MAESTRO_KEYCHAIN_OUTPUT=/ABSOLUTE/PRIVATE/CACHE/maestro_keychain.node \
  npm run build:native

DSH_HOME="$HOME/.config/care/deepseek-harness" \
  node ../../../deepseek-harness/apps/cli/lib/bin.js \
  plugin --profile maestro-care add "file:$PWD"

cd ../..
MAESTRO_KEYCHAIN_NATIVE_MODULE=/ABSOLUTE/PRIVATE/CACHE/maestro_keychain.node \
  uv run maestro app --dsh-source ../deepseek-harness
```

This provider calls macOS `Security.framework` directly and never invokes the
interactive `security` utility. There is no automatic migration from an
existing Harness credential file: migration must be explicit and verified
before that file is removed. The source bundle is packaged for development,
but the current `.app` does not ship or activate a universal `.node` addon.

Use `maestro app --legacy-workbench` to open the previous CARE workbench. In
that compatibility UI, an ephemeral authenticated CARE HTTP/SSE server owns
the form-based generation/execution flow, command palette, and legacy
workspace session store. The paragraphs below describe that compatibility
surface.

#### Legacy workbench behavior

The native **Chat** flow generates an interactive (`deployable=False`) chain
and, when **Execute** is enabled, runs it without creating Production Memory
records. The **Agent** flow has two deliberately different outcomes:

- with **Execute** off it generates a deployable chain and runs typed CARL
  admission as a preview, but does not run the chain or write to Memory or
  Platform;
- with **Execute** on it runs the native Production saga: deployable generation
  → runtime preparation → typed admission → exact-task deduplication →
  Memory save → confirmed baseline execution → baseline-card recording →
  optional Platform evolution.

Runtime preparation produces one chain snapshot before any Production write.
Typed admission, the Memory record, baseline execution, and the optional
evolution seed all consume that same content; the execution path does not
rewrite the chain after it has been saved. Dynamic tool synthesis is disabled
for this path, so no new executable tool code can appear after confirmation.
Missing tools must already exist in the reviewed/configured tool registry or
the run fails closed.

The baseline confirmation shows a bounded, secret-free preview of the pending
effects together with short binding and provider/tool-state digests. MAESTRO UI
recomputes the state before execution and blocks the stage if it changed after
confirmation. The digests identify the confirmed snapshot; they do not expose
provider keys or other configuration values.

The Production path is a saga, not an ACID transaction. The chain is durable
before the baseline-run confirmation, and a later failure is reported as a
partial result with per-stage receipts; already committed Memory or Platform
effects are not presented as rolled back. Cooperative cancellation follows the
same rule: the runner returns the receipts it reached before stopping. A forced
task cancellation may have only the stage events emitted before interruption,
so callers must not infer rollback from an absent final receipt.

After a successful Production run, the workspace session stores its
`latest` revision pointer. While that Agent session remains selected, the next
plain-language prompt is routed through the state-bound `/revise` preview and
explicit save/discard flow instead of creating an unrelated agent. Use `/new`
to clear the pointer and start a new agent.

The native **Settings** screen is available from the top-bar Settings button,
`Cmd/Ctrl+,`, or `/settings`. It configures the generator provider, arbitrary
model ID, fast/deep generation mode, run policy, Memory/Platform endpoints,
evolution models, web-search provider, and UI/runtime defaults. Its seven
write-only credential controls cover the generator, Memory, Platform,
Platform mutation model, Platform validation model, web search, and chain
upload. The web-search control keeps the `mage` and `tools` search-key aliases
in sync. DeepSeek, OpenRouter, and OpenAI have canonical endpoint presets;
`Custom endpoint` supports HTTPS services and loopback HTTP services.

API-key inputs are write-only: an existing credential is represented only by
"Credential configured" and is never returned to the WebView. Saving requires
an explicit one-use confirmation. New credentials are written to versioned
entries in the operating-system Keychain, while `config.toml` and the workspace
`.env` receive only `keystore://...` references. MAESTRO UI refuses plaintext/file
keystore fallback. Changing an endpoint or search provider while retaining a
credential is also refused; replace or clear that credential in the same save
so a key cannot silently cross provider boundaries. A project `care.toml` that
still contains a literal editable secret must first be migrated or cleaned,
rather than being silently shadowed by a supposedly secure reference.

On macOS the Keychain backend calls `Security.framework` in-process. It never
launches `/usr/bin/security`, and a secret is never placed in process arguments,
stdin, or a terminal prompt. A normal macOS Keychain authorization dialog can
still appear when the operating system evaluates an item's access policy.

Settings is not a projection of every `CareConfig` field. Advanced service
values such as `platform.master_base_url`, service timeouts, and the upload
`url` / `auth_header` remain configurable through `config.toml`, `care.toml`,
or their `CARE_*` environment variables. Project/environment ownership checks
prevent the native screen from silently flattening those higher-precedence
layers.

Current verification has deliberate limits: the implementation and local
tests do not constitute a real macOS Keychain round-trip or a live provider,
Memory, or Platform smoke test. Those checks require credentials and services
in the target environment. Packaging currently uses an ad-hoc signature;
Developer ID signing and Apple notarisation are separate release steps.

The native composer also supplies focused UI adapters for commands that need
more than plain stdout:

- `/history [N]` and `/runs [N]` open the persisted workspace run list with a
  validated 1–1000 row display limit;
- `/blocks`, `/blocks copy <N>`, and
  `/blocks save <N> <workspace-relative-path>` inspect the visible result and
  atomically save a bounded UTF-8 block below the current workspace;
- `/revise <chain-id> <instruction>` produces a state-bound revision preview;
  native **Review & save revision** and **Discard draft** actions consume the
  one-use draft, while a changed base chain fails closed;
- `/memory edit` opens a private CARE.md editor. The document body is removed
  from generic command rendering, history, and confirmation previews; saves
  are UTF-8/size checked, bound to the loaded SHA-256 revision, written
  atomically, and refused when credential-like content is detected.

For frontend development only, `maestro web [--port 8787] [--no-open]` serves
the same authenticated UI in a browser. This is a diagnostic surface, not the
recommended application launch.

### Build `Maestro.app` on macOS

```bash
uv sync --extra macos-app
# Prepare the pinned arm64 runtime once in ../deepseek-harness. The upstream
# build copies the executable, rg, and spawn-helper into python/sdk-runtime.
pnpm -C ../deepseek-harness install --frozen-lockfile
pnpm -C ../deepseek-harness exec tsx scripts/build-exe-for-python-sdk.ts \
  --targets=node24-macos-arm64
.venv/bin/python packaging/macos/setup.py py2app
.venv/bin/python packaging/macos/verify_bundle.py
codesign --verify --deep --strict --verbose=2 dist/Maestro.app
./dist/Maestro.app/Contents/MacOS/Maestro   # terminal-attached smoke test
```

The current `dist/Maestro.app` includes the Python runtime, CARE
generation/execution dependencies, native WebView bridge, MAESTRO preset, and
UI assets. It also includes the pinned official DeepSeek Harness
single-executable runtime plus the matching `rg` and `spawn-helper` files; the
bundle verifier byte-compares those three artifacts with the prepared pinned
source runtime. No external Node.js, `dsh`, or Harness checkout is required for
an ordinary run of the built app. This packaging target is macOS arm64 only,
and py2app produces a local ad-hoc-signed bundle; Developer ID signing and Apple
notarisation remain required before external distribution. The optional
Keychain integration is still packaged as source only and does not include or
activate a universal `.node` addon. Maestric images come from `care-landing`;
reuse in this MAESTRO UI application was explicitly authorised by the
project owner for this integration. The exact source commit and files are
recorded in `care/web_assets/ASSET_PROVENANCE.md`. The upstream repository
should still add a machine-readable licence before unrelated third parties
redistribute the artwork independently.

`maestro tui` opens the compatibility **chat surface** — a Claude-Code-style
transcript with a prompt at the bottom and a mode toggle above it.
Type a task in natural language to generate + run an agent chain;
type a slash command (`/help`, `/mode`, `/library`, `/dataset`,
`/evolution`, `/tour`, …) for the non-chat affordances. First-time
users see a one-line offer to type `/tour` for a 5-step walkthrough.

### Chat modes

The toggle above the prompt picks one of two modes; `/mode` switches
the same setting via the keyboard. Default is **Ad-Hoc** —
configurable per-deployment with `CARE_CHAT__DEFAULT_MODE`.

| Mode           | What happens on every prompt                                                                                                                                                                                                                                                                                             |
| -------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| **Ad-Hoc**     | The agent chain generator produces a chain, the agent chain format runs it on the spot, the answer prints inline. The agent may loop (ReAct) until it decides the task is done. **Nothing is saved.**                                                                                                                    |
| **Production** | The agent chain generator produces a _reproducible_ chain, MAESTRO UI saves it to IdeaL under a stable `chain_id`, runs one baseline to seed a dataset entry, and (when Platform is wired) kicks off an evolution run. Use `/dataset list <chain_id>` / `/dataset run <chain_id>` / `/evolution watch <run_id>` to follow up. |

Production requires `CARE_MEMORY__BASE_URL` (and `CARE_MEMORY__API_KEY`
when the deployment enforces auth). Without Memory configured,
selecting Production auto-falls back to Ad-Hoc with a warning line —
the toggle's tooltip explains why.

First boot (no `~/.config/care/config.toml` on disk) lands on the
**Settings** screen so you can configure Memory / Platform base URLs +
the agent chain generator LLM credentials before doing anything else. Returning users
land on the chat surface; `/library` opens the saved-agents table
(last-run time, favourite status, fitness scores, row-action keys
below).

## Canonical user flow

Generate Agent A → save it → generate B and C → return to A from the
library → re-run from the same task + context files → optionally
evolve A and accept the best individual back into the stable channel.

## Screens

The MAESTRO UI TUI ships **18+ screens** covering the full lifecycle:

| Screen                | Purpose                                                |
| --------------------- | ------------------------------------------------------ |
| `WelcomeScreen`       | Boot splash + recents sidebar                          |
| `LibraryScreen`       | Saved-agent table, row actions, search                 |
| `QueryScreen`         | + New agent — task description + context files         |
| `GenerationScreen`    | Live agent chain generator progress                    |
| `InspectionScreen`    | Saved-chain detail + run history                       |
| `EditAgentScreen`     | Inline edit + Save / Promote-to-stable                 |
| `ExecutionScreen`     | Live agent chain run + token streaming                 |
| `EvolutionScreen`     | GA + Pareto picker + accept-winner                     |
| `ReplayScreen`        | Step through a saved ReasoningResult                   |
| `CatalogScreen`       | Browse installed capabilities (skills/MCP/tools)       |
| `MarketplaceScreen`   | Search shared agent_skill listings on Memory           |
| `HelpScreen`          | Tutorial + key cheat-sheet                             |
| `SettingsScreen`      | Edit agent chain generator / Memory / Platform / theme |
| `TaskListDrawer`      | In-flight tasks panel                                  |
| `CommandPaletteModal` | Fuzzy palette over commands + saved entities           |
| `DiffModal`           | Side-by-side compare two chains                        |
| `LineageModal`        | Walk a chain's ancestry DAG                            |
| `ConflictModal`       | Resolve a name collision on save                       |

## Global keys

- `Ctrl+P` — Command palette (search commands + chains + skills)
- `Ctrl+B` — Task list drawer
- `Ctrl+K` — Capability catalog
- `Ctrl+S` — Save current artifact
- `Ctrl+R` — Re-run current chain
- `?` — Help (tutorial + every binding)
- `Esc` — Back / cancel
- `Ctrl+Q` — Quit

Each screen layers its own bindings — see `?` (Help) for the full set
filtered by the active screen.

A single-page reference table covering every screen + modal lives at
[`docs/screens/README.md`](docs/screens/README.md). It maps each
surface to its slash command + status (M0/M1) + primary purpose so a
new contributor can locate the right module from one click.

## Demo

A short asciicast walks the three CLI surfaces against a
hermetic seed directory. Reproduce or re-record with:

```bash
scripts/record_demo.sh
```

See [`examples/asciicast/recording_script.md`](examples/asciicast/recording_script.md)
for the per-act keystroke list. The output lands at
`docs/asciicasts/care-tour.cast` and can be embedded in this
README once recorded (`asciinema upload` for a hosted player or
a local relative link for offline reading).

## Architecture

MAESTRO UI integrates the four modules above — see
[`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) for the layer-by-layer
walk-through (generation / execution / persistence / evolution) with
cross-links to the upstream modules and MAESTRO UI's internal module
boundaries.

## Configuration

MAESTRO UI reads its configuration from four sources, in increasing precedence:

1. Defaults baked into `care.config.CareConfig`.
2. `~/.config/care/config.toml` (user-global TOML).
3. `./care.toml` in the current working directory (per-project overrides).
4. `CARE_*` environment variables.

Nested fields use double-underscores in env-var form, mirroring how the
TOML sections nest. For example, `[mage] mode = "fast"` in TOML is
`CARE_MAGE__MODE=fast` in the environment.

See [`.env.example`](.env.example) for the complete list of supported
variables with descriptions and defaults. Copy it to `.env` (or export the
vars in your shell) and override only what you need.

### Quick reference

| Section     | Env-var prefix      | Purpose                                                                         |
| ----------- | ------------------- | ------------------------------------------------------------------------------- |
| `mage`      | `CARE_MAGE__*`      | Agent chain generator (provider, API key, …)                                    |
| `memory`    | `CARE_MEMORY__*`    | IdeaL connection                                                       |
| `platform`  | `CARE_PLATFORM__*`  | GigaEvo Platform connection                                                     |
| `sandbox`   | `CARE_SANDBOX__*`   | AgentSkill sandbox backend + limits                                             |
| `tools`     | `CARE_TOOLS__*`     | `@carl_tool` registry + bundled builtins (web_search, …) + on-the-fly synthesis |
| `telemetry` | `CARE_TELEMETRY__*` | Opt-in event-stream sink (Langfuse, …)                                          |
| `defaults`  | `CARE_DEFAULTS__*`  | UI defaults (language, history size)                                            |

## CLI

`maestro` and `maestro app` launch the native desktop application;
`maestro tui` keeps the Textual client available explicitly. The headless
subcommands share the same `CareConfig` and domain layers. The desktop command
registry combines the live parser with reviewed slash adapters; its current
manifest exposes 44 command names and marks unsupported forms explicitly
rather than silently invoking a shell. The resident agent reaches that same
registry through separate catalog, review, reconciliation-status, and
approval-gated confirm tools.
Read-only commands may complete during review; an effectful form returns a
short-lived state-bound capability and cannot run until the exact confirm tool
is approved. The legacy palette uses the same bridge directly.
Matching CLI and slash names are not treated as semantic twins: for example,
CLI `export` creates a library bundle while TUI `/export` creates a transcript.

**Setup**

- `maestro app [--width 1280] [--height 820] [--debug]
  [--dsh-source DIR|--dsh-executable PATH]` — open the resident native Harness
  window. Its ephemeral port is loopback-only and the authenticated bootstrap
  URL is never logged. `--port` is accepted only with `--legacy-workbench`.
- `maestro tui` — launch the Textual compatibility interface explicitly.
- `maestro web [--host 127.0.0.1] [--port 8787] [--no-open]` — start the
  browser-only developer workbench. Credentials remain in the Python process
  and are never returned by its HTTP API.
- `maestro init [--non-interactive] [--api-key X] [--base-url Y] [--model Z] [--mode ad_hoc|production] [--force]` —
  one-shot quick-start: writes a minimal `.env` with the four agent
  chain generator + chat keys a fresh checkout needs. Refuses to overwrite an existing
  `.env` without `--force`.

**Setup (cont.)**

- `maestro doctor [--config PATH] [--no-probes]` — diagnostic report:
  which `CARE_*` env vars are set, the resolved config path, installed
  extras, and live probes against Memory, MAGE, and Platform (the MAGE
  probe makes an authenticated round-trip so an expired key shows red).
- `maestro migrate-secrets` — move literal `*_api_key` values in
  `~/.config/care/config.toml` into the system keystore and rewrite the
  TOML with `keystore://…` URLs.

**Discovery & validation**

- `maestro catalog [--json] [--kind ...]` — list installed AgentSkills,
  MCP servers, tools, capability memory cards.
- `maestro validate <chain.json>` — parse + preflight an agent chain.
- `maestro import <pattern>... [--apply]` — batch-validate (dry-run
  default) or import chain JSON files (`--apply` saves them to Memory).

**Generate / run / replay**

- `maestro generate "<task>" [--mode fast|deep] [--save NAME] [--output PATH]` —
  one-shot agent chain generation.
- `maestro run <chain_id> [--execute] [--task TEXT] [--input KEY=VAL] [--save-result NAME]` —
  fetch a saved chain, preflight, and optionally execute the chain.
- `maestro replay <run.json>` — step through a saved
  `ReasoningResult` / `RunRecord` JSON.

**Memory browse**

- `maestro memory ls [--entity-type ...] [--tag ...] [--q ...]` —
  list saved entities.
- `maestro memory show <entity_id> [--content-only]` — drill down on
  a single entity.
- `maestro memory history <chain_id>` — list recorded runs for a
  chain.
- `maestro search "<query>" [--search-type bm25|vector|hybrid]` —
  BM25 / vector / hybrid search across saved entities.
- `maestro diff <left_id> <right_id>` — side-by-side chain compare.
- `maestro lineage <chain_id>` — walk the ancestry DAG.
- `maestro favourite <entity_id> [--off]` — star / unstar a library
  entity.

**Edit, versions & lifecycle**

- `maestro revise <chain_id> "<change>" [--yes]` — AI-edit a chain into a
  new version (previews the plan; `--yes` saves).
- `maestro versions <id>` — version history + channel pointers.
- `maestro rollback <id> --to <version-id> [--channel]` — repoint a
  channel (reversible).
- `maestro promote <id> [--from latest --to stable]` — move a channel
  pointer.
- `maestro forget <id> [--force]` — soft-delete (preview without
  `--force`).
- `maestro export <out.tar.gz> <chain_id>... [--skill ID]` — pack a
  portable bundle (inverse of `import`).

**Capabilities & evolution**

- `maestro marketplace "<query>"` — search shared agent_skill listings.
- `maestro evolve <chain_id> [--wait] [--accept]` — submit + watch +
  accept an evolution run.

**Eval datasets**

- `maestro dataset list|add|run|export <chain_id> …` — manage a chain's
  eval cases; `run` substring-scores, `export` writes JSONL for external
  frameworks.

**Agent hub**

- `maestro deploy <chain_id> [--name --channel]` — deploy a chain as an
  HTTP agent (needs a running hub).
- `maestro deployments` / `maestro metrics [name]` — list deployments /
  show usage metrics.

**Long-term memory**

- `maestro remember "<note>"` — save an explicit note to long-term
  memory.
- `maestro notes` — show stored long-term-memory notes.

**UX**

- `maestro help [--markdown] [--commands]` — render the tutorial +
  cheat-sheet; `--commands` prints the audited command-surface inventory and
  calls out semantic differences instead of claiming false parity.

Run `maestro <subcommand> --help` for the full flag set on each.
