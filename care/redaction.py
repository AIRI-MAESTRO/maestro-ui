"""Shared redaction helpers for user-visible desktop diagnostics.

The native workbench deliberately shows CLI output and runtime failures, but
provider libraries sometimes include credentials in exception strings.  Keep
the redaction rules in one small module so command responses, run snapshots,
and SSE events cannot drift apart.
"""

from __future__ import annotations

import re
from collections.abc import Iterable


_SECRET_ASSIGNMENT_RE = re.compile(
    r"(?i)\b(api[-_]?key|api[-_]?token|access[-_]?token|refresh[-_]?token|"
    r"client[-_]?secret|password|passwd|secret|token)"
    r"(\s*[:=]\s*)(?:[\"']?)[^\s,;\"']+"
)
_SECRET_OPTION_RE = re.compile(
    r"(?i)(--(?:api[-_]?key|api[-_]?token|access[-_]?token|"
    r"refresh[-_]?token|client[-_]?secret|password|passwd|secret|token)\s+)"
    r"\S+"
)
_AUTHORIZATION_RE = re.compile(
    r"(?i)\b(authorization\s*[:=]\s*)(?:(?:bearer|basic)\s+)?\S+"
)
_BEARER_RE = re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=-]{8,}")
_OPENAI_KEY_RE = re.compile(r"\bsk-[A-Za-z0-9_-]{8,}\b")
_SECRET_QUERY_RE = re.compile(
    r"(?i)([?&](?:api[-_]?key|api[-_]?token|access[-_]?token|"
    r"refresh[-_]?token|client[-_]?secret|password|passwd|secret|token)=)"
    r"[^&#\s]+"
)


def redact_sensitive_text(
    value: object,
    supplied_secrets: Iterable[str] = (),
    *,
    max_chars: int | None = None,
) -> str:
    """Return bounded diagnostic text with common credential forms removed.

    ``supplied_secrets`` covers arbitrary values passed through known secret
    flags.  Pattern rules cover credentials originating inside a provider or
    runtime exception, including Authorization headers and secret URL query
    parameters.  The helper is intentionally for diagnostics; it is not a
    substitute for keeping configuration objects out of API payloads.
    """

    text = str(value)
    for secret in sorted(set(supplied_secrets), key=len, reverse=True):
        if secret:
            text = text.replace(secret, "[redacted]")
    text = _SECRET_OPTION_RE.sub(r"\1[redacted]", text)
    text = _SECRET_ASSIGNMENT_RE.sub(r"\1\2[redacted]", text)
    text = _AUTHORIZATION_RE.sub(r"\1[redacted]", text)
    text = _BEARER_RE.sub(r"\1 [redacted]", text)
    text = _OPENAI_KEY_RE.sub("[redacted]", text)
    text = _SECRET_QUERY_RE.sub(r"\1[redacted]", text)
    if max_chars is not None:
        if max_chars < 0:
            raise ValueError("max_chars must be non-negative")
        text = text[:max_chars]
    return text


__all__ = ["redact_sensitive_text"]
