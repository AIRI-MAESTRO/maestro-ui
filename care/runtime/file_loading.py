"""Canonical "read a file as chain-ready content" used by every attach surface.

Before this, four surfaces read files independently and diverged: the chat
``@``-refs, the chat document-skill bridge, the RunContextModal context files,
and ``care run --file``. Some capped the size, some didn't; some crashed on a
binary (``UnicodeDecodeError``); only one handled images. This module is the
single implementation they all call:

* office documents (docx/pptx/xlsx/odf/rtf) and PDFs are extracted to text
  via the same extractors the chat ``@``-refs use;
* images become a base64 ``data:`` URI (so vision models can consume them);
* everything else is read as UTF-8 with undecodable bytes replaced — a binary
  never crashes the handoff;
* text is capped at :data:`MAX_CONTEXT_FILE_CHARS` with a truncation flag.

:class:`LoadedFile.as_block` renders the ready-to-inject prompt block, matching
the chat ``@``-ref envelopes (``<file …>`` / ``<image …>``).
"""

from __future__ import annotations

import base64
import csv
import io
import json
import mimetypes
import re
from dataclasses import dataclass
from pathlib import Path

#: Cap on text pulled from a single file (chars). Bigger files are truncated
#: with a notice rather than ballooning the prompt / the run.
MAX_CONTEXT_FILE_CHARS = 200_000

#: Cap on an image attached inline as base64 (bytes). Larger images are
#: rejected with an error rather than bloating the request.
MAX_IMAGE_BYTES = 4_000_000

_OFFICE_EXTS = frozenset(
    {".docx", ".pptx", ".xlsx", ".odt", ".odp", ".ods", ".rtf"}
)
_IMAGE_EXTS = frozenset(
    {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}
)


def is_image_path(path: str | Path) -> bool:
    return Path(str(path)).suffix.lower() in _IMAGE_EXTS


@dataclass(frozen=True)
class LoadedFile:
    """Result of :func:`load_file` — chain-ready content for one file."""

    path: str
    content: str
    """Extracted text for documents; ``""`` for images / errors."""
    image_data_uri: str | None
    truncated: bool
    error: str | None
    size_bytes: int

    @property
    def is_image(self) -> bool:
        return self.image_data_uri is not None

    @property
    def ok(self) -> bool:
        return self.error is None

    @property
    def memory_value(self) -> str:
        """The string to store in ``context.memory["input"][…]`` — the image
        data URI for images, the extracted text otherwise."""
        return self.image_data_uri if self.is_image else self.content

    def as_block(self, label: str | None = None) -> str:
        """Render the file as a prompt block (``<file>`` / ``<image>``),
        matching the chat ``@``-ref envelopes so every surface looks the same.
        """
        name = label or Path(self.path).name or self.path
        if self.is_image:
            mime = mimetypes.guess_type(name)[0] or "image/png"
            return (
                f'<image path="{name}" mime="{mime}" '
                f'size_bytes="{self.size_bytes}">\n'
                f"{self.image_data_uri}\n</image>"
            )
        note = (
            f"\n[… truncated to {len(self.content)} chars]"
            if self.truncated
            else ""
        )
        return f'<file path="{name}">\n{self.content}{note}\n</file>'


@dataclass(frozen=True)
class FileProfile:
    """Content-free structural summary safe to expose to a planner.

    Profiles intentionally omit cell/field values. They describe only shape,
    missingness and document sections so planning can choose operations without
    copying user data into chain metadata or research queries.
    """

    format: str
    columns: tuple[str, ...] = ()
    row_count: int | None = None
    missing_by_column: tuple[tuple[str, int], ...] = ()
    section_outline: tuple[str, ...] = ()
    line_count: int | None = None
    truncated: bool = False
    error: str | None = None

    def as_dict(self) -> dict[str, object]:
        result: dict[str, object] = {
            "format": self.format,
            "truncated": self.truncated,
        }
        if self.columns:
            result["columns"] = list(self.columns)
        if self.row_count is not None:
            result["row_count"] = self.row_count
        if self.missing_by_column:
            result["missing_by_column"] = dict(self.missing_by_column)
        if self.section_outline:
            result["section_outline"] = list(self.section_outline)
        if self.line_count is not None:
            result["line_count"] = self.line_count
        if self.error is not None:
            result["error"] = self.error
        return result


