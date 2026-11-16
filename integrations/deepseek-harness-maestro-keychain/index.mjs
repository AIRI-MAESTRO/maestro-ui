/**
 * DeepSeek Harness credential provider backed by macOS generic-password
 * Keychain items. Provider values stay behind the existing credentials seam:
 * configuration surfaces receive only configured/source/writable facts.
 *
 * @module @maestro-care/dsh-credentials-keychain
 */

import { createRequire } from 'node:module'
import { isAbsolute } from 'node:path'
import { TextDecoder } from 'node:util'
import { CredentialProvider, parseCredentialKey } from '@deepseek-ai/dsh-credentials'
import { launchEnvironmentOf } from '@deepseek-ai/dsh-launch-environment'
import z from '@deepseek-ai/schemastery'

const require = createRequire(import.meta.url)

/** Default Keychain service. Accounts below it carry versioned ref/record prefixes. */
export const DEFAULT_KEYCHAIN_SERVICE = 'app.maestro.care.deepseek-harness'

/** Source label exposed by UI-safe `describe`; it never names an item or value. */
export const KEYCHAIN_SOURCE = 'macos-keychain'

const REF_ACCOUNT_PREFIX = 'dsh-v1:ref:'
const RECORD_ACCOUNT_PREFIX = 'dsh-v1:record:'
const REF_PATTERN = /^[A-Za-z_][A-Za-z0-9_]*$/
const UTF8 = new TextDecoder('utf-8', { fatal: true })

/**
 * Fail before loading a native artifact on unsupported platforms.
 * @param {string} platform - Node platform id.
 * @returns {void}
 */
export function assertSupportedPlatform(platform) {
  if (platform !== 'darwin') {
    throw new Error(
      `maestro-keychain credentials are unavailable on ${platform}; this provider requires macOS Security.framework`,
    )
  }
}

/**
 * Validate the tiny native API before any credential operation reaches it.
 * @param {unknown} candidate - loaded addon candidate.
 * @returns {{get(service: string, account: string): Buffer | null, set(service: string, account: string, value: Buffer): void, delete(service: string, account: string): boolean, listAccounts(service: string): string[]}}
 */
export function assertNativeBackend(candidate) {
  if (typeof candidate !== 'object' || candidate === null) {
    throw new TypeError('maestro-keychain native addon did not export an object')
  }
  for (const method of ['get', 'set', 'delete', 'listAccounts']) {
    if (typeof candidate[method] !== 'function') {
      throw new TypeError(`maestro-keychain native addon is missing ${method}()`)
    }
  }
  return candidate
}

/**
 * Load the architecture/Node-ABI-specific addon after checking the host OS.
 * @param {{platform?: string, modulePath?: string}} [options] - testable loader inputs.
 * @returns {ReturnType<typeof assertNativeBackend>}
 */
export function loadNativeBackend(options = {}) {
  const platform = options.platform ?? process.platform
  assertSupportedPlatform(platform)
  const modulePath = options.modulePath ?? './build/Release/maestro_keychain.node'
  try {
    return assertNativeBackend(require(modulePath))
  } catch (error) {
    const code = error?.code
    if (code === 'MODULE_NOT_FOUND' || code === 'ERR_DLOPEN_FAILED') {
      throw new Error(
        'maestro-keychain native addon is unavailable for this Node/macOS architecture; run `npm run build:native` in the bundle before installing it',
      )
    }
    throw error
  }
}

/** Decode a Keychain result without allowing parser diagnostics to quote it. */
function decodeUtf8(data, subject) {
  if (!Buffer.isBuffer(data)) {
    throw new TypeError(`maestro-keychain returned non-binary data for ${subject}`)
  }
  try {
    return UTF8.decode(data)
  } catch {
    throw new Error(`maestro-keychain stored invalid UTF-8 for ${subject}`)
  } finally {
    data.fill(0)
  }
}

