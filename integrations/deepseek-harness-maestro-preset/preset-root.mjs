import { fileURLToPath } from 'node:url'
import { syncBuiltinESMExports } from 'node:module'

// The pinned SEA carrier patches fs after creating its ESM exports. Refresh
// the live bindings before discovery uses the packaged filesystem adapter.
// https://github.com/deepseek-ai/deepseek-harness/discussions/5629
syncBuiltinESMExports()

export const name = 'maestro-agent-preset-root'

const presetRoot = fileURLToPath(new URL('./agent-presets/', import.meta.url))

/** Publish the package-relative preset root before the roster evaluates config. */
export function apply(ctx) {
  ctx.provide('maestroAgentPresetRoot', presetRoot)
}
