import assert from 'node:assert/strict'
import test from 'node:test'

import {
  apply,
  BACKGROUND_TOOL_NAME,
  OPERATION_TARGETS,
} from '../index.mjs'

const STATUS_TOOL = 'mcp__maestro__maestro_evolution_status'
const CANCEL_TOOL = 'mcp__maestro__maestro_evolution_cancel'
const CHAIN_DIGEST = 'a'.repeat(64)
const OUTPUT_DIGEST = 'b'.repeat(64)

function success(structuredContent, text = JSON.stringify(structuredContent)) {
  return {
    isError: false,
    value: { content: [{ type: 'text', text }], structuredContent },
    content: [{ type: 'text', text }],
  }
}

function generationSuccess({ requestId = 'r-1', artifactId = 'chain-1' } = {}) {
  return success({
    status: 'completed',
    request_id: requestId,
    receipt: {
      status: 'ready',
      artifact: {
        artifact_id: artifactId,
        content_sha256: CHAIN_DIGEST,
      },
    },
  })
}

function executionSuccess({ requestId = 'execute-1', successful = true } = {}) {
  return success({
    status: 'completed',
    request_id: requestId,
    receipt: {
      status: 'completed',
      success: successful,
      chain_digest: CHAIN_DIGEST,
      output_artifact: {
        artifact_id: 'execution-output-1',
        content_sha256: OUTPUT_DIGEST,
      },
    },
  })
}

function evolutionStartSuccess({ requestId = 'start-1', evolutionId = 'evo-1' } = {}) {
  return success({
    status: 'completed',
    request_id: requestId,
    receipt: {
      status: 'started',
      evolution_id: evolutionId,
      evolution_status: 'running',
    },
  })
}

function deferred() {
  let resolve
  const promise = new Promise(done => { resolve = done })
  return { promise, resolve }
}

function nextTask() {
  return new Promise(resolve => setImmediate(resolve))
}

function fixture(responder) {
  let policy
  let definition
  let hooks
  const calls = []
  const decisions = []

  const ctx = {
    on(event, callback, options) {
      assert.equal(event, 'tools/pre-execute')
      assert.deepEqual(options, { prepend: true })
      policy = callback
      return () => {}
    },
    tools: {
      register(candidate) {
        definition = candidate
        return () => {}
      },
      async execute(input) {
        calls.push(input)
        const downstream = () => Promise.resolve(
          input.name === STATUS_TOOL
            ? { kind: 'allow' }
            : {
                kind: 'ask',
                reason: `MAESTRO operation "${input.name}" may change state or start paid work.`,
              },
        )
        const decision = await policy(input, downstream)
        decisions.push(decision)
        if (decision.kind !== 'allow') {
          return {
            isError: true,
            error: { message: 'nested call was not authorized' },
            content: [{ type: 'text', text: 'nested call was not authorized' }],
          }
        }
        return await responder(input)
      },
    },
    jobs: {
      start(spec) {
        assert.equal(spec.kind, 'maestro')
        assert.equal(spec.owner, owner)
        hooks = spec.run()
        return 'maestro-1'
      },
    },
  }
  const owner = { id: 'agent-1' }
  const token = Symbol('outer-execution')
  const exec = {
    callId: 'outer-call',
    rootCallId: 'outer-call',
    token,
    agent: owner,
    signal: new AbortController().signal,
  }
  apply(ctx)
  return {
    calls,
    decisions,
    definition,
    exec,
    get hooks() { return hooks },
    policy: (...args) => policy(...args),
  }
}

