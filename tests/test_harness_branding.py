from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Callable

import pytest

from care.harness_branding import (
    BRAND_SCRIPT_MARKER,
    BRAND_STYLE_ID,
    HarnessBrandingError,
    apply_maestro_branding,
    bind_maestro_branding,
    build_maestro_brand_script,
)


def _asset(tmp_path: Path) -> Path:
    path = tmp_path / "maestric.webp"
    path.write_bytes(b"RIFF-private-test-image")
    return path


def _mark(tmp_path: Path) -> Path:
    path = tmp_path / "maestro-mark.svg"
    path.write_text("<svg><path d='M0 0'/></svg>", encoding="utf-8")
    return path


def test_brand_script_is_idempotent_value_free_and_uses_public_slots(
    tmp_path: Path,
) -> None:
    asset = _asset(tmp_path)
    mark = _mark(tmp_path)
    script = build_maestro_brand_script(maestric_path=asset, mark_path=mark)

    assert BRAND_SCRIPT_MARKER in script
    assert BRAND_STYLE_ID in script
    assert 'data-slot=\\"sidebar.brand.mark\\"' in script
    assert 'data-slot=\\"sidebar.brand.name\\"' in script
    assert 'data-slot=\\"conversation.hero.brand.mark\\"' in script
    assert "data-maestro-hero" in script
    assert "MutationObserver" in script
    assert "MAESTRO" in script
    assert "AGENTIC HARNESS" in script
    assert "data:image/webp;base64," in script
    assert "data:image/svg+xml;base64," in script
    assert "--dsw-static-deepseek-500" in script
    assert "--maestro-bg" in script
    assert "#faf6ef" in script
    assert "#0f1115" in script
    assert "display: inline-flex !important" in script
    assert str(asset) not in script
    assert str(mark) not in script
    assert "token" not in script.casefold()


def test_brand_script_rejects_missing_or_empty_asset(tmp_path: Path) -> None:
    with pytest.raises(HarnessBrandingError, match="read"):
        build_maestro_brand_script(
            maestric_path=tmp_path / "missing.webp",
            mark_path=_mark(tmp_path),
        )

    empty = tmp_path / "empty.webp"
    empty.touch()
    with pytest.raises(HarnessBrandingError, match="empty"):
        build_maestro_brand_script(maestric_path=empty, mark_path=_mark(tmp_path))


def test_brand_script_rejects_unsupported_asset_format(tmp_path: Path) -> None:
    unsupported = tmp_path / "mascot.bin"
    unsupported.write_bytes(b"not-an-image")
    with pytest.raises(HarnessBrandingError, match="unsupported"):
        build_maestro_brand_script(
            maestric_path=unsupported,
            mark_path=_mark(tmp_path),
        )


def test_apply_uses_only_evaluate_js(tmp_path: Path) -> None:
    class Window:
        def __init__(self) -> None:
            self.scripts: list[str] = []

        def evaluate_js(self, script: str) -> None:
            self.scripts.append(script)

    window = Window()
    apply_maestro_branding(
        window,
        maestric_path=_asset(tmp_path),
        mark_path=_mark(tmp_path),
    )

    assert len(window.scripts) == 1
    assert BRAND_SCRIPT_MARKER in window.scripts[0]


def test_apply_fails_when_window_has_no_js_surface(tmp_path: Path) -> None:
    with pytest.raises(HarnessBrandingError, match="evaluate_js"):
        apply_maestro_branding(
            object(),
            maestric_path=_asset(tmp_path),
            mark_path=_mark(tmp_path),
        )


class LoadedEvent:
    def __init__(self, *, already_loaded: bool = False) -> None:
        self.handlers: list[Callable[..., None]] = []
        self.already_loaded = already_loaded

    def __iadd__(self, handler: Callable[..., None]) -> LoadedEvent:
        self.handlers.append(handler)
        return self

    def __isub__(self, handler: Callable[..., None]) -> LoadedEvent:
        self.handlers.remove(handler)
        return self

    def is_set(self) -> bool:
        return self.already_loaded

    def fire(self) -> None:
        for handler in tuple(self.handlers):
            handler()


class EventWindow:
    def __init__(self, *, already_loaded: bool = False) -> None:
        self.events = SimpleNamespace(loaded=LoadedEvent(already_loaded=already_loaded))
        self.scripts: list[str] = []

    def evaluate_js(self, script: str) -> None:
        self.scripts.append(script)


def test_bind_applies_on_every_document_load_and_replaces_old_binding(
    tmp_path: Path,
) -> None:
    window = EventWindow()
    kwargs = {"maestric_path": _asset(tmp_path), "mark_path": _mark(tmp_path)}

    first = bind_maestro_branding(window, **kwargs)
    assert window.scripts == []
    window.events.loaded.fire()
    assert len(window.scripts) == 1

    second = bind_maestro_branding(window, **kwargs)
    assert second is not first
    assert window.events.loaded.handlers == [second]
    window.events.loaded.fire()
    assert len(window.scripts) == 2
    assert all(BRAND_SCRIPT_MARKER in script for script in window.scripts)


def test_bind_applies_immediately_when_document_is_already_loaded(
    tmp_path: Path,
) -> None:
    window = EventWindow(already_loaded=True)
    bind_maestro_branding(
        window,
        maestric_path=_asset(tmp_path),
        mark_path=_mark(tmp_path),
    )
    assert len(window.scripts) == 1


def test_bind_rejects_window_without_loaded_event(tmp_path: Path) -> None:
    with pytest.raises(HarnessBrandingError, match="loaded event"):
        bind_maestro_branding(
            SimpleNamespace(evaluate_js=lambda _script: None),
            maestric_path=_asset(tmp_path),
            mark_path=_mark(tmp_path),
        )
