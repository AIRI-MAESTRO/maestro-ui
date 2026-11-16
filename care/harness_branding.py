"""Native-only MAESTRO presentation layer for DeepSeek Harness Web.

DeepSeek Harness remains the owner of the DOM, accessibility, interaction,
settings, and agent state.  The native shell injects only design tokens,
bundled brand assets, and presentation metadata anchored to public
``data-slot`` seats.  No chat text, credential, session id, bootstrap URL, or
runtime value enters this script.
"""

from __future__ import annotations

import base64
import json
import os
from pathlib import Path
from typing import Any, Callable


BRAND_STYLE_ID = "maestro-care-native-brand"
BRAND_SCRIPT_MARKER = "maestro-care-brand-v2"
_BRAND_OBSERVER_KEY = "__maestroCareBrandObserverV2"
_BRAND_LOADED_HANDLER_ATTR = "_maestro_brand_loaded_handler"


class HarnessBrandingError(RuntimeError):
    """The bundled MAESTRO brand asset could not be prepared safely."""


def default_maestric_path() -> Path:
    """Resolve Maestric from py2app resources or the source package."""

    resource_path = os.environ.get("RESOURCEPATH", "").strip()
    candidates = []
    if resource_path:
        candidates.append(Path(resource_path) / "web_assets" / "mascot" / "mascot.webp")
    candidates.append(Path(__file__).resolve().parent / "web_assets" / "mascot" / "mascot.webp")
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    raise HarnessBrandingError("the bundled Maestric brand asset is unavailable")


def default_maestro_mark_path() -> Path:
    """Resolve the vector MAESTRO mark from resources or the source package."""

    resource_path = os.environ.get("RESOURCEPATH", "").strip()
    candidates = []
    if resource_path:
        candidates.append(Path(resource_path) / "web_assets" / "maestro-mark.svg")
    candidates.append(Path(__file__).resolve().parent / "web_assets" / "maestro-mark.svg")
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    raise HarnessBrandingError("the bundled MAESTRO mark is unavailable")


def _asset_data_url(path: Path) -> str:
    try:
        payload = path.read_bytes()
    except OSError as exc:
        raise HarnessBrandingError("could not read the bundled Maestric asset") from exc
    if not payload:
        raise HarnessBrandingError("the bundled Maestric asset is empty")
    media_types = {
        ".gif": "image/gif",
        ".png": "image/png",
        ".svg": "image/svg+xml",
        ".webp": "image/webp",
    }
    media_type = media_types.get(path.suffix.casefold())
    if media_type is None:
        raise HarnessBrandingError(
            f"unsupported MAESTRO brand asset format: {path.suffix or '<none>'}",
        )
    return f"data:{media_type};base64," + base64.b64encode(payload).decode("ascii")


