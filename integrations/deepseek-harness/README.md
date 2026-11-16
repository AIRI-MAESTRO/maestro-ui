# MAESTRO CARE bundle for DeepSeek Harness

This directory is an external DeepSeek Harness Profile Bundle. It connects the
resident Harness agent to CARE's typed stdio MCP server and adds host-enforced
approval policy for current state-changing MAESTRO tools. It does not fork or
modify the DeepSeek Harness repository.

The bundle contract and local tests are pinned to DeepSeek Harness commit
`dd6322d604e00eec1ba5e0c8541159906a21094a` (`dsh-v0.1.2-alpha.3`). Harness is
still a developer preview, so re-run these tests and a profile dump when moving
to a newer commit.

MAESTRO CARE is compatible with and built on DeepSeek Harness; it is not an
official DeepSeek product and this bundle does not imply endorsement.

## Install into a dedicated profile

Use absolute paths. Creating a non-shipped profile initially installs the base
bundle; adding `dsh-web-app` gives that profile the Web application, and adding
this directory contributes the MAESTRO MCP and policy rows.

```bash
dsh plugin --profile maestro-care add \
  @deepseek-ai/dsh-web-app \
  file:/ABSOLUTE/PATH/TO/care/integrations/deepseek-harness
```

Run CARE's interpreter explicitly so the MCP child imports the same checkout or
installed wheel as the native application:

```bash
MAESTRO_CARE_PYTHON=/ABSOLUTE/PATH/TO/care/.venv/bin/python \
MAESTRO_CARE_CWD=/ABSOLUTE/PATH/TO/care \
MAESTRO_HARNESS_WORKSPACE_ROOT=/ABSOLUTE/PATH/TO/USER/WORKSPACE \
MAESTRO_HARNESS_SESSION_ID=manual_maestro \
MAESTRO_HARNESS_ARTIFACT_ROOT=/ABSOLUTE/PRIVATE/ARTIFACT/DIRECTORY \
MAESTRO_HARNESS_MEMORY_ENABLED=true \
MAESTRO_HARNESS_GENERATION_ENABLED=true \
MAESTRO_HARNESS_EXECUTION_ENABLED=true \
MAESTRO_HARNESS_MEMORY_WRITE_ENABLED=true \
MAESTRO_HARNESS_EVOLUTION_ENABLED=true \
dsh --profile maestro-care --port 0 --no-open
```

The workspace and artifact paths must already be absolute, private, and
non-symlinked; the artifact directory must be writable. The native host creates
and validates them automatically. A manual launch may set individual feature
flags to `false`, but the broad reviewed command bridge stays unavailable unless
the complete effect surface is explicitly enabled, so a command cannot bypass
a disabled typed capability.

An installed bundle resolves the policy through its static package name. A
native host applying an overlay directly uses
`cordis.direct.patch.yml.in` instead: it calls
`care.runtime.deepseek_bundle.materialize_direct_patch`, which replaces the
template's strict module tokens with JSON-quoted absolute paths for the policy,
preset, and jobs modules,
writes the result to an owner-only temporary file, keeps that file alive for
the DSH process, and removes it after shutdown. Never pass the `.in` template
to DSH unchanged.

This materialization is required because Cordis evaluates `!!js` only inside a
plugin's `config` and `disabled` fields. Entry metadata such as `name` remains
literal, and relative module names resolve from the active profile directory,
not from the patch source directory.

`MAESTRO_CARE_PYTHON` and `MAESTRO_CARE_CWD` are paths, not credentials. The
patch never places provider keys in argv, YAML, or the child environment. CARE
keeps provider secrets behind its own settings/keystore boundary. DeepSeek
Harness deliberately runs stdio MCP commands as trusted host processes outside
the agent sandbox, so only install bundles you trust.

Inspect the effective tree without starting a model call:

```bash
dsh --profile maestro-care --dump-config
```

After changing bundle membership, restart the profile. Ordinary plugins and
other DeepSeek Harness bundles can still be installed into the same profile
with `dsh plugin --profile maestro-care add ...`.

## Permission contract

The MCP server publishes tools as `mcp__maestro__<raw-name>`. The exact
read-only allow-list lives in `maestro-harness.json` and contains capability and
command inspection, command-reconciliation status, CARL validation, explicit
input-contract inspection, lossless adaptation inspection/preview, Memory
reads, and evolution status. Adaptation receipts contain hashes and mapping
diagnostics rather than raw input values. Those calls delegate to any other
Harness policy; this bundle never overrides another denial or approval request.

Every other tool under `mcp__maestro__` returns a `tools/pre-execute` `ask`
decision. Harness proceeds only after an `allowed-once` response. Rejection,
cancellation, an agent-less call, or a missing approval service/channel is a
denial before dispatch. This is fail-closed for newly added generation,
execution, deployment, evolution, and Memory-write tools.

The adapter also exports typed generation, private staging, execution, Memory
write, evolution start/cancel, and reviewed command-confirm operations. They are
model-callable only behind one-shot approval. A command review exposes a public
`challenge_id`, never CommandBridge's bearer token. If a synchronous confirmed
handler outlives transport cancellation, an identical retry stays blocked until
`maestro_command_status` returns the retained result.

The native profile is startup-frozen and its direct MAESTRO patch is mounted
after installed profile bundles. This is also a trust boundary: Cordis policy
hooks are normal in-process listeners, so preset/plugin code able to mount a
later prepended listener must be treated as trusted deployment code.

The MAESTRO persona in the patch teaches the resident agent this product
contract, but it is guidance rather than a security boundary. Enforcement
remains in typed CARE admission, ToolRuntime policy, and host approval.

## Remove

```bash
dsh plugin --profile maestro-care remove @maestro-care/deepseek-harness-bundle
```
