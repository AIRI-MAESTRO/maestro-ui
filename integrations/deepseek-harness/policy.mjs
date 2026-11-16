/**
 * Host-side approval policy for MAESTRO tools bridged through MCP.
 *
 * The explicit read-only set delegates to the rest of the Harness policy
 * waterfall. Every other tool in MAESTRO's namespace short-circuits to
 * `ask`. DeepSeek Harness turns `ask` into a one-shot user approval and fails
 * closed when no approval service, agent, or answer channel is available.
 */

import { readFileSync } from 'node:fs'

const manifest = JSON.parse(
  readFileSync(new URL('./maestro-harness.json', import.meta.url), 'utf8'),
)

function requireString(value, field) {
  if (typeof value !== 'string' || value.length === 0) {
    throw new Error(`maestro-care-tool-policy: ${field} must be a non-empty string`)
  }
  return value
}

function requireStringArray(value, field) {
  if (!Array.isArray(value) || value.some(item => typeof item !== 'string' || item.length === 0)) {
    throw new Error(`maestro-care-tool-policy: ${field} must be an array of non-empty strings`)
  }
  if (new Set(value).size !== value.length) {
    throw new Error(`maestro-care-tool-policy: ${field} must not contain duplicates`)
  }
  return value
}

if (manifest.schemaVersion !== 1) {
  throw new Error('maestro-care-tool-policy: unsupported manifest schemaVersion')
}
if (manifest.maestroToolDefaultDecision !== 'ask') {
  throw new Error('maestro-care-tool-policy: the default MAESTRO decision must be ask')
}

export const MAESTRO_TOOL_PREFIX = requireString(
  manifest.publicToolPrefix,
  'publicToolPrefix',
)

const serverName = requireString(manifest.mcpServerName, 'mcpServerName')
if (!/^[A-Za-z0-9_-]{1,32}$/.test(serverName)) {
  throw new Error('maestro-care-tool-policy: mcpServerName is not a valid DSH MCP namespace')
}
if (MAESTRO_TOOL_PREFIX !== `mcp__${serverName}__`) {
  throw new Error('maestro-care-tool-policy: publicToolPrefix does not match mcpServerName')
}

const readOnlyRawTools = requireStringArray(
  manifest.readOnlyRawTools,
  'readOnlyRawTools',
)
if (readOnlyRawTools.some(rawName => !/^maestro_[A-Za-z0-9_-]+$/.test(rawName))) {
  throw new Error('maestro-care-tool-policy: readOnlyRawTools contains an invalid MAESTRO name')
}

export const READ_ONLY_MAESTRO_TOOLS = Object.freeze(
  readOnlyRawTools.map(
    rawName => `${MAESTRO_TOOL_PREFIX}${rawName}`,
  ),
)

const readOnlyTools = new Set(READ_ONLY_MAESTRO_TOOLS)

/**
 * Pure classifier kept exported so the security boundary can be tested
 * without booting a model, web server, or MCP child.
 */
export function classifyMaestroTool(toolName) {
  if (typeof toolName !== 'string' || !toolName.startsWith(MAESTRO_TOOL_PREFIX)) {
    return 'delegate'
  }
  return readOnlyTools.has(toolName) ? 'delegate' : 'ask'
}

export const name = 'maestro-care-tool-policy'
export const inject = ['tools']

export function apply(ctx) {
  ctx.on('tools/pre-execute', (execution, next) => {
    if (classifyMaestroTool(execution.name) === 'delegate') return next()
    return Promise.resolve({
      kind: 'ask',
      reason: `MAESTRO operation "${execution.name}" may change state or start paid work.`,
    })
  }, { prepend: true })
}