test('the producer always asks, while preserving stricter downstream policy', async () => {
  const f = fixture(async () => success({ status: 'completed' }))
  const ask = await f.policy(
    { name: BACKGROUND_TOOL_NAME },
    () => Promise.resolve({ kind: 'allow' }),
  )
  assert.deepEqual(ask, {
    kind: 'ask',
    reason: 'Start approved MAESTRO provider work as a cancellable background job.',
  })

  const denied = await f.policy(
    { name: BACKGROUND_TOOL_NAME },
    () => Promise.resolve({ kind: 'deny', reason: 'deployment denied' }),
  )
  assert.deepEqual(denied, { kind: 'deny', reason: 'deployment denied' })

  const foreignAsk = await f.policy(
    {
      name: OPERATION_TARGETS.generate_chain,
      parent: Symbol('foreign'),
    },
    () => Promise.resolve({ kind: 'ask', reason: 'foreign policy' }),
  )
  assert.deepEqual(foreignAsk, { kind: 'ask', reason: 'foreign policy' })
})

test('generation becomes an owned JobRegistry task and uses the exact approved nested call', async () => {
  const f = fixture(async () => generationSuccess())

  const started = await f.definition.execute({
    operation: 'generate_chain',
    arguments: { idempotency_key: 'generate-123', task: 'build a chain' },
  }, f.exec)
  assert.deepEqual(started, {
    kind: 'background',
    operation: 'generate_chain',
    job_id: 'maestro-1',
  })

  const outcome = await f.hooks.done
  assert.deepEqual(outcome, { status: 'completed', detail: 'generate_chain completed' })
  assert.equal(f.calls.length, 1)
  assert.equal(f.calls[0].name, OPERATION_TARGETS.generate_chain)
  assert.deepEqual(f.calls[0].arguments, {
    idempotency_key: 'generate-123',
    task: 'build a chain',
  })
  assert.equal(f.calls[0].parent, f.exec.token)
  assert.equal(f.calls[0].agent, f.exec.agent)
  assert.equal(f.calls[0].rootCallId, f.exec.rootCallId)
  assert.notEqual(f.calls[0].signal, f.exec.signal)
  assert.deepEqual(f.decisions, [{ kind: 'allow' }])
  assert.match(f.hooks.readOutput(), /"artifact_id":"chain-1"/)
  assert.equal(f.hooks.readOutput(), '')
})

test('every operation reports an unavailable MCP result as a failed job', async () => {
  const cases = [
    ['generate_chain', { idempotency_key: 'generate-unavailable' }],
    ['execute_chain', { idempotency_key: 'execute-unavailable' }],
    ['evolve_chain', { idempotency_key: 'evolve-unavailable' }],
  ]

  for (const [operation, args] of cases) {
    const f = fixture(async () => success({
      status: 'unavailable',
      request_id: `${operation}-request`,
      receipt: null,
      message: 'capability is disabled',
    }))
    await f.definition.execute({ operation, arguments: args }, f.exec)
    const outcome = await f.hooks.done
    assert.equal(outcome.status, 'failed')
    assert.match(outcome.detail, /unavailable: capability is disabled/)
  }
})

test('operation-specific validation rejects missing and malformed success receipts', async () => {
  const cases = [
    [
      'generate_chain',
      success({ status: 'completed', request_id: 'generate-no-receipt' }),
      /returned no receipt/,
    ],
    [
      'execute_chain',
      success({
        status: 'completed',
        request_id: 'execute-invalid-receipt',
        receipt: {
          status: 'completed',
          success: true,
          chain_digest: 'not-a-digest',
          output_artifact: {
            artifact_id: 'output-1',
            content_sha256: OUTPUT_DIGEST,
          },
        },
      }),
      /invalid execution receipt/,
    ],
    [
      'evolve_chain',
      success({
        status: 'completed',
        request_id: 'evolve-invalid-receipt',
        receipt: {
          status: 'started',
          evolution_id: '',
          evolution_status: 'running',
        },
      }),
      /invalid start receipt/,
    ],
  ]

  for (const [operation, result, detail] of cases) {
    const f = fixture(async () => result)
    await f.definition.execute({
      operation,
      arguments: { idempotency_key: `${operation}-invalid` },
    }, f.exec)
    const outcome = await f.hooks.done
    assert.equal(outcome.status, 'failed')
    assert.match(outcome.detail, detail)
  }
})