def build_maestro_brand_script(
    *,
    maestric_path: str | os.PathLike[str] | None = None,
    mark_path: str | os.PathLike[str] | None = None,
) -> str:
    """Build an idempotent, value-free browser script for the native window.

    The script only depends on two public Harness slot addresses.  A small
    observer decorates their structural parents when React mounts or replaces
    them; CSS never depends on generated module-class names or translated
    DeepSeek copy.
    """

    asset_path = (
        default_maestric_path()
        if maestric_path is None
        else Path(maestric_path).expanduser().resolve()
    )
    mark_asset_path = (
        default_maestro_mark_path()
        if mark_path is None
        else Path(mark_path).expanduser().resolve()
    )
    maestric_data_url = _asset_data_url(asset_path)
    mark_data_url = _asset_data_url(mark_asset_path)
    css = f"""
body {{
  --maestro-bg: #faf6ef;
  --maestro-surface: #fffdf8;
  --maestro-surface-2: #fff9f0;
  --maestro-text: #2a2520;
  --maestro-muted: #6b5f52;
  --maestro-accent: #2a7d6f;
  --maestro-crystal: #4db8a4;
  --maestro-cream: #e8c89a;
  --maestro-border: rgba(42, 37, 32, 0.10);
  --maestro-glow: rgba(42, 125, 111, 0.24);

  --dsw-static-deepseek-50: #eff7f3;
  --dsw-static-deepseek-100: #dceee9;
  --dsw-static-deepseek-200: #bfe3da;
  --dsw-static-deepseek-300: #88d2c2;
  --dsw-static-deepseek-400: #4db8a4;
  --dsw-static-deepseek-450: #36a892;
  --dsw-static-deepseek-500: #2a7d6f;
  --dsw-static-deepseek-600: #1f6358;
  --dsw-static-deepseek-800: #23423e;
  --dsw-static-deepseek-900: #17312e;

  --dsw-alias-bg-base: var(--maestro-bg);
  --dsw-alias-bg-layer-1: var(--maestro-surface);
  --dsw-alias-bg-layer-2: var(--maestro-surface-2);
  --dsw-alias-bg-layer-3: #f4ebe0;
  --dsw-alias-bg-module-platform: #f4ebe0;
  --dsw-alias-bg-multi-select: #f4ebe0;
  --dsw-alias-bg-overlay: #eadcc9;
  --dsw-alias-border-l1: rgba(42, 37, 32, 0.06);
  --dsw-alias-border-l2-darkmode-thin: var(--maestro-border);
  --dsw-alias-border-l2: var(--maestro-border);
  --dsw-alias-border-l3: rgba(42, 37, 32, 0.14);
  --dsw-alias-border-l4: rgba(42, 37, 32, 0.20);
  --dsw-alias-brand-primary-new-colorprimary-new-color: #2a7d6f;
  --dsw-alias-brand-primary: var(--maestro-text);
  --dsw-alias-brand-text: var(--maestro-text);
  --dsw-alias-button-info-fill: var(--maestro-accent);
  --dsw-alias-button-info-hover: #1f6358;
  --dsw-alias-button-primary-fill: var(--maestro-accent);
  --dsw-alias-button-primary-hover: #1f6358;
  --dsw-alias-button-primary-dimmed: #dceee9;
  --dsw-alias-interactive-bg-active: rgba(42, 125, 111, 0.14);
  --dsw-alias-interactive-bg-hover-accent: rgba(42, 125, 111, 0.16);
  --dsw-alias-interactive-bg-hover-solid: #f4ebe0;
  --dsw-alias-interactive-bg-hover: rgba(42, 125, 111, 0.08);
  --dsw-alias-label-caption: #9a8d7e;
  --dsw-alias-label-dimmed: #d2c4b3;
  --dsw-alias-label-primary-bluish: #1f6358;
  --dsw-alias-label-primary-dimmed: #413a33;
  --dsw-alias-label-primary: var(--maestro-text);
  --dsw-alias-label-secondary: #5b5147;
  --dsw-alias-label-tertiary: var(--maestro-muted);
  --dsw-alias-state-business-primary: #2a7d6f;
  --dsw-alias-state-business-tertiary: #dceee9;
  --dsw-alias-markdown-code-block-banner: #f4ebe0;
  --dsw-alias-markdown-code-block: #fff9f0;
  --dsw-alias-markdown-inline-code: #f4ebe0;
  --dsw-alias-markdown-placeholder: #fff9f0;
  --dsw-alias-markdown-tag: #f4ebe0;
  --dsw-specific-bubble: #eff7f3;
  --dsw-specific-bubble-highlight: #dceee9;
  --dsw-specific-input-major: var(--maestro-surface);
  --dsw-specific-login-input: #fff9f0;
  --dsw-specific-menu: var(--maestro-surface);
  --dsw-specific-selector: #f4ebe0;
  --dsw-specific-sidebar-fill: #f4ebe0;
  --dsw-specific-sidebar-nav-item-active-accent: #dceee9;
  --dsw-specific-sidebar-nav-item-active: #eadcc9;
  --dsw-specific-sidebar-nav-item-hover: #eee3d5;
  --dsw-specific-tip: #fff9f0;
}}

body[data-ds-dark-theme] {{
  --maestro-bg: #0f1115;
  --maestro-surface: #1f242c;
  --maestro-surface-2: #252b35;
  --maestro-text: #e6e6e6;
  --maestro-muted: #a0a8b4;
  --maestro-accent: #33d9b2;
  --maestro-crystal: #7ef0dd;
  --maestro-cream: #c8e8e0;
  --maestro-border: rgba(126, 240, 221, 0.12);
  --maestro-glow: rgba(51, 217, 178, 0.30);

  --dsw-static-deepseek-50: #e7fff9;
  --dsw-static-deepseek-100: #c8f8ec;
  --dsw-static-deepseek-200: #98ead7;
  --dsw-static-deepseek-300: #69ddc2;
  --dsw-static-deepseek-400: #33d9b2;
  --dsw-static-deepseek-450: #33d9b2;
  --dsw-static-deepseek-500: #2ebfae;
  --dsw-static-deepseek-600: #259b8e;
  --dsw-static-deepseek-800: #173c38;
  --dsw-static-deepseek-900: #142c2a;

  --dsw-alias-bg-base: var(--maestro-bg);
  --dsw-alias-bg-layer-1: var(--maestro-surface);
  --dsw-alias-bg-layer-2: var(--maestro-surface-2);
  --dsw-alias-bg-layer-3: #2d3540;
  --dsw-alias-bg-module-platform: #252b35;
  --dsw-alias-bg-multi-select: #252b35;
  --dsw-alias-bg-overlay: #3d4653;
  --dsw-alias-border-l1: rgba(126, 240, 221, 0.07);
  --dsw-alias-border-l2-darkmode-thin: var(--maestro-border);
  --dsw-alias-border-l2: var(--maestro-border);
  --dsw-alias-border-l3: rgba(126, 240, 221, 0.17);
  --dsw-alias-border-l4: rgba(126, 240, 221, 0.24);
  --dsw-alias-brand-primary-new-colorprimary-new-color: #33d9b2;
  --dsw-alias-brand-primary: var(--maestro-text);
  --dsw-alias-brand-text: var(--maestro-text);
  --dsw-alias-button-info-fill: var(--maestro-accent);
  --dsw-alias-button-info-hover: #2ebfae;
  --dsw-alias-button-primary-fill: var(--maestro-accent);
  --dsw-alias-button-primary-hover: #2ebfae;
  --dsw-alias-button-primary-dimmed: #23423e;
  --dsw-alias-interactive-bg-active: rgba(51, 217, 178, 0.16);
  --dsw-alias-interactive-bg-hover-accent: rgba(51, 217, 178, 0.22);
  --dsw-alias-interactive-bg-hover-solid: #252b35;
  --dsw-alias-interactive-bg-hover: rgba(51, 217, 178, 0.10);
  --dsw-alias-label-caption: #707a88;
  --dsw-alias-label-dimmed: #48515e;
  --dsw-alias-label-primary-bluish: #c8f5ec;
  --dsw-alias-label-primary-dimmed: #c8d0d8;
  --dsw-alias-label-primary: var(--maestro-text);
  --dsw-alias-label-secondary: #c5cbd3;
  --dsw-alias-label-tertiary: var(--maestro-muted);
  --dsw-alias-state-business-primary: #33d9b2;
  --dsw-alias-state-business-tertiary: #173c38;
  --dsw-alias-markdown-code-block-banner: #1f242c;
  --dsw-alias-markdown-code-block: #181c22;
  --dsw-alias-markdown-inline-code: #252b35;
  --dsw-alias-markdown-placeholder: #181c22;
  --dsw-alias-markdown-tag: #252b35;
  --dsw-specific-bubble: #18312f;
  --dsw-specific-bubble-highlight: #244d47;
  --dsw-specific-input-major: var(--maestro-surface);
  --dsw-specific-login-input: #181c22;
  --dsw-specific-menu: #252b35;
  --dsw-specific-selector: #252b35;
  --dsw-specific-sidebar-fill: #181c22;
  --dsw-specific-sidebar-nav-item-active-accent: #173c38;
  --dsw-specific-sidebar-nav-item-active: #2d3540;
  --dsw-specific-sidebar-nav-item-hover: #1f242c;
  --dsw-specific-tip: #252b35;
}}

[data-slot="sidebar.brand.mark"] {{
  display: inline-flex !important;
  align-items: center;
  justify-content: center;
  position: relative;
  width: 26px;
  height: 26px;
  border-radius: 8px;
  background: linear-gradient(145deg, var(--maestro-crystal), var(--maestro-accent));
  box-shadow: 0 5px 18px var(--maestro-glow);
  overflow: hidden;
}}

[data-slot="sidebar.brand.mark"] > * {{ display: none !important; }}

[data-slot="sidebar.brand.mark"]::after {{
  content: "";
  width: 100%;
  height: 100%;
  background: url({json.dumps(mark_data_url)}) center / cover no-repeat;
}}

[data-slot="sidebar.brand.name"] {{
  display: inline-flex !important;
  align-items: center;
}}

[data-slot="sidebar.brand.name"] > * {{ display: none !important; }}

[data-slot="sidebar.brand.name"]::before {{
  content: "MAESTRO";
  color: var(--maestro-text);
  font-size: 16px;
  font-weight: 750;
  line-height: 24px;
  letter-spacing: 0.10em;
}}

[data-slot="conversation.hero.brand.mark"] {{
  display: inline-flex !important;
  align-items: center;
  justify-content: center;
  width: 78px;
  height: 82px;
  filter: drop-shadow(0 12px 22px var(--maestro-glow));
}}

[data-slot="conversation.hero.brand.mark"] > * {{ display: none !important; }}

[data-slot="conversation.hero.brand.mark"]::after {{
  content: "";
  width: 78px;
  height: 82px;
  background: url({json.dumps(maestric_data_url)}) center 42% / 76px auto no-repeat;
}}

[data-maestro-hero] {{
  position: relative;
  isolation: isolate;
  grid-template-columns: 78px minmax(0, auto) !important;
  grid-template-rows: auto auto !important;
  column-gap: 16px !important;
  row-gap: 0 !important;
}}

[data-maestro-hero]::before {{
  content: "";
  position: absolute;
  z-index: -1;
  width: min(620px, 78vw);
  height: 210px;
  left: 50%;
  top: 50%;
  transform: translate(-50%, -50%);
  border-radius: 50%;
  background:
    radial-gradient(ellipse at 22% 55%, color-mix(in srgb, var(--maestro-accent) 17%, transparent), transparent 52%),
    radial-gradient(ellipse at 76% 38%, color-mix(in srgb, var(--maestro-cream) 25%, transparent), transparent 58%);
  filter: blur(2px);
  pointer-events: none;
}}

[data-maestro-hero] > [data-maestro-hero-mark] {{
  grid-column: 1 !important;
  grid-row: 1 / span 2 !important;
  align-self: center !important;
}}

[data-maestro-hero] > [data-maestro-hero-title] {{
  grid-column: 2 !important;
  grid-row: 1 !important;
  align-self: end !important;
  color: transparent !important;
  font-size: 0 !important;
  white-space: nowrap;
}}

[data-maestro-hero] > [data-maestro-hero-title]::before {{
  content: "MAESTRO";
  color: var(--maestro-text);
  font-size: 30px;
  font-weight: 760;
  line-height: 35px;
  letter-spacing: 0.10em;
}}

[data-maestro-hero] > [data-maestro-hero-badge] {{
  grid-column: 2 !important;
  grid-row: 2 !important;
  justify-self: start !important;
  align-self: start !important;
  margin: 2px 0 0 !important;
  padding: 3px 9px 2px !important;
  border: 1px solid color-mix(in srgb, var(--maestro-crystal) 50%, transparent) !important;
  border-radius: 999px !important;
  background: color-mix(in srgb, var(--maestro-accent) 11%, var(--maestro-surface)) !important;
  color: transparent !important;
  font-size: 0 !important;
  box-shadow: 0 7px 22px color-mix(in srgb, var(--maestro-accent) 10%, transparent);
}}

[data-maestro-hero] > [data-maestro-hero-badge]::before {{
  content: "AGENTIC HARNESS";
  color: var(--maestro-accent);
  font-family: var(--ds-font-family-code);
  font-size: 10px;
  font-weight: 650;
  line-height: 16px;
  letter-spacing: 0.10em;
}}

body::before {{
  content: "";
  position: fixed;
  z-index: -1;
  pointer-events: none;
  inset: 0;
  background:
    radial-gradient(58rem 35rem at 82% -8%, color-mix(in srgb, var(--maestro-crystal) 10%, transparent), transparent 70%),
    radial-gradient(42rem 30rem at 4% 104%, color-mix(in srgb, var(--maestro-cream) 10%, transparent), transparent 72%);
}}

@media (max-width: 720px) {{
  [data-maestro-hero] {{
    grid-template-columns: 60px minmax(0, auto) !important;
    column-gap: 11px !important;
  }}

  [data-slot="conversation.hero.brand.mark"],
  [data-slot="conversation.hero.brand.mark"]::after {{
    width: 60px;
    height: 64px;
  }}

  [data-slot="conversation.hero.brand.mark"]::after {{
    background-size: 59px auto;
  }}

  [data-maestro-hero] > [data-maestro-hero-title]::before {{
    font-size: 23px;
    line-height: 29px;
  }}
}}

@media (prefers-reduced-motion: no-preference) {{
  [data-slot="conversation.hero.brand.mark"] {{
    animation: maestro-maestric-float 3.6s ease-in-out infinite;
  }}
}}

@keyframes maestro-maestric-float {{
  0%, 100% {{ transform: translateY(0); }}
  50% {{ transform: translateY(-4px); }}
}}
""".strip()
    # JSON quoting makes the CSS an inert JS string even when the binary data
    # or future CSS contains quotes/newlines. The marker and style id make the
    # operation idempotent across WebView load callbacks.
    return (
        "(() => {\n"
        f"  const marker = {json.dumps(BRAND_SCRIPT_MARKER)};\n"
        f"  const id = {json.dumps(BRAND_STYLE_ID)};\n"
        "  document.documentElement.dataset.maestroCare = marker;\n"
        "  let style = document.getElementById(id);\n"
        "  if (!style) { style = document.createElement('style'); style.id = id; (document.head || document.documentElement).appendChild(style); }\n"
        f"  style.textContent = {json.dumps(css)};\n"
        "  const decorate = () => {\n"
        "    const slot = document.querySelector('[data-slot=\"conversation.hero.brand.mark\"]');\n"
        "    const mark = slot?.parentElement;\n"
        "    const hero = mark?.parentElement;\n"
        "    if (!mark || !hero || mark.firstElementChild !== slot) return;\n"
        "    const title = mark.nextElementSibling;\n"
        "    const badge = title?.nextElementSibling;\n"
        "    hero.dataset.maestroHero = marker;\n"
        "    hero.setAttribute('role', 'heading');\n"
        "    hero.setAttribute('aria-level', '1');\n"
        "    hero.setAttribute('aria-label', 'MAESTRO — Agentic Harness');\n"
        "    mark.dataset.maestroHeroMark = marker;\n"
        "    mark.setAttribute('aria-hidden', 'true');\n"
        "    if (title) { title.dataset.maestroHeroTitle = marker; title.setAttribute('aria-hidden', 'true'); }\n"
        "    if (badge) { badge.dataset.maestroHeroBadge = marker; badge.setAttribute('aria-hidden', 'true'); }\n"
        "  };\n"
        f"  const observerKey = {json.dumps(_BRAND_OBSERVER_KEY)};\n"
        "  const oldObserver = globalThis[observerKey];\n"
        "  if (oldObserver && typeof oldObserver.disconnect === 'function') oldObserver.disconnect();\n"
        "  decorate();\n"
        "  if (typeof MutationObserver === 'function') {\n"
        "    const observer = new MutationObserver(decorate);\n"
        "    observer.observe(document.documentElement, { childList: true, subtree: true });\n"
        "    globalThis[observerKey] = observer;\n"
        "  }\n"
        "  return marker;\n"
        "})()"
    )


