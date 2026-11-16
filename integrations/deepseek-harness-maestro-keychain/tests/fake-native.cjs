// Process-local native-addon stand-in for DSH composition smokes. It exercises
// the provider's full read/write API without touching macOS Keychain.
const items = new Map()

function address(service, account) {
  return `${service}\u0000${account}`
}

module.exports = {
  get(service, account) {
    const value = items.get(address(service, account))
    return value === undefined ? null : Buffer.from(value)
  },
  set(service, account, value) {
    if (!Buffer.isBuffer(value)) throw new TypeError('fake keychain value must be a Buffer')
    items.set(address(service, account), Buffer.from(value))
  },
  delete(service, account) {
    return items.delete(address(service, account))
  },
  listAccounts(service) {
    const prefix = `${service}\u0000`
    return [...items.keys()]
      .filter(key => key.startsWith(prefix))
      .map(key => key.slice(prefix.length))
  },
}
