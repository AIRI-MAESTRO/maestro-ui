/** Resolve the bundle's pinned DSH imports from the sibling source checkout. */

import { existsSync } from 'node:fs'
import { resolve as resolvePath } from 'node:path'
import { fileURLToPath, pathToFileURL } from 'node:url'

const defaultSource = fileURLToPath(new URL('../../../../deepseek-harness/', import.meta.url))
const source = resolvePath(process.env.DSH_SOURCE ?? defaultSource)

const mappings = new Map([
  ['@deepseek-ai/cordis', 'vendor/cordis/lib/index.js'],
  ['@deepseek-ai/dsh-credentials', 'packages/credentials/credentials/lib/index.js'],
  ['@deepseek-ai/dsh-launch-environment', 'packages/util/launch-environment/lib/index.js'],
  ['@deepseek-ai/schemastery', 'vendor/schemastery/lib/index.mjs'],
])

export async function resolve(specifier, context, nextResolve) {
  const relative = mappings.get(specifier)
  if (relative === undefined) return nextResolve(specifier, context)
  const target = resolvePath(source, relative)
  if (!existsSync(target)) {
    throw new Error(`pinned DeepSeek Harness build artifact is missing: ${target}`)
  }
  return { url: pathToFileURL(target).href, shortCircuit: true }
}