def apply_maestro_branding(
    window: Any,
    *,
    maestric_path: Path | None = None,
    mark_path: Path | None = None,
) -> None:
    """Apply the native brand script through pywebview's narrow JS surface."""

    evaluate = getattr(window, "evaluate_js", None)
    if not callable(evaluate):
        raise HarnessBrandingError("native window does not expose evaluate_js")
    evaluate(
        build_maestro_brand_script(
            maestric_path=maestric_path,
            mark_path=mark_path,
        )
    )


def bind_maestro_branding(
    window: Any,
    *,
    maestric_path: Path | None = None,
    mark_path: Path | None = None,
) -> Callable[..., None]:
    """Apply branding after every completed WebView document load.

    A one-shot ``webview.start(func=...)`` callback races the first document
    and cannot cover an in-window reload.  pywebview's public ``loaded`` event
    is the deterministic lifecycle boundary.  Returning the handler gives the
    native shell an optional unsubscribe handle without exposing any browser
    state.
    """

    events = getattr(window, "events", None)
    loaded = getattr(events, "loaded", None)
    if loaded is None or not hasattr(loaded, "__iadd__"):
        raise HarnessBrandingError("native window does not expose a loaded event")

    script = build_maestro_brand_script(
        maestric_path=maestric_path,
        mark_path=mark_path,
    )

    def on_loaded(*_args: object, **_kwargs: object) -> None:
        evaluate = getattr(window, "evaluate_js", None)
        if not callable(evaluate):
            raise HarnessBrandingError("native window does not expose evaluate_js")
        evaluate(script)

    previous = getattr(window, _BRAND_LOADED_HANDLER_ATTR, None)
    if previous is not None and hasattr(loaded, "__isub__"):
        try:
            loaded -= previous
        except (TypeError, ValueError):
            pass
    loaded += on_loaded
    try:
        setattr(window, _BRAND_LOADED_HANDLER_ATTR, on_loaded)
    except (AttributeError, TypeError):
        # Some backend proxies reject arbitrary Python attributes. The event
        # itself still owns the callback, so branding remains functional.
        pass

    is_set = getattr(loaded, "is_set", None)
    if callable(is_set) and is_set():
        on_loaded()
    return on_loaded


__all__ = [
    "BRAND_SCRIPT_MARKER",
    "BRAND_STYLE_ID",
    "HarnessBrandingError",
    "apply_maestro_branding",
    "bind_maestro_branding",
    "build_maestro_brand_script",
    "default_maestro_mark_path",
    "default_maestric_path",
]
