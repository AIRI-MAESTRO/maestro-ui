"""Executable contract against the pinned DeepSeek Harness schema validator."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess

import pytest

from care.harness_mcp import CareMcpAdapter


ROOT = Path(__file__).resolve().parents[1]
DSH_SOURCE = ROOT.parent / "deepseek-harness"
TSX = DSH_SOURCE / "node_modules" / ".bin" / (
    "tsx.cmd" if os.name == "nt" else "tsx"
)
VALIDATOR = DSH_SOURCE / "packages/core/tools/src/json-schema.ts"


@pytest.mark.skipif(
    not TSX.is_file() or not VALIDATOR.is_file(),
    reason="the pinned DeepSeek Harness source checkout and tsx are required",
)
def test_all_mcp_schemas_pass_the_pinned_dsh_validator() -> None:
    """Reject schema drift with DSH's actual exact-one implementation.

    Keyword allow-list tests cannot detect semantic overlaps such as
    ``oneOf: [{}, {type: 'null'}]``.  DeepSeek correctly rejects ``null`` for
    that schema because both branches match, so exercise its validator rather
    than maintaining a second approximation in CARE.
    """

    specs = CareMcpAdapter().list_tools()
    schemas = {
        spec.name: {
            "input": spec.input_schema,
            "output": spec.output_schema,
        }
        for spec in specs
    }
    script = """
      import {
        assertObjectJsonSchema,
        assertSupportedJsonSchema,
        validateJsonSchemaValue,
      } from './packages/core/tools/src/json-schema.ts'
      import { readFileSync } from 'node:fs'

      const schemas = JSON.parse(readFileSync(0, 'utf8'))
      for (const [name, pair] of Object.entries(schemas)) {
        for (const side of ['input', 'output']) {
          try {
            assertSupportedJsonSchema(pair[side])
            assertObjectJsonSchema(pair[side])
          } catch (error) {
            throw new Error(`${name}.${side}: ${String(error)}`)
          }
        }
      }
      for (const name of ['maestro_command_review', 'maestro_command_confirm']) {
        const transcript = schemas[name].input.properties.transcript
        const violations = validateJsonSchemaValue(transcript, null, 'transcript')
        if (violations.length !== 0) {
          throw new Error(`${name}.transcript rejects explicit null: ${violations.join('; ')}`)
        }
      }
    """

    completed = subprocess.run(
        [str(TSX), "--eval", script],
        cwd=DSH_SOURCE,
        input=json.dumps(schemas, ensure_ascii=False),
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr or completed.stdout
