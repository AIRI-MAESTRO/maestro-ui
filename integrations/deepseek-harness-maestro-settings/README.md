# MAESTRO native settings

This package contributes a `MAESTRO` page through DeepSeek Harness's public
`settings.section` slot. It intentionally does not use the Harness settings or
credential documents: chain generation and CARE commands consume
`CareConfig`, while credentials are stored by the native Python bridge in the
operating-system Keychain.

The browser plugin can read a redacted snapshot and request a validation
preview. Replacement credentials are entered in an AppKit secure-text dialog;
JavaScript receives only a short-lived, field-bound opaque handle. A save is
committed only after the host-provided native approval callback accepts the
exact previewed payload. Without either native boundary, mutation fails closed.

Every successful save returns `restart_required: true`. The running agent keeps
its startup configuration, so the UI tells the user to fully quit and reopen
MAESTRO before saved provider, model, credential, or runtime changes take effect.
