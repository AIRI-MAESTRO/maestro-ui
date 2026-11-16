import assert from 'node:assert/strict'
import { afterEach, describe, test } from 'node:test'
import { Context } from '@deepseek-ai/cordis'
import { credentialKey, credentialRef } from '@deepseek-ai/dsh-credentials'
import {
  createLaunchEnvironmentSnapshot,
  DSH_LAUNCH_ENVIRONMENT_KEY,
} from '@deepseek-ai/dsh-launch-environment'
import {
  KEYCHAIN_SOURCE,
  MacOSKeychainCredentialProvider,
  assertNativeBackend,
  assertSupportedPlatform,
  loadNativeBackend,
} from '../index.mjs'

class FakeNativeKeychain {
  constructor() {
    this.items = new Map()
  }

  address(service, account) {
    return `${service}\u0000${account}`
  }

  get(service, account) {
    const value = this.items.get(this.address(service, account))
    return value === undefined ? null : Buffer.from(value)
  }

  set(service, account, value) {
    assert.ok(Buffer.isBuffer(value))
    this.items.set(this.address(service, account), Buffer.from(value))
  }

  delete(service, account) {
    return this.items.delete(this.address(service, account))
  }

  listAccounts(service) {
    const prefix = `${service}\u0000`
    return [...this.items.keys()]
      .filter(address => address.startsWith(prefix))
      .map(address => address.slice(prefix.length))
  }
}

const disposals = []

afterEach(async () => {
  while (disposals.length > 0) await disposals.pop()()
})

async function boot(native, layers = [{ source: 'process', values: {} }]) {
  const ctx = new Context()
  ctx.provide(DSH_LAUNCH_ENVIRONMENT_KEY, createLaunchEnvironmentSnapshot(layers))
  class TestProvider extends MacOSKeychainCredentialProvider {
    constructor(innerCtx, config) {
      super(innerCtx, config, native)
    }
  }
  const fiber = ctx.plugin(TestProvider, { service: 'test.maestro.keychain' })
  disposals.push(() => fiber.dispose())
  await fiber
  return ctx
}

describe('native load boundary', () => {
  test('reports non-macOS unavailability before trying to load an addon', () => {
    assert.throws(() => assertSupportedPlatform('linux'), /requires macOS Security\.framework/)
    assert.throws(
      () => loadNativeBackend({ platform: 'linux', modulePath: '/must/not/load.node' }),
      /unavailable on linux/,
    )
  })

  test('rejects an incomplete native API', () => {
    assert.throws(() => assertNativeBackend({ get() {} }), /missing set\(\)/)
  })
})

describe('reference credentials', () => {
  test('stores, resolves, describes, and removes a value without returning it from describe', async () => {
    const native = new FakeNativeKeychain()
    const ctx = await boot(native)
    const ref = credentialRef('MAESTRO_TEST_API_KEY')
    const updates = []
    ctx.on('credentials/reference-updated', updated => updates.push(updated))

    await ctx.credentials.set(ref, 'secret-value-never-in-description')
    assert.deepEqual(await ctx.credentials.resolve(ref), {
      value: 'secret-value-never-in-description',
      source: KEYCHAIN_SOURCE,
    })
    const description = await ctx.credentials.describe(ref)
    assert.deepEqual(description, { configured: true, source: KEYCHAIN_SOURCE, writable: true })
    assert.equal(JSON.stringify(description).includes('secret-value'), false)

    await ctx.credentials.unset(ref)
    assert.equal(await ctx.credentials.resolve(ref), undefined)
    assert.deepEqual(updates, [ref, ref])
  })

  test('preserves DSH environment precedence and refuses shadowed writes', async () => {
    const native = new FakeNativeKeychain()
    const ctx = await boot(native, [
      { source: 'process', values: { MAESTRO_SHADOWED_KEY: 'from-env' } },
      { source: 'project-env', path: '/project/.env', values: { MAESTRO_FALLBACK_KEY: 'from-project' } },
    ])
    const shadowed = credentialRef('MAESTRO_SHADOWED_KEY')
    const fallback = credentialRef('MAESTRO_FALLBACK_KEY')

    assert.deepEqual(await ctx.credentials.describe(shadowed), {
      configured: true,
      source: 'env',
      writable: false,
    })
    await assert.rejects(ctx.credentials.set(shadowed, 'new-value'), /launching environment supplies it read-only/)
    assert.deepEqual(await ctx.credentials.resolve(fallback), { value: 'from-project', source: 'project-env' })

    await ctx.credentials.set(fallback, 'stored-wins')
    assert.deepEqual(await ctx.credentials.resolve(fallback), { value: 'stored-wins', source: KEYCHAIN_SOURCE })
  })

  test('rejects an empty value before native storage', async () => {
    const native = new FakeNativeKeychain()
    const ctx = await boot(native)
    await assert.rejects(ctx.credentials.set(credentialRef('MAESTRO_EMPTY_KEY'), ''), /empty value/)
    assert.deepEqual(native.listAccounts('test.maestro.keychain'), [])
  })
})

describe('durable credential records', () => {
  test('keeps values out of record descriptions and enumeration', async () => {
    const native = new FakeNativeKeychain()
    const ctx = await boot(native)
    const key = credentialKey('llm-pi-ai', 'openai-codex')
    const record = {
      kind: 'grant',
      payload: { access: 'top-secret-access', refresh: 'top-secret-refresh' },
    }
    const updates = []
    ctx.on('credentials/record-updated', updated => updates.push(updated))

    assert.deepEqual(await ctx.credentials.modifyRecord(key, async () => record), record)
    assert.deepEqual(await ctx.credentials.readRecord(key), record)
    const description = await ctx.credentials.describeRecord(key)
    const list = await ctx.credentials.listRecords()
    assert.deepEqual(description, { configured: true, kind: 'grant', writable: true })
    assert.deepEqual(list, [{ key, kind: 'grant' }])
    assert.equal(JSON.stringify({ description, list }).includes('top-secret'), false)

    await ctx.credentials.deleteRecord(key)
    assert.deepEqual(updates, [key, key])
  })

  test('serializes same-process read-modify-write operations', async () => {
    const native = new FakeNativeKeychain()
    const ctx = await boot(native)
    const key = credentialKey('llm-pi-ai', 'serialized')
    await ctx.credentials.modifyRecord(key, async () => ({ kind: 'grant', payload: { generation: 0 } }))
    let release
    const gate = new Promise(resolve => { release = resolve })
    const seen = []

    const first = ctx.credentials.modifyRecord(key, async current => {
      seen.push(current.payload.generation)
      await gate
      return { kind: 'grant', payload: { generation: 1 } }
    })
    const second = ctx.credentials.modifyRecord(key, async current => {
      seen.push(current.payload.generation)
      return { kind: 'grant', payload: { generation: 2 } }
    })
    await new Promise(resolve => setImmediate(resolve))
    assert.deepEqual(seen, [0])
    release()
    await Promise.all([first, second])
    assert.deepEqual(seen, [0, 1])
  })

  test('sanitizes malformed durable JSON diagnostics', async () => {
    const native = new FakeNativeKeychain()
    const ctx = await boot(native)
    const key = credentialKey('llm-pi-ai', 'broken')
    native.set(
      'test.maestro.keychain',
      `dsh-v1:record:${key}`,
      Buffer.from('{"kind":"grant","payload":"DO_NOT_LEAK",'),
    )

    await assert.rejects(ctx.credentials.readRecord(key), error => {
      assert.match(error.message, /is not valid JSON/)
      assert.equal(error.message.includes('DO_NOT_LEAK'), false)
      return true
    })
  })
})
