import assert from 'node:assert/strict'
import test from 'node:test'

import { apply, BACKGROUND_TOOL_NAME, OPERATION_TARGETS } from '../index.mjs'

const dshRoot = new URL('../../../../deepseek-harness/', import.meta.url)

async function dshImport(path) {
  return await import(new URL(path, dshRoot).href)
}

test('the bundle composes against the pinned public ToolRuntime and JobRegistry', async () => {
  const [
    { Context },
    { default: SystemPrompt },
    { default: ToolRuntime },
    { default: AgentRegistry, Inbox },
    { default: ApprovalService },
    { default: LocalJobRegistry },
    { Session, SessionId },
  ] = await Promise.all([
    dshImport('vendor/cordis/lib/index.js'),
    dshImport('packages/core/system-prompt/lib/index.js'),
    dshImport('packages/core/tools/lib/index.js'),
    dshImport('packages/core/agent/lib/index.js'),
    dshImport('packages/interaction/user-approval/lib/index.js'),
    dshImport('packages/jobs/jobs-local/lib/index.js'),
    dshImport('packages/core/session/lib/index.js'),
  ])

  const ctx = new Context()
  await ctx.plugin(SystemPrompt, {})
  await ctx.plugin(ToolRuntime, { mode: 'native' })
  await ctx.plugin(AgentRegistry)
  await ctx.plugin(ApprovalService, { policy: 'ask' })
  await ctx.plugin(LocalJobRegistry, {})
  ctx.jobs.attachController('maestro-jobs-runtime-test')

  const scope = ctx.plugin(() => {})
  const session = Session.create(SessionId('maestro-jobs-runtime-test'))
  session.append('turn/start', { turn: 1 })
  const agent = {
    id: session.id,
    options: {},
    session,
    inbox: new Inbox(session, {
      inserted: () => {},
      discarded: () => {},
      claimed: () => {},
    }),
    status: 'idle',
    ctx: scope.ctx,
    send: () => {},
    followup: () => {},
    steer: () => ({ outcome: Promise.resolve({ status: 'rejected' }) }),
    inject: () => {},
    cancel: () => {},
    runMaintenance: job => job(new AbortController().signal),
    whenIdle: () => Promise.resolve(),
  }
  ctx.agents.register(agent)

  apply(ctx)
  ctx.on('tools/pre-execute', (exec, next) => {
    if (!exec.name.startsWith('mcp__maestro__')) return next()
    return Promise.resolve({
      kind: 'ask',
      reason: `MAESTRO operation "${exec.name}" may change state or start paid work.`,
    })
  })
  ctx.on('approval/request', () => Promise.resolve('allowed-once'))

  let nestedAgent
  let nestedParent
  ctx.tools.register({
    name: OPERATION_TARGETS.generate_chain,
    description: 'provider-free MAESTRO generation fixture',
    parameters: {
      type: 'object',
      properties: { idempotency_key: { type: 'string' } },
      required: ['idempotency_key'],
      additionalProperties: false,
    },
    output: {
      schema: {
        type: 'object',
        properties: {
          content: { type: 'array' },
          structuredContent: {
            type: 'object',
            properties: {
              status: { type: 'string', const: 'completed' },
              request_id: { type: 'string' },
              receipt: { type: 'object' },
            },
            required: ['status', 'request_id', 'receipt'],
            additionalProperties: false,
          },
        },
        required: ['content', 'structuredContent'],
        additionalProperties: false,
      },
      render: (_args, value) => [{ type: 'text', text: JSON.stringify(value) }],
    },
    async execute(_args, exec) {
      nestedAgent = exec.agent
      nestedParent = exec.parent
      return {
        content: [{ type: 'text', text: 'generated chain-1' }],
        structuredContent: {
          status: 'completed',
          request_id: 'runtime-generation-1',
          receipt: {
            status: 'ready',
            artifact: {
              artifact_id: 'chain-1',
              content_sha256: 'a'.repeat(64),
            },
          },
        },
      }
    },
  })

  try {
    const result = await ctx.tools.execute({
      callId: 'outer-runtime-call',
      name: BACKGROUND_TOOL_NAME,
      arguments: {
        operation: 'generate_chain',
        arguments: { idempotency_key: 'runtime-123' },
      },
      agent,
      signal: new AbortController().signal,
    })
    assert.equal(result.isError, false)
    assert.equal(result.value.job_id, 'maestro-1')

    const snapshot = await ctx.jobs.wait(result.value.job_id, 2_000, agent)
    assert.equal(snapshot.status, 'completed')
    assert.equal(nestedAgent, agent)
    assert.equal(typeof nestedParent, 'symbol')
    assert.match(ctx.jobs.read(result.value.job_id, agent).text, /"status":"completed"/)

    const approvals = session.events.filter(event => event.type === 'approval/asked')
    assert.equal(approvals.length, 1)
    assert.equal(approvals[0].data.toolName, BACKGROUND_TOOL_NAME)
  } finally {
    await ctx.fiber.dispose()
  }
})
