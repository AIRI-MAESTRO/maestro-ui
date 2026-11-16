"""Request-local workspace binding for embedded Maestro runtimes.

The CLI historically resolves relative paths against :func:`Path.cwd`.  An
embedded host cannot safely emulate that contract with ``os.chdir`` because a
single MCP child may execute several requests concurrently.  This module
provides a ``ContextVar``-backed override: code that opts into
``current_workspace_root`` sees the selected host workspace, while ordinary
CLI/TUI callers keep the exact legacy current-directory default.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
import glob as _glob
import os
from pathlib import Path


_BOUND_WORKSPACE_ROOT: ContextVar[Path | None] = ContextVar(
    "maestro_bound_workspace_root",
    default=None,
)


def current_workspace_root() -> Path:
    """Return the request-bound workspace or the legacy process cwd."""

    bound = _BOUND_WORKSPACE_ROOT.get()
    return Path.cwd() if bound is None else bound


def workspace_path(value: str | os.PathLike[str]) -> Path:
    """Expand ``value`` and root relative paths in the current workspace."""

    candidate = Path(value).expanduser()
    if candidate.is_absolute():
        return candidate
    return current_workspace_root() / candidate


class WorkspaceBoundaryError(ValueError):
    """A resident command path escaped its host-selected workspace."""


def command_workspace_path(value: str | os.PathLike[str]) -> Path:
    """Resolve one command input below the bound workspace.

    An ordinary CLI/TUI call has no bound root and deliberately keeps the
    historical :func:`workspace_path` behaviour.  A resident ``CommandBridge``
    call binds an owner-selected root, so user-controlled path arguments are
    canonicalised and rejected when they contain parent traversal or resolve
    through a symlink outside that root.  ``strict=False`` also covers output
    paths that do not exist yet while still resolving every existing parent.
    """

    bound = _BOUND_WORKSPACE_ROOT.get()
    if bound is None:
        return workspace_path(value)

    candidate = Path(value).expanduser()
    if os.pardir in candidate.parts:
        raise WorkspaceBoundaryError(
            "command paths cannot contain parent-directory traversal"
        )
    rooted = candidate if candidate.is_absolute() else bound / candidate
    try:
        resolved = rooted.resolve(strict=False)
    except (OSError, RuntimeError) as exc:
        raise WorkspaceBoundaryError(
            "command path cannot be safely resolved inside the workspace"
        ) from exc
    try:
        resolved.relative_to(bound)
    except ValueError as exc:
        raise WorkspaceBoundaryError(
            "command path must stay inside the selected workspace"
        ) from exc
    return resolved


def command_workspace_glob_pattern(value: str | os.PathLike[str]) -> str:
    """Root and preflight one command glob without expanding it.

    The non-magic prefix is canonicalised so an explicit symlink or absolute
    prefix cannot leave a bound workspace.  Wildcard matches require a second
    check via :func:`command_workspace_glob_match`, because a wildcard may
    itself select a symlink.
    """

    pattern = Path(value).expanduser()
    bound = _BOUND_WORKSPACE_ROOT.get()
    if bound is None:
        if pattern.is_absolute():
            return str(pattern)
        return str(current_workspace_root() / pattern)

    if os.pardir in pattern.parts:
        raise WorkspaceBoundaryError(
            "command glob patterns cannot contain parent-directory traversal"
        )
    if pattern.is_absolute():
        try:
            relative_pattern = pattern.relative_to(bound)
        except ValueError as exc:
            raise WorkspaceBoundaryError(
                "command glob pattern must stay inside the selected workspace"
            ) from exc
    else:
        relative_pattern = pattern

    parts = relative_pattern.parts
    magic_at = next(
        (index for index, part in enumerate(parts) if _glob.has_magic(part)),
        len(parts),
    )
    prefix = Path(*parts[:magic_at]) if magic_at else Path()
    safe_prefix = command_workspace_path(bound / prefix)
    escaped_prefix = _glob.escape(str(safe_prefix))
    if magic_at == len(parts):
        return escaped_prefix
    return os.path.join(escaped_prefix, *parts[magic_at:])


def command_workspace_glob_match(value: str | os.PathLike[str]) -> Path:
    """Canonicalise one expanded command-glob result when resident-bound."""

    if _BOUND_WORKSPACE_ROOT.get() is None:
        # Preserve the public bulk-import helper's legacy relative Path values.
        return Path(value)
    return command_workspace_path(value)


@contextmanager
def bind_workspace_root(root: Path | None) -> Iterator[None]:
    """Bind one already-validated workspace for the current execution context."""

    if root is None:
        yield
        return
    token = _BOUND_WORKSPACE_ROOT.set(root)
    try:
        yield
    finally:
        _BOUND_WORKSPACE_ROOT.reset(token)


__all__ = [
    "WorkspaceBoundaryError",
    "bind_workspace_root",
    "command_workspace_glob_match",
    "command_workspace_glob_pattern",
    "command_workspace_path",
    "current_workspace_root",
    "workspace_path",
]
