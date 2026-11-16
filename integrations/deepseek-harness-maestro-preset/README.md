# MAESTRO Agent preset for DeepSeek Harness Web

DeepSeek Harness Web composes model-facing tools and identity from an Agent
preset. Its shipped `standard` preset therefore shadows a host-level
`system-prompt.config.persona`. This bundle installs a system preset named
`maestro`, selects it for newly created sessions, and leaves the shipped
`standard` preset available.

The deployment default is a base setting: an existing user-level
`agent-presets.default` still wins. Clear that override, select `maestro` in
the UI, or pass `agentPreset: "maestro"` to `session/create`. Existing non-blank
sessions keep the preset and tool schemas they started with.

The `maestro` composition is a pinned semantic copy of DSH `standard` at commit
`dd6322d604e00eec1ba5e0c8541159906a21094a`; only
`persona.config.text` differs. Consequently it retains the standard shell,
filesystem, Web, Skills, jobs, goals, planning, subagent, and workflow rows.
Global tools contributed by other DSH bundles and MCP servers remain visible
through the normal scoped tool registry.

DSH Agent presets intentionally have no inheritance or patch operator. On a DSH
upgrade, compare the new `standard/agent.cordis.yml`, update this copy, and then
update `upstream-standard.json`. The CARE test fails if the pinned checkout and
the copy differ anywhere except the persona.

Install this layer after the Web and MAESTRO capability bundles:

```bash
dsh plugin --profile maestro-care add \
  @deepseek-ai/dsh-web-app \
  file:/ABSOLUTE/PATH/TO/care/integrations/deepseek-harness \
  file:/ABSOLUTE/PATH/TO/care/integrations/deepseek-harness-maestro-preset
```

For a direct native launch, pass this `cordis.patch.yml` after the materialized
MAESTRO capability patch. Relative plugin anchoring is owned by DSH's overlay
loader; do not copy only the YAML away from `preset-root.mjs` and
`agent-presets/`.
