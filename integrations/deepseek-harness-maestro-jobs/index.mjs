/**
 * User-visible background jobs for long MAESTRO MCP operations.
 *
 * This plugin deliberately does not wrap `tools/execute`: an around-dispatch
 * wrapper must await `next()` to quiescence and cannot detach that work. Instead
 * it registers one explicit producer tool. The producer itself receives the
 * user's one-shot approval, transfers ownership to `ctx.jobs`, and dispatches a
 * fixed allowlist of already-registered MAESTRO MCP tools through the public
 * ToolRuntime API with a job-owned AbortSignal.
 *
 * @module @maestro-care/deepseek-harness-maestro-jobs
 */

import { randomUUID } from 'node:crypto'

export const name = 'maestro-care-background-jobs'
export const inject = ['tools', 'jobs']

export const BACKGROUND_TOOL_NAME = 'maestro_background_run'
export const TESTED_DSH_COMMIT = 'dd6322d604e00eec1ba5e0c8541159906a21094a'

const MAESTRO_PREFIX = 'mcp__maestro__'
const JOB_KIND = 'maestro'
const OUTPUT_LIMIT_BYTES = 256 * 1024
const PENDING_OUTPUT_LIMIT_BYTES = 512 * 1024
const EVOLUTION_POLL_INTERVAL_MS = 2_000

const OPERATIONS = Object.freeze({
  generate_chain: Object.freeze({
    target: `${MAESTRO_PREFIX}maestro_generate_chain`,
    label: 'Generate and admit a CARL chain',
  }),
  execute_chain: Object.freeze({
    target: `${MAESTRO_PREFIX}maestro_execute_chain`,
    label: 'Execute a digest-pinned CARL chain',
  }),
  evolve_chain: Object.freeze({
    target: `${MAESTRO_PREFIX}maestro_evolution_start`,
    statusTarget: `${MAESTRO_PREFIX}maestro_evolution_status`,
    cancelTarget: `${MAESTRO_PREFIX}maestro_evolution_cancel`,
    label: 'Evolve a digest-pinned CARL chain',
  }),
})

export const OPERATION_TARGETS = Object.freeze(Object.fromEntries(
  Object.entries(OPERATIONS).map(([operation, spec]) => [operation, spec.target]),
))

const EVOLUTION_STATUSES = new Set([
  'unknown',
  'prepared',
  'pending',
  'queued',
  'dispatching',
  'initializing',
  'preparing',
  'running',
  'paused',
  'stopping',
  'completed',
  'failed',
  'cancelled',
])
const TERMINAL_EVOLUTION_STATUSES = new Set(['completed', 'failed', 'cancelled'])
const SHA256_PATTERN = /^[0-9a-f]{64}$/
const EVOLUTION_ID_PATTERN = /^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$/
const encoder = new TextEncoder()
const decoder = new TextDecoder()