test('an unsuccessful execution receipt settles failed, not completed', async () => {
  const f = fixture(async () => executionSuccess({ successful: false }))
  await f.definition.execute({
    operation: 'execute_chain',
    arguments: { idempotency_key: 'execute-unsuccessful' },
  }, f.exec)

  assert.deepEqual(await f.hooks.done, {
    status: 'failed',
    detail: 'execute_chain completed with an unsuccessful chain result',
  })
})

test('a valid successful execution receipt settles completed', async () => {
  const f = fixture(async () => executionSuccess())
  await f.definition.execute({
    operation: 'execute_chain',
    arguments: { idempotency_key: 'execute-successful' },
  }, f.exec)

  assert.deepEqual(await f.hooks.done, {
    status: 'completed',
    detail: 'execute_chain completed',
  })
})

test('job cancellation aborts an in-flight chain execution and settles killed', async () => {
  let nestedSignal
  const f = fixture(async (input) => {
    nestedSignal = input.signal
    return await new Promise(resolve => {
      input.signal.addEventListener('abort', () => resolve({
        isError: true,
        error: { message: 'tool call aborted' },
        content: [{ type: 'text', text: 'Error: tool call aborted' }],
      }), { once: true })
    })
  })

  await f.definition.execute({
    operation: 'execute_chain',
    arguments: {
      idempotency_key: 'execute-123',
      chain: { artifact_id: 'chain-1', content_sha256: 'a'.repeat(64) },
    },
  }, f.exec)
  await nextTask()
  assert.equal(nestedSignal.aborted, false)
  f.hooks.cancel('user stopped the job')
  const outcome = await f.hooks.done
  assert.equal(nestedSignal.aborted, true)
  assert.deepEqual(outcome, {
    status: 'killed',
    detail: 'execute_chain request stopped; remote outcome may be uncertain',
  })
  const output = f.hooks.readOutput()
  assert.match(output, /Cancellation requested/)
  assert.match(output, /replay its idempotency key/)
})

test('a successful receipt wins a cancellation race and remains collectable', async () => {
  const result = deferred()
  const f = fixture(async () => await result.promise)

  await f.definition.execute({
    operation: 'generate_chain',
    arguments: { idempotency_key: 'generate-race-123' },
  }, f.exec)
  await nextTask()
  f.hooks.cancel('race')
  result.resolve(success({
    status: 'completed',
    request_id: 'generated-after-cancel',
    receipt: {
      status: 'ready',
      artifact: {
        artifact_id: 'chain-race',
        content_sha256: CHAIN_DIGEST,
      },
    },
  }))

  assert.deepEqual(await f.hooks.done, {
    status: 'completed',
    detail: 'generate_chain completed before cancellation took effect',
  })
  const output = f.hooks.readOutput()
  assert.match(output, /"artifact_id":"chain-race"/)
  assert.match(output, /successful receipt before cancellation took effect/)
})

test('an unavailable result cannot win a cancellation race as success', async () => {
  const result = deferred()
  const f = fixture(async () => await result.promise)

  await f.definition.execute({
    operation: 'generate_chain',
    arguments: { idempotency_key: 'generate-unavailable-race' },
  }, f.exec)
  await nextTask()
  f.hooks.cancel('race')
  result.resolve(success({
    status: 'unavailable',
    request_id: 'generated-unavailable-after-cancel',
    receipt: null,
    message: 'generator disabled',
  }))

  assert.deepEqual(await f.hooks.done, {
    status: 'failed',
    detail: 'generate_chain unavailable: generator disabled',
  })
  assert.doesNotMatch(f.hooks.readOutput(), /successful receipt before cancellation/)
})