/** Reject non-JSON values, cycles, accessors, non-finite numbers, and exotic objects. */
function assertJsonValue(value, subject, seen = new Set()) {
  if (value === null || typeof value === 'string' || typeof value === 'boolean') return
  if (typeof value === 'number') {
    if (Number.isFinite(value)) return
    throw new TypeError(`maestro-keychain ${subject} contains a non-finite number`)
  }
  if (typeof value !== 'object' || (!Array.isArray(value) && Object.getPrototypeOf(value) !== Object.prototype)) {
    throw new TypeError(`maestro-keychain ${subject} contains a value JSON cannot represent`)
  }
  if (seen.has(value)) throw new TypeError(`maestro-keychain ${subject} is cyclic`)
  seen.add(value)
  const descriptors = Object.getOwnPropertyDescriptors(value)
  for (const descriptor of Object.values(descriptors)) {
    if ('get' in descriptor || 'set' in descriptor) {
      throw new TypeError(`maestro-keychain ${subject} contains an accessor`)
    }
    assertJsonValue(descriptor.value, subject, seen)
  }
  seen.delete(value)
}

/** Admit a record at the Keychain durable boundary without quoting values. */
function assertCredentialRecord(record, key) {
  if (typeof record !== 'object' || record === null || Array.isArray(record)) {
    throw new TypeError(`maestro-keychain record "${key}" must be an object`)
  }
  const fields = Object.keys(record)
  if (record.kind === 'api-key') {
    for (const field of fields) {
      if (!['kind', 'key', 'env'].includes(field)) {
        throw new TypeError(`maestro-keychain record "${key}" has unknown field "${field}"`)
      }
    }
    if (record.key !== undefined && (typeof record.key !== 'string' || record.key.length === 0)) {
      throw new TypeError(`maestro-keychain record "${key}" has an empty or non-string key`)
    }
    if (record.env !== undefined) {
      if (typeof record.env !== 'object' || record.env === null || Array.isArray(record.env)) {
        throw new TypeError(`maestro-keychain record "${key}" has a non-object env`)
      }
      for (const [name, value] of Object.entries(record.env)) {
        if (!REF_PATTERN.test(name) || typeof value !== 'string' || value.length === 0) {
          throw new TypeError(`maestro-keychain record "${key}" has an invalid environment entry named "${name}"`)
        }
      }
    }
    return
  }
  if (record.kind === 'grant') {
    for (const field of fields) {
      if (!['kind', 'payload'].includes(field)) {
        throw new TypeError(`maestro-keychain record "${key}" has unknown field "${field}"`)
      }
    }
    if (!Object.hasOwn(record, 'payload')) {
      throw new TypeError(`maestro-keychain record "${key}" has no payload`)
    }
    assertJsonValue(record.payload, `record "${key}" payload`)
    return
  }
  throw new TypeError(`maestro-keychain record "${key}" has an unknown kind`)
}

/** Parse one record while replacing JSON parser text with a value-free error. */
function parseStoredRecord(data, key) {
  const text = decodeUtf8(data, `record "${key}"`)
  let record
  try {
    record = JSON.parse(text)
  } catch {
    throw new Error(`maestro-keychain record "${key}" is not valid JSON`)
  }
  assertCredentialRecord(record, key)
  return record
}

/** Serialize one admitted record and wipe the intermediate bytes after use. */
function recordBytes(record, key) {
  assertCredentialRecord(record, key)
  return Buffer.from(JSON.stringify(record), 'utf8')
}

/** macOS Keychain Service Provider for the pinned DeepSeek Harness credentials seam. */
export class MacOSKeychainCredentialProvider extends CredentialProvider {
  static Config = z.object({
    service: z.string().default(DEFAULT_KEYCHAIN_SERVICE),
    nativeModulePath: z.string(),
  })

  /**
   * @param {import('@deepseek-ai/cordis').Context} ctx - Cordis context.
   * @param {{service?: string, nativeModulePath?: string}} config - plugin configuration.
   * @param {ReturnType<typeof assertNativeBackend>} [native] - hermetic test seam.
   */
  constructor(ctx, config = {}, native = undefined) {
    super(ctx)
    this.service = config.service ?? DEFAULT_KEYCHAIN_SERVICE
    if (typeof this.service !== 'string' || this.service.length === 0) {
      throw new TypeError('maestro-keychain service must be a non-empty string')
    }
    if (config.nativeModulePath !== undefined && !isAbsolute(config.nativeModulePath)) {
      throw new TypeError('maestro-keychain nativeModulePath must be absolute when configured')
    }
    this.native = assertNativeBackend(native ?? loadNativeBackend({ modulePath: config.nativeModulePath }))
    this.operations = Promise.resolve()
  }