function isRecord(value) {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

function validateStartArguments(value) {
  if (!isRecord(value)) throw new Error('maestro_background_run arguments must be an object')
  const keys = Object.keys(value)
  if (keys.some(key => key !== 'operation' && key !== 'arguments')) {
    throw new Error('maestro_background_run accepts only operation and arguments')
  }
  if (!Object.hasOwn(OPERATIONS, value.operation)) {
    throw new Error('operation must be generate_chain, execute_chain, or evolve_chain')
  }
  if (!isRecord(value.arguments)) throw new Error('arguments must be a JSON object')
  return { operation: value.operation, arguments: value.arguments }
}

function expectedMaestroApprovalReason(toolName) {
  return `MAESTRO operation "${toolName}" may change state or start paid work.`
}

function safeErrorMessage(result) {
  const message = result?.error?.message
  if (typeof message === 'string' && message.length > 0) return message.slice(0, 2_000)
  return 'MAESTRO tool failed without a structured error message'
}

function textFromResult(result) {
  const content = Array.isArray(result?.content) ? result.content : []
  const text = content
    .filter(block => isRecord(block) && block.type === 'text' && typeof block.text === 'string')
    .map(block => block.text)
    .join('\n')
  if (text.length > 0) return text
  try {
    return JSON.stringify(result?.value ?? null)
  } catch {
    return '(result was not serializable)'
  }
}

function structuredContent(result) {
  const value = result?.value
  return isRecord(value) && isRecord(value.structuredContent)
    ? value.structuredContent
    : undefined
}

function unavailableDetail(operation, payload) {
  const message = payload.message
  const suffix = typeof message === 'string' && message.length > 0
    ? `: ${message.slice(0, 2_000)}`
    : ''
  return `${operation} unavailable${suffix}`
}

function completedReceipt(result, operation) {
  if (result?.isError === true) {
    return {
      kind: 'tool-error',
      detail: `${operation} failed: ${safeErrorMessage(result)}`,
    }
  }
  const payload = structuredContent(result)
  if (!isRecord(payload)) {
    return { kind: 'invalid', detail: `${operation} returned no structured result` }
  }
  if (payload.status === 'unavailable') {
    return { kind: 'unavailable', detail: unavailableDetail(operation, payload) }
  }
  if (payload.status !== 'completed') {
    return {
      kind: 'invalid',
      detail: `${operation} returned invalid result status`,
    }
  }
  if (typeof payload.request_id !== 'string' || payload.request_id.length === 0) {
    return { kind: 'invalid', detail: `${operation} returned no request id` }
  }
  if (!isRecord(payload.receipt)) {
    return { kind: 'invalid', detail: `${operation} returned no receipt` }
  }
  return { kind: 'receipt', receipt: payload.receipt }
}

function isArtifactReference(value) {
  return isRecord(value)
    && typeof value.artifact_id === 'string'
    && value.artifact_id.length > 0
    && typeof value.content_sha256 === 'string'
    && SHA256_PATTERN.test(value.content_sha256)
}

function singleOperationReceipt(operation, result) {
  const envelope = completedReceipt(result, operation)
  if (envelope.kind !== 'receipt') return envelope
  const receipt = envelope.receipt

  if (operation === 'generate_chain') {
    if (receipt.status !== 'ready' || !isArtifactReference(receipt.artifact)) {
      return {
        kind: 'invalid',
        detail: 'generate_chain returned an invalid generation receipt',
      }
    }
    return { kind: 'success', receipt }
  }

  if (operation === 'execute_chain') {
    if (receipt.status !== 'completed'
      || typeof receipt.chain_digest !== 'string'
      || !SHA256_PATTERN.test(receipt.chain_digest)
      || !isArtifactReference(receipt.output_artifact)) {
      return {
        kind: 'invalid',
        detail: 'execute_chain returned an invalid execution receipt',
      }
    }
    if (receipt.success !== true) {
      return {
        kind: 'unsuccessful',
        detail: 'execute_chain completed with an unsuccessful chain result',
      }
    }
    return { kind: 'success', receipt }
  }

  return { kind: 'invalid', detail: `${operation} has no receipt validator` }
}

function boundUtf8Tail(text, maximumBytes) {
  const encoded = encoder.encode(text)
  if (encoded.byteLength <= maximumBytes) return text
  const notice = '[earlier MAESTRO job output omitted]\n'
  const noticeBytes = encoder.encode(notice).byteLength
  return notice + decoder.decode(encoded.slice(encoded.byteLength - (maximumBytes - noticeBytes)))
}

function createOutputBuffer() {
  let pending = ''
  return {
    append(text) {
      const line = text.endsWith('\n') ? text : `${text}\n`
      pending = boundUtf8Tail(pending + line, PENDING_OUTPUT_LIMIT_BYTES)
    },
    read() {
      const output = pending
      pending = ''
      return output
    },
  }
}

function createAbortableDelay(ms, cancellation) {
  if (cancellation.requested) return Promise.resolve()
  return new Promise(resolve => {
    let settled = false
    let unsubscribe = () => {}
    const finish = () => {
      if (settled) return
      settled = true
      clearTimeout(timer)
      unsubscribe()
      resolve()
    }
    const timer = setTimeout(finish, ms)
    timer.unref?.()
    unsubscribe = cancellation.subscribe(finish)
  })
}

function createCancellationState() {
  let requested = false
  let activeController
  let phase = 'idle'
  const subscribers = new Set()
  return {
    get requested() { return requested },
    setPhase(next, controller) {
      phase = next
      activeController = controller
      if (requested && phase !== 'evolution-start') controller?.abort(new Error('MAESTRO job cancelled'))
    },
    request() {
      if (requested) return
      requested = true
      if (phase !== 'evolution-start') activeController?.abort(new Error('MAESTRO job cancelled'))
      for (const subscriber of subscribers) subscriber()
      subscribers.clear()
    },
    subscribe(subscriber) {
      if (requested) {
        subscriber()
        return () => {}
      }
      subscribers.add(subscriber)
      return () => subscribers.delete(subscriber)
    },
  }
}

function publicStartResult(operation, jobId) {
  return { kind: 'background', operation, job_id: jobId }
}

function operationOutput(operation, result, output) {
  output.append(textFromResult(result))
  const validation = singleOperationReceipt(operation, result)
  return validation.kind === 'success'
    ? { status: 'completed', detail: `${operation} completed` }
    : { status: 'failed', detail: validation.detail }
}

function evolutionStatusPayload(result) {
  const envelope = completedReceipt(result, 'evolution status')
  if (envelope.kind !== 'receipt') return envelope
  const receipt = envelope.receipt
  if (typeof receipt.evolution_id !== 'string'
    || !EVOLUTION_ID_PATTERN.test(receipt.evolution_id)
    || typeof receipt.status !== 'string'
    || !EVOLUTION_STATUSES.has(receipt.status)
    || typeof receipt.terminal !== 'boolean'
    || receipt.terminal !== TERMINAL_EVOLUTION_STATUSES.has(receipt.status)) {
    return {
      kind: 'invalid',
      detail: 'evolution status returned an invalid snapshot receipt',
    }
  }
  return { kind: 'success', receipt }
}

function evolutionStartPayload(result) {
  const envelope = completedReceipt(result, 'evolution start')
  if (envelope.kind !== 'receipt') return envelope
  const receipt = envelope.receipt
  if ((receipt.status !== 'started' && receipt.status !== 'cancel_requested')
    || typeof receipt.evolution_id !== 'string'
    || !EVOLUTION_ID_PATTERN.test(receipt.evolution_id)
    || typeof receipt.evolution_status !== 'string'
    || !EVOLUTION_STATUSES.has(receipt.evolution_status)) {
    return {
      kind: 'invalid',
      detail: 'evolution start returned an invalid start receipt',
    }
  }
  return { kind: 'success', receipt }
}

function evolutionCancelPayload(result, evolutionId) {
  const envelope = completedReceipt(result, 'evolution cancel')
  if (envelope.kind !== 'receipt') return envelope
  const receipt = envelope.receipt
  if (receipt.status !== 'cancel_requested'
    || receipt.evolution_id !== evolutionId
    || receipt.cancellation_dispatched !== true
    || typeof receipt.evolution_status !== 'string'
    || !EVOLUTION_STATUSES.has(receipt.evolution_status)) {
    return {
      kind: 'invalid',
      detail: 'evolution cancel returned an invalid acknowledgement receipt',
    }
  }
  return { kind: 'success', receipt }
}

/** Register the background producer and its one-shot authorization transfer. */
export function apply(ctx) {
  // An unforgeable ToolExecutionToken is the capability: only this tool body
  // receives it, and only calls made on behalf of the same Agent/root call are
  // eligible to consume the already-approved MAESTRO decision.
  const grants = new Map()

  ctx.on('tools/pre-execute', async (exec, next) => {
    if (exec.name === BACKGROUND_TOOL_NAME) {
      const downstream = await next()
      if (downstream.kind !== 'allow') return downstream
      return {
        kind: 'ask',
        reason: 'Start approved MAESTRO provider work as a cancellable background job.',
      }
    }

    const grant = exec.parent === undefined ? undefined : grants.get(exec.parent)
    if (grant === undefined
      || exec.agent !== grant.agent
      || exec.rootCallId !== grant.rootCallId
      || !grant.tools.has(exec.name)) return next()

    const downstream = await next()
    // Preserve every foreign denial/approval. Only consume the exact ask made
    // by the companion MAESTRO MCP policy for this exact already-approved call.
    if (downstream.kind === 'ask'
      && downstream.reason === expectedMaestroApprovalReason(exec.name)) {
      return { kind: 'allow' }
    }
    return downstream
  }, { prepend: true })

  ctx.tools.register({
    name: BACKGROUND_TOOL_NAME,
    description: 'Start long MAESTRO work as a real background job. Use generate_chain or execute_chain for one MCP operation; evolve_chain stays live and reports real MAESTRO evolution status until terminal. The returned maestro-N id works with job_output, job_list, and job_kill. Starting may incur provider cost and always asks for one-shot approval.',
    parameters: {
      type: 'object',
      additionalProperties: false,
      properties: {
        operation: {
          type: 'string',
          enum: ['generate_chain', 'execute_chain', 'evolve_chain'],
          description: 'The fixed MAESTRO operation to run in the background.',
        },
        arguments: {
          type: 'object',
          description: 'Arguments for the mapped MAESTRO MCP tool. Its strict schema is still enforced at dispatch.',
        },
      },
      required: ['operation', 'arguments'],
    },
    output: {
      schema: {
        type: 'object',
        additionalProperties: false,
        properties: {
          kind: { type: 'string', enum: ['background'] },
          operation: { type: 'string', enum: ['generate_chain', 'execute_chain', 'evolve_chain'] },
          job_id: { type: 'string' },
        },
        required: ['kind', 'operation', 'job_id'],
      },
      render(_args, value) {
        return [{
          type: 'text',
          text: `Started MAESTRO background job ${value.job_id} (${value.operation}). Use job_output to collect it or job_kill to cancel it.`,
        }]
      },
    },
    presentCall(rawArgs) {
      const operation = isRecord(rawArgs) && typeof rawArgs.operation === 'string'
        ? rawArgs.operation
        : 'operation'
      return { card: 'generic', title: `Start MAESTRO ${operation} job`, kind: 'execute' }
    },
    presentResult(_args, result) {
      const value = result?.isError === false && isRecord(result.value) ? result.value : undefined
      const jobId = typeof value?.job_id === 'string' ? value.job_id : undefined
      return {
        card: 'generic',
        title: jobId === undefined ? 'MAESTRO background job' : `MAESTRO job ${jobId}`,
        kind: 'execute',
      }
    },
    async execute(rawArgs, exec) {
      const { operation, arguments: operationArguments } = validateStartArguments(rawArgs)
      if (exec.agent === undefined) {
        throw new Error('maestro_background_run requires an owning Agent')
      }
      if (exec.signal.aborted) throw new Error('maestro_background_run was cancelled before job registration')

      const spec = OPERATIONS[operation]
      const output = createOutputBuffer()
      const cancellation = createCancellationState()
      let callSequence = 0

      const allowedTools = new Set([spec.target])
      if (operation === 'evolve_chain') {
        allowedTools.add(spec.statusTarget)
        allowedTools.add(spec.cancelTarget)
      }

      const dispatch = async (toolName, args, phase, controller = new AbortController()) => {
        cancellation.setPhase(phase, controller)
        callSequence += 1
        return await ctx.tools.execute({
          callId: `${String(exec.callId)}:maestro-job:${callSequence}`,
          rootCallId: exec.rootCallId,
          name: toolName,
          arguments: args,
          agent: exec.agent,
          parent: exec.token,
          signal: controller.signal,
        })
      }

      const runEvolution = async () => {
        output.append('Submitting the MAESTRO evolution run.')
        const started = await dispatch(spec.target, operationArguments, 'evolution-start')
        output.append(textFromResult(started))
        const startValidation = evolutionStartPayload(started)
        if (startValidation.kind !== 'success') {
          return { status: 'failed', detail: startValidation.detail }
        }
        const startReceipt = startValidation.receipt
        const evolutionId = startReceipt.evolution_id

        const requestRemoteCancellation = async () => {
          output.append(`Requesting cancellation of evolution ${evolutionId}.`)
          const cancelled = await dispatch(spec.cancelTarget, {
            idempotency_key: `maestro-job-cancel:${randomUUID()}`,
            evolution_id: evolutionId,
          }, 'evolution-cancel', new AbortController())
          output.append(textFromResult(cancelled))
          const cancelValidation = evolutionCancelPayload(cancelled, evolutionId)
          if (cancelValidation.kind !== 'success') {
            return {
              status: 'failed',
              detail: `${cancelValidation.detail}; inspect the evolution status`,
            }
          }
          return { status: 'killed', detail: 'remote evolution cancellation requested' }
        }

        if (cancellation.requested || startReceipt.status === 'cancel_requested') {
          return await requestRemoteCancellation()
        }

        let lastSnapshot = ''
        for (;;) {
          if (cancellation.requested) return await requestRemoteCancellation()
          const status = await dispatch(spec.statusTarget, { evolution_id: evolutionId }, 'evolution-status')
          if (cancellation.requested) return await requestRemoteCancellation()
          if (status.isError) {
            output.append(textFromResult(status))
            return { status: 'failed', detail: `evolution status failed: ${safeErrorMessage(status)}` }
          }
          const statusValidation = evolutionStatusPayload(status)
          if (statusValidation.kind !== 'success') {
            output.append(textFromResult(status))
            return { status: 'failed', detail: statusValidation.detail }
          }
          const receipt = statusValidation.receipt
          if (receipt.evolution_id !== evolutionId) {
            output.append(textFromResult(status))
            return {
              status: 'failed',
              detail: 'evolution status returned a receipt for a different run',
            }
          }
          const snapshot = JSON.stringify(receipt)
          if (snapshot !== lastSnapshot) {
            output.append(snapshot)
            lastSnapshot = snapshot
          }
          if (TERMINAL_EVOLUTION_STATUSES.has(receipt.status)) {
            if (receipt.status === 'completed') return { status: 'completed', detail: 'evolution completed' }
            if (receipt.status === 'cancelled') return { status: 'killed', detail: 'evolution cancelled' }
            return { status: 'failed', detail: 'evolution failed' }
          }
          await createAbortableDelay(EVOLUTION_POLL_INTERVAL_MS, cancellation)
        }
      }

      const run = async () => {
        if (operation === 'evolve_chain') return await runEvolution()
        output.append(`Dispatching ${operation}.`)
        const result = await dispatch(spec.target, operationArguments, operation)
        const outcome = operationOutput(operation, result, output)
        if (cancellation.requested) {
          if (outcome.status === 'completed') {
            output.append(`${operation} returned a successful receipt before cancellation took effect.`)
            return { status: 'completed', detail: `${operation} completed before cancellation took effect` }
          }
          if (result?.isError !== true) return outcome
          output.append(`${operation} request stopped; replay its idempotency key to resolve any uncertain remote outcome.`)
          return { status: 'killed', detail: `${operation} request stopped; remote outcome may be uncertain` }
        }
        return outcome
      }

      const jobId = ctx.jobs.start({
        kind: JOB_KIND,
        label: spec.label,
        outputLimitBytes: OUTPUT_LIMIT_BYTES,
        owner: exec.agent,
        run: () => {
          grants.set(exec.token, {
            agent: exec.agent,
            rootCallId: exec.rootCallId,
            tools: allowedTools,
          })
          const done = Promise.resolve()
            .then(run)
            .catch(error => {
              output.append(`MAESTRO background job failed: ${error instanceof Error ? error.message.slice(0, 2_000) : String(error).slice(0, 2_000)}`)
              return { status: cancellation.requested ? 'killed' : 'failed', detail: 'MAESTRO background job failed' }
            })
            .finally(() => {
              grants.delete(exec.token)
            })
          return {
            cancel: () => {
              output.append('Cancellation requested for the MAESTRO background job.')
              cancellation.request()
            },
            done,
            readOutput: () => output.read(),
          }
        },
      })

      return publicStartResult(operation, jobId)
    },
  })
}