test('evolution polls admitted status receipts until the remote run is terminal', async () => {
  const f = fixture(async (input) => {
    if (input.name === OPERATION_TARGETS.evolve_chain) {
      return success({
        status: 'completed',
        request_id: 'start-1',
        receipt: {
          status: 'started',
          evolution_id: 'evo-7',
          evolution_status: 'running',
        },
      })
    }
    assert.equal(input.name, STATUS_TOOL)
    return success({
      status: 'completed',
      request_id: 'status-1',
      receipt: {
        evolution_id: 'evo-7',
        status: 'completed',
        terminal: true,
        generation: 4,
      },
    })
  })

  await f.definition.execute({
    operation: 'evolve_chain',
    arguments: {
      idempotency_key: 'evolve-123',
      base_chain_id: 'base-1',
      chain: { artifact_id: 'chain-1', content_sha256: 'b'.repeat(64) },
    },
  }, f.exec)
  const outcome = await f.hooks.done
  assert.deepEqual(outcome, { status: 'completed', detail: 'evolution completed' })
  assert.deepEqual(f.calls.map(call => call.name), [OPERATION_TARGETS.evolve_chain, STATUS_TOOL])
  assert.match(f.hooks.readOutput(), /"generation":4/)
})

test('an unavailable evolution status settles failed instead of looping or completing', async () => {
  const f = fixture(async (input) => {
    if (input.name === OPERATION_TARGETS.evolve_chain) {
      return evolutionStartSuccess({ evolutionId: 'evo-unavailable-status' })
    }
    assert.equal(input.name, STATUS_TOOL)
    return success({
      status: 'unavailable',
      request_id: 'status-unavailable',
      receipt: null,
      message: 'status backend disabled',
    })
  })

  await f.definition.execute({
    operation: 'evolve_chain',
    arguments: { idempotency_key: 'evolve-status-unavailable' },
  }, f.exec)

  assert.deepEqual(await f.hooks.done, {
    status: 'failed',
    detail: 'evolution status unavailable: status backend disabled',
  })
  assert.deepEqual(f.calls.map(call => call.name), [OPERATION_TARGETS.evolve_chain, STATUS_TOOL])
})

test('evolution cancellation waits for a run id, then calls the typed cancel tool', async () => {
  const start = deferred()
  let startSignal
  const f = fixture(async (input) => {
    if (input.name === OPERATION_TARGETS.evolve_chain) {
      startSignal = input.signal
      return await start.promise
    }
    assert.equal(input.name, CANCEL_TOOL)
    return success({
      status: 'completed',
      request_id: 'cancel-1',
      receipt: {
        status: 'cancel_requested',
        evolution_id: 'evo-9',
        cancellation_dispatched: true,
        evolution_status: 'stopping',
      },
    })
  })

  await f.definition.execute({
    operation: 'evolve_chain',
    arguments: {
      idempotency_key: 'evolve-456',
      base_chain_id: 'base-1',
      chain: { artifact_id: 'chain-1', content_sha256: 'c'.repeat(64) },
    },
  }, f.exec)
  await nextTask()
  f.hooks.cancel('stop')
  assert.equal(startSignal.aborted, false)
  start.resolve(success({
    status: 'completed',
    request_id: 'start-2',
    receipt: {
      status: 'started',
      evolution_id: 'evo-9',
      evolution_status: 'running',
    },
  }))

  const outcome = await f.hooks.done
  assert.deepEqual(outcome, {
    status: 'killed',
    detail: 'remote evolution cancellation requested',
  })
  assert.deepEqual(f.calls.map(call => call.name), [OPERATION_TARGETS.evolve_chain, CANCEL_TOOL])
  assert.equal(f.calls[1].arguments.evolution_id, 'evo-9')
  assert.match(f.calls[1].arguments.idempotency_key, /^maestro-job-cancel:[0-9a-f-]{36}$/)
})

test('the tool rejects unsupported operations and agent-less execution before starting work', async () => {
  const f = fixture(async () => success({ status: 'completed' }))
  await assert.rejects(
    f.definition.execute({ operation: 'deploy_chain', arguments: {} }, f.exec),
    /operation must be/,
  )
  await assert.rejects(
    f.definition.execute(
      { operation: 'generate_chain', arguments: {} },
      { ...f.exec, agent: undefined },
    ),
    /requires an owning Agent/,
  )
  assert.equal(f.calls.length, 0)
})
