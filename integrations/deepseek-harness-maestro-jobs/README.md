# MAESTRO jobs bundle for DeepSeek Harness

This bundle adds one explicit `maestro_background_run` producer. It transfers
approved generation, execution, or evolution work into DeepSeek Harness'
public `ctx.jobs` registry, so the standard `job_output`, `job_list`, and
`job_kill` controls and Web jobs UI work without private Session access.

The bundle is pinned to DeepSeek Harness commit
`dd6322d604e00eec1ba5e0c8541159906a21094a`. Mount it after
`@deepseek-ai/dsh-base`, the Web bundle, the MAESTRO MCP bundle, and the
MAESTRO Agent preset bundle. The profile must include `tool-jobs` in the active
Agent preset; the copied `standard` preset does.

## Guarantees

- The outer producer always requests one-shot approval before work starts.
- Only `maestro_generate_chain`, `maestro_execute_chain`, and the three typed
  evolution endpoints are reachable through the job bridge.
- The approved outer execution's opaque ToolExecution token, Agent identity,
  and root call id authorize the corresponding nested MCP dispatch. Foreign
  approval or denial decisions are preserved; only the exact companion
  MAESTRO-policy ask is consumed.
- `job_kill` aborts generation/execution MCP requests. Evolution submission is
  allowed to return its run id first, then the bridge invokes the typed
  `maestro_evolution_cancel` operation so cancellation does not knowingly
  orphan a submitted remote run.
- Evolution progress is made from actual `maestro_evolution_status` receipts.
  Generate and execute expose only truthful dispatch/final phases because the
  pinned MCP client does not publish MCP progress notifications as a public
  Harness hook.

## Non-goals and limitations

This is not a transparent `tools/execute` interceptor. The public around-tool
contract requires wrappers to await `next()` to quiescence, and an early job
receipt cannot satisfy the original MCP tool's declared output schema.

Jobs are process-local because `@deepseek-ai/dsh-jobs-local` is process-local.
Restarting the Harness loses their UI records, although CARE's operation-level
idempotency contracts still apply. Hot-unmounting this bundle while an
evolution job is live is unsupported; the MAESTRO profile uses startup-only
patch loading.
