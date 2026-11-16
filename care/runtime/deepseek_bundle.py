"""Safe materialization of MAESTRO's direct DeepSeek Harness patch.

Cordis deliberately evaluates ``!!js`` only inside plugin ``config`` and the
``disabled`` field. An entry's module ``name`` is static metadata, so a native
host applying a patch directly must replace explicit template tokens with
concrete absolute module paths before DSH parses the YAML.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path, PurePosixPath
from typing import Final


DIRECT_POLICY_MODULE_TOKEN: Final = "__MAESTRO_POLICY_MODULE_JSON__"
DIRECT_PRESET_ROOT_MODULE_TOKEN: Final = "__MAESTRO_PRESET_ROOT_MODULE_JSON__"
DIRECT_SETTINGS_MODULE_TOKEN: Final = "__MAESTRO_SETTINGS_MODULE_JSON__"
DIRECT_BRANDING_MODULE_TOKEN: Final = "__MAESTRO_BRANDING_MODULE_JSON__"
DIRECT_JOBS_MODULE_TOKEN: Final = "__MAESTRO_JOBS_MODULE_JSON__"
_UNRESOLVED_MAESTRO_TOKEN = re.compile(r"__MAESTRO_[A-Z0-9_]+__")


class DeepSeekBundleMaterializationError(ValueError):
    """A direct-patch template or policy path violated its strict contract."""


def materialize_direct_patch(
    template_text: str,
    policy_module: str | os.PathLike[str],
    preset_root_module: str | os.PathLike[str] | None = None,
    jobs_module: str | os.PathLike[str] | None = None,
    settings_module: str | os.PathLike[str] | None = None,
    branding_module: str | os.PathLike[str] | None = None,
) -> str:
    """Render one direct DSH patch with JSON-quoted absolute module paths.

    The supported preset-root, settings, branding, policy, and jobs tokens must each
    occur exactly once, in that order. Unknown MAESTRO tokens, an
    already-rendered template, duplicate occurrences, relative paths, and
    NUL-bearing paths fail before a temporary patch can be written.
    """

    if not isinstance(template_text, str) or not template_text:
        raise DeepSeekBundleMaterializationError(
            "direct patch template must be a non-empty string"
        )
    tokens = _UNRESOLVED_MAESTRO_TOKEN.findall(template_text)
    expected_tokens = [
        DIRECT_PRESET_ROOT_MODULE_TOKEN,
        DIRECT_SETTINGS_MODULE_TOKEN,
        DIRECT_BRANDING_MODULE_TOKEN,
        DIRECT_POLICY_MODULE_TOKEN,
        DIRECT_JOBS_MODULE_TOKEN,
    ]
    if (
        tokens != expected_tokens
        or preset_root_module is None
        or jobs_module is None
        or settings_module is None
        or branding_module is None
    ):
        raise DeepSeekBundleMaterializationError(
            "direct patch template must contain exactly one supported preset, "
            "settings, branding, policy, and jobs token in that order"
        )

    def quoted_absolute_module(
        module: str | os.PathLike[str],
        *,
        label: str,
    ) -> str:
        raw_path = os.fspath(module)
        if not isinstance(raw_path, str) or not raw_path or "\x00" in raw_path:
            raise DeepSeekBundleMaterializationError(
                f"{label} module path must be a non-empty NUL-free string"
            )
        expanded = os.path.expanduser(raw_path)
        if not (Path(expanded).is_absolute() if os.name == "nt" else PurePosixPath(expanded).is_absolute()):
            raise DeepSeekBundleMaterializationError(
                f"{label} module path must be an absolute POSIX path"
            )
        path = Path(expanded).resolve(strict=False)
        # ESM interprets drive letters as URL schemes; use a file URL on Windows.
        resolved = path.as_uri() if os.name == "nt" else path.as_posix()
        return json.dumps(resolved, ensure_ascii=False)

    quoted_policy = quoted_absolute_module(policy_module, label="policy")
    quoted_preset = quoted_absolute_module(preset_root_module, label="preset root")
    quoted_jobs = quoted_absolute_module(jobs_module, label="jobs")
    quoted_settings = quoted_absolute_module(settings_module, label="settings")
    quoted_branding = quoted_absolute_module(branding_module, label="branding")
    rendered = template_text.replace(
        DIRECT_PRESET_ROOT_MODULE_TOKEN,
        quoted_preset,
    ).replace(
        DIRECT_SETTINGS_MODULE_TOKEN,
        quoted_settings,
    ).replace(
        DIRECT_BRANDING_MODULE_TOKEN,
        quoted_branding,
    ).replace(
        DIRECT_POLICY_MODULE_TOKEN,
        quoted_policy,
    ).replace(
        DIRECT_JOBS_MODULE_TOKEN,
        quoted_jobs,
    )
    if _UNRESOLVED_MAESTRO_TOKEN.search(rendered):
        raise DeepSeekBundleMaterializationError(
            "direct patch contains an unresolved MAESTRO token"
        )
    return rendered


__all__ = [
    "DIRECT_BRANDING_MODULE_TOKEN",
    "DIRECT_POLICY_MODULE_TOKEN",
    "DIRECT_PRESET_ROOT_MODULE_TOKEN",
    "DIRECT_SETTINGS_MODULE_TOKEN",
    "DIRECT_JOBS_MODULE_TOKEN",
    "DeepSeekBundleMaterializationError",
    "materialize_direct_patch",
]
