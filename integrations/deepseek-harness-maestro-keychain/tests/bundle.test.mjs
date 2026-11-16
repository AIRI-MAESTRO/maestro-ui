import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import { describe, test } from 'node:test'

const root = new URL('../', import.meta.url)

describe('bundle composition', () => {
  test('replaces the one base credentials row and stays pinned to the audited DSH build', async () => {
    const patch = await readFile(new URL('cordis.patch.yml', root), 'utf8')
    const manifest = JSON.parse(await readFile(new URL('keychain-provider.json', root), 'utf8'))
    assert.match(
      patch,
      /- id: credentials\n  name: '@deepseek-ai\/dsh-credentials-local'\n  disabled: true/,
    )
    assert.match(
      patch,
      /- id: credentials-maestro-keychain\n      name: '@maestro-care\/dsh-credentials-keychain'/,
    )
    assert.match(patch, /nativeModulePath: !!js process\.env\.MAESTRO_KEYCHAIN_NATIVE_MODULE/)
    assert.equal((patch.match(/^- id: credentials$/gm) ?? []).length, 1)
    assert.equal(manifest.testedDeepSeekHarnessCommit, 'dd6322d604e00eec1ba5e0c8541159906a21094a')
    assert.equal(manifest.disabledProfileRow, 'credentials')
    assert.equal(manifest.providerProfileRow, 'credentials-maestro-keychain')
    assert.equal(manifest.resolutionMode, 'persistent-profile-dependency')
    assert.equal(manifest.nativeModuleEnvironment, 'MAESTRO_KEYCHAIN_NATIVE_MODULE')
  })

  test('native source calls Security.framework and has no command-process path', async () => {
    const source = await readFile(new URL('native/keychain.cc', root), 'utf8')
    const provider = await readFile(new URL('index.mjs', root), 'utf8')
    assert.match(source, /#include <Security\/Security\.h>/)
    assert.match(source, /SecItemCopyMatching/)
    assert.match(source, /SecItemUpdate/)
    assert.match(source, /SecItemAdd/)
    assert.match(source, /SecItemDelete/)
    for (const forbidden of ['security add-generic-password', 'execFile', 'spawn(', 'child_process']) {
      assert.equal(`${source}\n${provider}`.includes(forbidden), false)
    }
  })

  test('package exports only the UI-redacted DSH provider, not a secret RPC', async () => {
    const manifest = JSON.parse(await readFile(new URL('package.json', root), 'utf8'))
    assert.equal(manifest.main, './index.mjs')
    assert.deepEqual(manifest.dsh, { bundle: { patch: './cordis.patch.yml' } })
    assert.equal(Object.keys(manifest).includes('bin'), false)
  })
})
