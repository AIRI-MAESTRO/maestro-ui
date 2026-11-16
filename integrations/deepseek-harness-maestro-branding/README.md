# MAESTRO client branding

This package is the product-owned presentation layer for the native MAESTRO
agent client. It uses the public `sidebar.brand.mark`, `sidebar.brand.name`,
and `conversation.hero.brand.mark` slots, plus the public theme token registry.
It does not patch upstream source files or change provider identities: the
DeepSeek provider remains named DeepSeek wherever a model/provider choice is
being shown.

The small document observer handles product copy for upstream surfaces that do
not currently expose a copy/locale slot. It changes only exact stock hero and
welcome text or the complete product name `DeepSeek Harness`; it skips code,
package identifiers, form controls, and provider options. Re-mounted React
trees are decorated again, and the observer is disposed with the plugin.

## Native-profile integration

Mount `index.mjs` as its own top-level plugin in the native direct patch:

```yaml
- insert:
    - id: maestro-client-branding
      name: __MAESTRO_BRANDING_MODULE_JSON__
```

The native materializer should replace that token with the JSON-quoted absolute
path to this package's `index.mjs`, and include the whole package directory in
the application resources. Mount it after the Web bundle; the client manifest
orders the renderer, sidebar, conversation, and theme dependencies.

Once this plugin is mounted, the launcher should not also inject the legacy
native branding script: that script hides brand-slot children and would mask
the real slot occupants supplied here.