  inherited(ref) {
    const entry = launchEnvironmentOf(this.ctx).getFrom(ref, ['process'])
    return entry !== undefined && entry.value.length > 0 ? entry.value : undefined
  }

  dotenvFallback(ref) {
    const entry = launchEnvironmentOf(this.ctx).getFrom(ref, ['project-env', 'user-env'])
    return entry !== undefined && entry.value.length > 0 ? entry : undefined
  }

  readRef(ref) {
    const data = this.native.get(this.service, `${REF_ACCOUNT_PREFIX}${ref}`)
    if (data === null) return undefined
    const value = decodeUtf8(data, `reference "${ref}"`)
    if (value.length === 0) throw new Error(`maestro-keychain stored an empty value for reference "${ref}"`)
    return value
  }

  async resolve(ref) {
    const inherited = this.inherited(ref)
    if (inherited !== undefined) return { value: inherited, source: 'env' }
    const stored = this.readRef(ref)
    if (stored !== undefined) return { value: stored, source: KEYCHAIN_SOURCE }
    const fallback = this.dotenvFallback(ref)
    if (fallback !== undefined) return { value: fallback.value, source: fallback.source }
    return undefined
  }

  async describe(ref) {
    if (this.inherited(ref) !== undefined) {
      return { configured: true, source: 'env', writable: false }
    }
    if (this.readRef(ref) !== undefined) {
      return { configured: true, source: KEYCHAIN_SOURCE, writable: true }
    }
    const fallback = this.dotenvFallback(ref)
    if (fallback !== undefined) {
      return { configured: true, source: fallback.source, writable: true }
    }
    return { configured: false, writable: true }
  }

  async set(ref, value) {
    if (value.length === 0) {
      throw new Error(`maestro-keychain cannot store an empty value for "${ref}"; use unset`)
    }
    await this.enqueue(async () => {
      this.assertUnshadowed(ref, 'set')
      const data = Buffer.from(value, 'utf8')
      try {
        this.native.set(this.service, `${REF_ACCOUNT_PREFIX}${ref}`, data)
      } finally {
        data.fill(0)
      }
      this.notifyUpdated(ref)
    })
  }

  async unset(ref) {
    await this.enqueue(async () => {
      this.assertUnshadowed(ref, 'unset')
      if (this.native.delete(this.service, `${REF_ACCOUNT_PREFIX}${ref}`)) this.notifyUpdated(ref)
    })
  }

  async readRecord(key) {
    const data = this.native.get(this.service, `${RECORD_ACCOUNT_PREFIX}${key}`)
    return data === null ? undefined : parseStoredRecord(data, key)
  }

  async describeRecord(key) {
    const record = await this.readRecord(key)
    return record === undefined
      ? { configured: false, writable: true }
      : { configured: true, kind: record.kind, writable: true }
  }

  async listRecords() {
    const entries = []
    for (const account of this.native.listAccounts(this.service)) {
      if (!account.startsWith(RECORD_ACCOUNT_PREFIX)) continue
      const key = parseCredentialKey(account.slice(RECORD_ACCOUNT_PREFIX.length))
      const record = await this.readRecord(key)
      if (record !== undefined) entries.push({ key, kind: record.kind })
    }
    return entries
  }

  modifyRecord(key, mutate) {
    return this.enqueue(async () => {
      const current = await this.readRecord(key)
      const next = await mutate(current)
      if (next === undefined) return current
      const data = recordBytes(next, key)
      try {
        this.native.set(this.service, `${RECORD_ACCOUNT_PREFIX}${key}`, data)
      } finally {
        data.fill(0)
      }
      this.notifyRecordUpdated(key)
      return next
    })
  }

  async deleteRecord(key) {
    await this.enqueue(async () => {
      if (this.native.delete(this.service, `${RECORD_ACCOUNT_PREFIX}${key}`)) this.notifyRecordUpdated(key)
    })
  }

  enqueue(operation) {
    const task = this.operations.then(operation)
    this.operations = task.then(() => undefined, () => undefined)
    return task
  }

  assertUnshadowed(ref, verb) {
    if (this.inherited(ref) !== undefined) {
      throw new Error(
        `maestro-keychain cannot ${verb} "${ref}" because the launching environment supplies it read-only`,
      )
    }
  }
}

export default MacOSKeychainCredentialProvider
