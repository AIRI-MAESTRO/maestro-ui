/** Build the dependency-free N-API bridge against macOS Security.framework. */

import { spawnSync } from 'node:child_process'
import { mkdirSync, statSync } from 'node:fs'
import { dirname, isAbsolute, join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..')

if (process.platform !== 'darwin') {
  throw new Error(
    `maestro-keychain: cannot build on ${process.platform}; this provider requires macOS Security.framework`,
  )
}

function isDirectory(path) {
  try {
    return statSync(path).isDirectory()
  } catch {
    return false
  }
}

const configured = process.env.MAESTRO_NODE_HEADERS
const executablePrefix = resolve(dirname(process.execPath), '..')
const candidates = [
  configured,
  process.config.variables.nodedir === undefined
    ? undefined
    : join(process.config.variables.nodedir, 'include', 'node'),
  join(executablePrefix, 'include', 'node'),
  '/opt/homebrew/include/node',
  '/usr/local/include/node',
].filter(path => typeof path === 'string' && path.length > 0)

const headers = candidates.find(isDirectory)
if (headers === undefined) {
  throw new Error(
    'maestro-keychain: Node headers were not found; set MAESTRO_NODE_HEADERS to the directory containing node_api.h',
  )
}

const configuredOutput = process.env.MAESTRO_KEYCHAIN_OUTPUT
if (configuredOutput !== undefined && !isAbsolute(configuredOutput)) {
  throw new Error('maestro-keychain: MAESTRO_KEYCHAIN_OUTPUT must be an absolute path')
}
const output = configuredOutput ?? join(root, 'build', 'Release', 'maestro_keychain.node')
const outputDir = dirname(output)
mkdirSync(outputDir, { recursive: true })

const args = [
  '-std=c++17',
  '-O2',
  '-fvisibility=hidden',
  '-DNAPI_VERSION=8',
  '-DNODE_GYP_MODULE_NAME=maestro_keychain',
  `-I${headers}`,
  '-bundle',
  '-undefined',
  'dynamic_lookup',
  '-framework',
  'Security',
  '-framework',
  'CoreFoundation',
  join(root, 'native', 'keychain.cc'),
  '-o',
  output,
]

const result = spawnSync(process.env.CXX ?? 'clang++', args, { stdio: 'inherit' })
if (result.error !== undefined) throw result.error
if (result.status !== 0) process.exit(result.status ?? 1)
process.stdout.write(`maestro-keychain: built ${output}\n`)