def profile_loaded_file(loaded: LoadedFile, *, name: str | None = None) -> FileProfile:
    """Build a bounded structural profile from canonical loaded content."""
    filename = name or Path(loaded.path).name
    suffix = Path(filename).suffix.lower()
    format_name = suffix.removeprefix(".") or ("image" if loaded.is_image else "text")
    if not loaded.ok:
        return FileProfile(format=format_name, truncated=loaded.truncated, error=loaded.error)
    if loaded.is_image:
        return FileProfile(format="image", truncated=False)

    if suffix in {".csv", ".tsv"}:
        delimiter = "\t" if suffix == ".tsv" else ","
        return _profile_delimited(loaded.content, delimiter=delimiter, format_name=format_name, truncated=loaded.truncated)
    if suffix == ".json":
        return _profile_json(loaded.content, truncated=loaded.truncated)

    headings = tuple(
        match.group(1).strip()
        for line in loaded.content.splitlines()
        if (match := re.match(r"^#{1,6}\s+(.+?)\s*$", line))
    )
    return FileProfile(
        format=format_name,
        section_outline=headings[:100],
        line_count=len(loaded.content.splitlines()),
        truncated=loaded.truncated,
    )


def _profile_delimited(
    content: str,
    *,
    delimiter: str,
    format_name: str,
    truncated: bool,
) -> FileProfile:
    try:
        rows = list(csv.reader(io.StringIO(content), delimiter=delimiter))
    except (csv.Error, UnicodeError) as exc:
        return FileProfile(format=format_name, truncated=truncated, error=type(exc).__name__)
    if not rows:
        return FileProfile(format=format_name, columns=(), row_count=0, truncated=truncated)
    columns = tuple((cell.strip() or f"column_{index + 1}") for index, cell in enumerate(rows[0]))
    data_rows = rows[1:]
    missing = []
    for index, column in enumerate(columns):
        missing.append(
            (
                column,
                sum(index >= len(row) or not row[index].strip() for row in data_rows),
            )
        )
    return FileProfile(
        format=format_name,
        columns=columns,
        row_count=len(data_rows),
        missing_by_column=tuple(missing),
        truncated=truncated,
    )


def _profile_json(content: str, *, truncated: bool) -> FileProfile:
    try:
        value = json.loads(content)
    except (json.JSONDecodeError, UnicodeError) as exc:
        return FileProfile(format="json", truncated=truncated, error=type(exc).__name__)
    if isinstance(value, list) and all(isinstance(item, dict) for item in value):
        columns = tuple(sorted({str(key) for item in value for key in item}))
        missing = tuple(
            (column, sum(column not in item or item.get(column) in (None, "") for item in value))
            for column in columns
        )
        return FileProfile(
            format="json",
            columns=columns,
            row_count=len(value),
            missing_by_column=missing,
            truncated=truncated,
        )
    if isinstance(value, dict):
        return FileProfile(
            format="json",
            columns=tuple(sorted(str(key) for key in value)),
            row_count=1,
            truncated=truncated,
        )
    return FileProfile(format="json", row_count=1, truncated=truncated)


def load_file(
    path: str | Path,
    *,
    max_chars: int = MAX_CONTEXT_FILE_CHARS,
    max_image_bytes: int = MAX_IMAGE_BYTES,
) -> LoadedFile:
    """Read ``path`` into chain-ready content. Never raises — a problem is
    reported via :attr:`LoadedFile.error` with empty content."""
    p = Path(str(path)).expanduser()
    try:
        size = p.stat().st_size
    except OSError:
        return LoadedFile(str(path), "", None, False, "file not found", 0)

    if is_image_path(p):
        if size > max_image_bytes:
            return LoadedFile(
                str(path), "", None, False,
                f"image too large ({size} bytes)", size,
            )
        try:
            data = p.read_bytes()
        except OSError as exc:
            return LoadedFile(
                str(path), "", None, False, type(exc).__name__, size,
            )
        mime = mimetypes.guess_type(str(p))[0] or "image/png"
        uri = f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"
        return LoadedFile(str(path), "", uri, False, None, size)

    ext = p.suffix.lower()
    try:
        if ext == ".pdf":
            from pypdf import PdfReader

            text = "\n".join(
                (page.extract_text() or "") for page in PdfReader(str(p)).pages
            )
        elif ext in _OFFICE_EXTS:
            from care.runtime.document_extract import extract_document_text

            text = extract_document_text(p)
        else:
            text = p.read_text(encoding="utf-8", errors="replace")
    except Exception as exc:  # noqa: BLE001 — never crash the attach/handoff
        return LoadedFile(str(path), "", None, False, type(exc).__name__, size)

    truncated = len(text) > max_chars
    if truncated:
        text = text[:max_chars]
    return LoadedFile(str(path), text, None, truncated, None, size)


def load_file_text(
    path: str | Path, *, max_chars: int = MAX_CONTEXT_FILE_CHARS,
) -> tuple[str, bool]:
    """Convenience: ``(content, truncated)``. Images yield their data URI as
    the content. Kept small so existing ``(text, truncated)`` call sites can
    delegate here."""
    lf = load_file(path, max_chars=max_chars)
    return lf.memory_value, lf.truncated


__all__ = [
    "MAX_CONTEXT_FILE_CHARS",
    "MAX_IMAGE_BYTES",
    "FileProfile",
    "LoadedFile",
    "is_image_path",
    "load_file",
    "load_file_text",
    "profile_loaded_file",
]
