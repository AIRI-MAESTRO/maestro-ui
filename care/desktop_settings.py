"""Secret-safe settings bridge for Maestro's native desktop UI.

The browser surface never receives an existing credential.  A settings read
contains ordinary configuration plus ``*_key_present`` booleans; credential
replacement is write-only.  Saves are validated, require a short-lived
one-use confirmation, and update ``config.toml`` and the project ``.env``
with rollback on ordinary Python exceptions.  The multi-file update is not
power-loss atomic.  Secret values are stored in an OS-native keystore and
only ``keystore://`` references are persisted.

This deliberately does *not* use :meth:`CareConfig.save_to_disk`: that legacy
path falls back to a plaintext file keystore when an OS store is unavailable
and its TUI caller mirrors literal keys into ``.env``.  Desktop saves fail
closed instead.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import logging
import os
import secrets
import stat
import tempfile
import threading
import time
import tomllib
from collections.abc import Callable, Mapping, MutableMapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from care.config import (
    DEFAULT_CONFIG_PATH,
    PROJECT_CONFIG_FILENAME,
    CareConfig,
    StagePolicy,
    _API_KEY_SLOTS,
    _format_toml,
    resolve_platform_api_base_url,
    summarize_config_changes,
)
from care.desktop_auth import (
    DesktopAuthConfig,
    DesktopAuthMode,
    save_desktop_auth_mode,
)
from care.runtime.keystore import (
    DEFAULT_SERVICE,
    FileKeystore,
    Keystore,
    MemoryKeystore,
    detect_keystore,
    is_keystore_url,
    make_keystore_url,
    parse_keystore_url,
)
from care.redaction import redact_sensitive_text


_log = logging.getLogger("care.desktop_settings")

DEFAULT_CONFIRMATION_TTL_SECONDS = 90.0
DEFAULT_MAX_CONFIRMATIONS = 64
MAX_TEXT_CHARS = 2_048
MAX_MODEL_CHARS = 512
MAX_SECRET_CHARS = 16_384

PROVIDERS = ("openrouter", "deepseek", "openai", "custom")
SEARCH_PROVIDERS = (
    "tavily",
    "serper",
    "exa",
    "serpapi",
    "brave",
    "duckduckgo",
)
SECRET_ACTIONS = ("keep", "replace", "clear")

_ALLOWED_FIELDS = frozenset(
    {
        "provider",
        "base_url",
        "model",
        "api_key",
        "api_key_action",
        "generation_mode",
        "default_mode",
        "interactive_run",
        "memory_base_url",
        "memory_api_key",
        "memory_api_key_action",
        "platform_base_url",
        "platform_api_key",
        "platform_api_key_action",
        "mutation_base_url",
        "mutation_model",
        "mutation_api_key",
        "mutation_api_key_action",
        "validation_base_url",
        "validation_model",
        "validation_api_key",
        "validation_api_key_action",
        "search_provider",
        "search_api_key",
        "search_api_key_action",
        "upload_api_key",
        "upload_api_key_action",
        "enable_memory_research",
        "enable_web_research",
        "ui_language",
        "reduced_motion",
        "app_lock_mode",
    }
)

_EDITABLE_SECRET_SLOTS: dict[str, tuple[tuple[str, str], ...]] = {
    "api_key": (("mage", "api_key"),),
    "memory_api_key": (("memory", "api_key"),),
    "platform_api_key": (("platform", "api_key"),),
    "search_api_key": (
        ("mage", "web_search_api_key"),
        ("tools", "web_search_api_key"),
    ),
    "mutation_api_key": (("platform", "mutation_api_key"),),
    "validation_api_key": (("platform", "validation_api_key"),),
    "upload_api_key": (("upload", "api_key"),),
}

_MANAGED_NONSECRET_PATHS: tuple[tuple[str, ...], ...] = (
    ("mage", "base_url"),
    ("mage", "model"),
    ("mage", "mode"),
    ("mage", "web_search_provider"),
    ("mage", "enable_memory_research"),
    ("mage", "enable_web_research"),
    ("memory", "base_url"),
    ("platform", "base_url"),
    ("platform", "mutation_base_url"),
    ("platform", "mutation_model"),
    ("platform", "validation_base_url"),
    ("platform", "validation_model"),
    ("tools", "web_search_provider"),
    ("defaults", "ui_language"),
    ("defaults", "reduced_motion"),
    ("chat", "default_mode"),
    ("chat", "mode", "interactive", "run"),
)

_MANAGED_PROJECT_PATHS = frozenset(
    (*_MANAGED_NONSECRET_PATHS, *_API_KEY_SLOTS)
)

_MANAGED_ENV_KEYS = frozenset(
    {
        "CARE_" + "__".join(part.upper() for part in path)
        for path in _MANAGED_NONSECRET_PATHS
    }
    | {
        f"CARE_{parent_name.upper()}__{field_name.upper()}"
        for parent_name, field_name in _API_KEY_SLOTS
    }
)


class SettingsRequestError(ValueError):
    """Stable validation failure that is safe to return to the UI."""

    def __init__(self, code: str, message: str, *, status: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.status = status


class SettingsPersistenceError(RuntimeError):
    """A save failed without exposing the credential or backend stderr."""

    def __init__(self, message: str, *, recovery_incomplete: bool = False) -> None:
        super().__init__(message)
        self.recovery_incomplete = recovery_incomplete


class SettingsLoadError(RuntimeError):
    """A settings read failed without exposing loader details."""


@dataclass(frozen=True)
class SettingsDispatch:
    """HTTP-neutral response from :class:`DesktopSettingsBridge`."""

    status: int
    payload: dict[str, Any]


@dataclass(frozen=True)
class _Confirmation:
    digest: str
    expires_at: float


@dataclass(frozen=True)
class _FileSnapshot:
    existed: bool
    body: bytes
    mode: int


@dataclass(frozen=True)
class _PersistenceResult:
    backend: str
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class _NormalisedUpdate:
    config: CareConfig
    provider: str
    raw_payload: dict[str, Any]
    changes: tuple[str, ...]
    secret_updates: dict[str, "_SecretUpdate"]
    app_lock: DesktopAuthConfig
    app_lock_mode: DesktopAuthMode


@dataclass(frozen=True)
class _SecretUpdate:
    action: str
    value: str | None


class DesktopSettingsBridge:
    """Read and persist the native app's settings with a fail-closed policy."""

    def __init__(
        self,
        *,
        config_loader: Callable[[], CareConfig] | None = None,
        config_path: Path | str | None = None,
        env_path: Path | str | None = None,
        cwd: Path | str | None = None,
        keystore_factory: Callable[[], Keystore] = detect_keystore,
        confirmation_ttl_seconds: float = DEFAULT_CONFIRMATION_TTL_SECONDS,
        max_confirmations: int = DEFAULT_MAX_CONFIRMATIONS,
        clock: Callable[[], float] = time.monotonic,
        token_factory: Callable[[], str] | None = None,
        secret_key_factory: Callable[[], str] | None = None,
        environ: MutableMapping[str, str] | None = None,
        desktop_auth_path: Path | str | None = None,
    ) -> None:
        if confirmation_ttl_seconds <= 0:
            raise ValueError("confirmation_ttl_seconds must be positive")
        if max_confirmations <= 0:
            raise ValueError("max_confirmations must be positive")
        self._cwd = Path(cwd) if cwd is not None else Path.cwd()
        explicit_config_path = config_path is not None
        self._config_path = Path(config_path or DEFAULT_CONFIG_PATH).expanduser()
        self._env_path = (
            Path(env_path).expanduser()
            if env_path is not None
            else self._cwd / ".env"
        )
        self._environ = os.environ if environ is None else environ
        self._desktop_auth_path = (
            Path(desktop_auth_path).expanduser()
            if desktop_auth_path is not None
            else None
        )
        self._config_loader = config_loader or (
            (
                lambda: CareConfig.load(
                    path=self._config_path,
                    env=dict(self._environ),
                    resolve_secrets=False,
                )
            )
            if explicit_config_path
            else lambda: CareConfig.load(
                cwd=self._cwd,
                env=dict(self._environ),
                resolve_secrets=False,
            )
        )
        self._keystore_factory = keystore_factory
        self._ttl = float(confirmation_ttl_seconds)
        self._max_confirmations = int(max_confirmations)
        self._clock = clock
        self._token_factory = token_factory or (lambda: secrets.token_urlsafe(32))
        self._secret_key_factory = secret_key_factory or (lambda: secrets.token_hex(16))
        self._lock = threading.RLock()
        self._confirmations: dict[str, _Confirmation] = {}

    def snapshot(self) -> dict[str, Any]:
        """Return non-secret settings and credential-presence booleans."""

        with self._lock:
            try:
                return self._public_snapshot(
                    self._config_loader(),
                    self._load_desktop_auth(),
                )
            except SettingsLoadError:
                raise
            except Exception as exc:  # noqa: BLE001 - exception text may hold a key
                _log.error(
                    "native settings load failed (%s)",
                    type(exc).__name__,
                    exc_info=False,
                )
                raise SettingsLoadError(
                    "Settings could not be loaded safely."
                ) from None

    def update(
        self,
        settings: Any,
        *,
        confirmation_token: Any = None,
    ) -> SettingsDispatch:
        """Validate, confirm, then persist with rollback on ordinary failures."""

        with self._lock:
            try:
                current = self._config_loader()
                normalised = self._normalise_update(settings, current)
                digest = _settings_digest(normalised.raw_payload)
                confirmation = self._confirm(confirmation_token, digest=digest)
                if confirmation is not None:
                    confirmation.payload["changes"] = list(normalised.changes)
                    return confirmation
                projected_app_lock = DesktopAuthConfig(
                    mode=normalised.app_lock_mode,
                    path=normalised.app_lock.path,
                    overridden_by_environment=(
                        normalised.app_lock.overridden_by_environment
                    ),
                )
                fallback_snapshot = self._public_snapshot(
                    normalised.config,
                    projected_app_lock,
                )
                for field, update in normalised.secret_updates.items():
                    if update.action == "replace":
                        fallback_snapshot["settings"][f"{field}_present"] = True
                    elif update.action == "clear":
                        fallback_snapshot["settings"][f"{field}_present"] = False
                persistence = self._persist(
                    normalised.config,
                    secret_updates=normalised.secret_updates,
                    app_lock=normalised.app_lock,
                    app_lock_mode=normalised.app_lock_mode,
                )
                warnings = list(persistence.warnings)
                try:
                    snapshot = self._public_snapshot(
                        normalised.config,
                        self._load_desktop_auth(),
                    )
                except Exception as exc:  # noqa: BLE001 - post-commit boundary
                    _log.error(
                        "saved settings snapshot refresh failed (%s)",
                        type(exc).__name__,
                        exc_info=False,
                    )
                    snapshot = fallback_snapshot
                    warnings.append(
                        "Settings were saved, but the refreshed settings view "
                        "could not be loaded. Restart MAESTRO to verify the "
                        "applied values."
                    )
                return SettingsDispatch(
                    200,
                    {
                        "ok": True,
                        "settings": snapshot["settings"],
                        "choices": snapshot["choices"],
                        "storage": {
                            **snapshot["storage"],
                            "backend": _display_backend(persistence.backend),
                        },
                        "changes": list(normalised.changes),
                        "warnings": warnings,
                    },
                )
            except SettingsRequestError as exc:
                return _error_dispatch(exc.status, exc.code, str(exc))
            except SettingsPersistenceError as exc:
                code = (
                    "settings_recovery_incomplete"
                    if exc.recovery_incomplete
                    else "settings_save_failed"
                )
                return _error_dispatch(500, code, str(exc))
            except Exception as exc:  # noqa: BLE001 - exception text may hold a key
                _log.error(
                    "native settings save failed (%s)",
                    type(exc).__name__,
                    exc_info=False,
                )
                return _error_dispatch(
                    500,
                    "settings_save_failed",
                    "Settings could not be saved; no secret value was exposed.",
                )

    def _load_desktop_auth(self) -> DesktopAuthConfig:
        return DesktopAuthConfig.load(
            path=self._desktop_auth_path,
            environ=self._environ,
        )

    def _public_snapshot(
        self,
        config: CareConfig,
        app_lock: DesktopAuthConfig,
    ) -> dict[str, Any]:
        mage = config.mage
        platform = config.platform
        tools = config.tools
        interactive = config.chat.mode.interactive.run
        raw_secrets = self._load_effective_raw_secrets()
        settings = {
            "provider": _infer_provider(mage.base_url),
            "base_url": _public_text(mage.base_url),
            "model": _public_text(mage.model),
            "api_key_present": _secret_is_present(
                config,
                _EDITABLE_SECRET_SLOTS["api_key"],
                raw_secrets,
            ),
            "generation_mode": mage.mode,
            "default_mode": config.chat.default_mode,
            "interactive_run": str(interactive or StagePolicy.ASK),
            "memory_base_url": _public_text(config.memory.base_url),
            "memory_api_key_present": _secret_is_present(
                config,
                _EDITABLE_SECRET_SLOTS["memory_api_key"],
                raw_secrets,
            ),
            "platform_base_url": _public_text(platform.base_url),
            "platform_api_key_present": _secret_is_present(
                config,
                _EDITABLE_SECRET_SLOTS["platform_api_key"],
                raw_secrets,
            ),
            "mutation_base_url": _public_text(platform.mutation_base_url),
            "mutation_model": _public_text(platform.mutation_model),
            "mutation_api_key_present": _secret_is_present(
                config,
                _EDITABLE_SECRET_SLOTS["mutation_api_key"],
                raw_secrets,
            ),
            "validation_base_url": _public_text(platform.validation_base_url),
            "validation_model": _public_text(platform.validation_model),
            "validation_api_key_present": _secret_is_present(
                config,
                _EDITABLE_SECRET_SLOTS["validation_api_key"],
                raw_secrets,
            ),
            "search_provider": tools.web_search_provider,
            "search_api_key_present": _secret_is_present(
                config,
                _EDITABLE_SECRET_SLOTS["search_api_key"],
                raw_secrets,
            ),
            "upload_api_key_present": _secret_is_present(
                config,
                _EDITABLE_SECRET_SLOTS["upload_api_key"],
                raw_secrets,
            ),
            "enable_memory_research": mage.enable_memory_research,
            "enable_web_research": mage.enable_web_research,
            "ui_language": config.defaults.ui_language,
            "reduced_motion": config.defaults.reduced_motion,
            "app_lock_mode": app_lock.mode.value,
        }
        return {
            "settings": settings,
            "choices": {
                "provider": list(PROVIDERS),
                "generation_mode": ["fast", "deep"],
                "default_mode": ["interactive", "production"],
                "interactive_run": ["ask", "auto"],
                "search_provider": list(SEARCH_PROVIDERS),
                "ui_language": ["ru", "en"],
                "app_lock_mode": [
                    DesktopAuthMode.DISABLED.value,
                    DesktopAuthMode.TOUCH_ID.value,
                ],
            },
            "storage": {
                "policy": "os_keychain_required",
                "existing_secrets_returned": False,
                "config_target": self._config_path.name,
                "env_target": self._env_path.name,
                "app_lock_target": app_lock.path.name,
                "app_lock_overridden_by_environment": (
                    app_lock.overridden_by_environment
                ),
            },
        }

    def _load_effective_raw_secrets(self) -> dict[tuple[str, str], str]:
        return _effective_raw_secret_values(
            config_body=_snapshot_file(self._config_path).body,
            project_body=self._project_body(),
            env_body=_snapshot_file(self._env_path).body,
            environ=self._environ,
        )

    def _project_body(self) -> bytes:
        project_path = self._cwd / PROJECT_CONFIG_FILENAME
        if _same_path(project_path, self._config_path) or not project_path.exists():
            return b""
        return project_path.read_bytes()

    def _assert_layer_ownership(self, *, request: bool) -> None:
        try:
            message = _layer_ownership_conflict(
                project_body=self._project_body(),
                env_body=_snapshot_file(self._env_path).body,
                environ=self._environ,
            )
        except SettingsPersistenceError as exc:
            if request:
                raise SettingsRequestError(
                    "settings_source_conflict",
                    str(exc),
                    status=409,
                ) from None
            raise
        if message is None:
            return
        if request:
            raise SettingsRequestError(
                "settings_source_conflict",
                message,
                status=409,
            )
        raise SettingsPersistenceError(message)

    def _normalise_update(
        self,
        settings: Any,
        current: CareConfig,
    ) -> _NormalisedUpdate:
        if not isinstance(settings, Mapping):
            raise SettingsRequestError(
                "invalid_settings",
                "settings must be a JSON object",
            )
        unknown = sorted(str(key) for key in set(settings) - _ALLOWED_FIELDS)
        if unknown:
            raise SettingsRequestError(
                "invalid_settings",
                f"unknown field(s): {', '.join(unknown)}",
            )
        raw = dict(settings)
        self._assert_layer_ownership(request=True)
        app_lock = self._load_desktop_auth()
        requested_app_lock_mode = raw.get("app_lock_mode", app_lock.mode.value)
        if (
            app_lock.overridden_by_environment
            and requested_app_lock_mode != app_lock.mode.value
        ):
            raise SettingsRequestError(
                "settings_source_conflict",
                "App lock is controlled by MAESTRO_APP_LOCK_MODE for this launch.",
                status=409,
            )
        app_lock_mode = DesktopAuthMode(
            _choice(
                raw,
                "app_lock_mode",
                app_lock.mode.value,
                (
                    DesktopAuthMode.DISABLED.value,
                    DesktopAuthMode.TOUCH_ID.value,
                ),
            )
        )
        provider = _choice(
            raw,
            "provider",
            _infer_provider(current.mage.base_url),
            PROVIDERS,
        )
        base_url = _url_value(
            raw,
            "base_url",
            current.mage.base_url or "",
            required=False,
        )
        _validate_provider_url(provider, base_url)
        model = _text_value(
            raw,
            "model",
            current.mage.model or "",
            maximum=MAX_MODEL_CHARS,
            required=False,
        )
        generation_mode = _choice(
            raw,
            "generation_mode",
            current.mage.mode,
            ("fast", "deep"),
        )
        default_mode = _choice(
            raw,
            "default_mode",
            current.chat.default_mode,
            ("interactive", "production"),
        )
        interactive_run = _choice(
            raw,
            "interactive_run",
            str(current.chat.mode.interactive.run or StagePolicy.ASK),
            ("ask", "auto"),
        )
        memory_base_url = _url_value(
            raw,
            "memory_base_url",
            current.memory.base_url,
        )
        platform_base_url = _url_value(
            raw,
            "platform_base_url",
            current.platform.base_url,
        )
        mutation_base_url = _url_value(
            raw,
            "mutation_base_url",
            current.platform.mutation_base_url,
        )
        mutation_model = _text_value(
            raw,
            "mutation_model",
            current.platform.mutation_model,
            maximum=MAX_MODEL_CHARS,
            required=True,
        )
        validation_base_url = _url_value(
            raw,
            "validation_base_url",
            current.platform.validation_base_url,
        )
        validation_model = _text_value(
            raw,
            "validation_model",
            current.platform.validation_model,
            maximum=MAX_MODEL_CHARS,
            required=True,
        )
        for field, service_url in (
            ("memory_base_url", memory_base_url),
            ("platform_base_url", platform_base_url),
            ("mutation_base_url", mutation_base_url),
            ("validation_base_url", validation_base_url),
        ):
            _validate_credential_service_url(field, service_url)
        search_provider = _choice(
            raw,
            "search_provider",
            current.tools.web_search_provider,
            SEARCH_PROVIDERS,
        )
        ui_language = _choice(
            raw,
            "ui_language",
            current.defaults.ui_language,
            ("ru", "en"),
        )
        enable_memory_research = _bool_value(
            raw,
            "enable_memory_research",
            current.mage.enable_memory_research,
        )
        enable_web_research = _bool_value(
            raw,
            "enable_web_research",
            current.mage.enable_web_research,
        )
        reduced_motion = _bool_value(
            raw,
            "reduced_motion",
            current.defaults.reduced_motion,
        )

        secret_updates: dict[str, _SecretUpdate] = {}
        for field, slots in _EDITABLE_SECRET_SLOTS.items():
            current_value = _first_secret(current, slots)
            secret_updates[field] = _secret_update(raw, field, current_value)
        raw_secrets = self._load_effective_raw_secrets()
        prospective_platform_route = current.platform.model_copy(
            update={"base_url": platform_base_url}
        )
        _require_secret_rebind_for_endpoint_change(
            field="api_key",
            old_value=current.mage.base_url or "",
            new_value=base_url,
            update=secret_updates["api_key"],
            present=_secret_is_present(
                current,
                _EDITABLE_SECRET_SLOTS["api_key"],
                raw_secrets,
            ),
        )
        _require_secret_rebind_for_endpoint_change(
            field="mutation_api_key",
            old_value=current.platform.mutation_base_url,
            new_value=mutation_base_url,
            update=secret_updates["mutation_api_key"],
            present=_secret_is_present(
                current,
                _EDITABLE_SECRET_SLOTS["mutation_api_key"],
                raw_secrets,
            ),
        )
        _require_secret_rebind_for_endpoint_change(
            field="validation_api_key",
            old_value=current.platform.validation_base_url,
            new_value=validation_base_url,
            update=secret_updates["validation_api_key"],
            present=_secret_is_present(
                current,
                _EDITABLE_SECRET_SLOTS["validation_api_key"],
                raw_secrets,
            ),
        )
        _require_secret_rebind_for_endpoint_change(
            field="search_api_key",
            old_value=current.tools.web_search_provider,
            new_value=search_provider,
            update=secret_updates["search_api_key"],
            present=_secret_is_present(
                current,
                _EDITABLE_SECRET_SLOTS["search_api_key"],
                raw_secrets,
            ),
            compare_origin=False,
        )
        _require_secret_rebind_for_endpoint_change(
            field="memory_api_key",
            old_value=current.memory.base_url,
            new_value=memory_base_url,
            update=secret_updates["memory_api_key"],
            present=_secret_is_present(
                current,
                _EDITABLE_SECRET_SLOTS["memory_api_key"],
                raw_secrets,
            ),
        )
        _require_secret_rebind_for_endpoint_change(
            field="platform_api_key",
            old_value=resolve_platform_api_base_url(current.platform),
            new_value=resolve_platform_api_base_url(prospective_platform_route),
            update=secret_updates["platform_api_key"],
            present=_secret_is_present(
                current,
                _EDITABLE_SECRET_SLOTS["platform_api_key"],
                raw_secrets,
            ),
        )
        upload_url = str(current.upload.url or "").strip()
        if secret_updates["upload_api_key"].action == "replace" and upload_url:
            checked_upload_url = _url_value(
                {"upload_url": upload_url},
                "upload_url",
                "",
                required=True,
            )
            _validate_credential_service_url("upload_url", checked_upload_url)

        mage = current.mage.model_copy(
            update={
                "base_url": base_url,
                "model": model,
                "mode": generation_mode,
                "api_key": secret_updates["api_key"].value,
                "web_search_provider": search_provider,
                "web_search_api_key": secret_updates["search_api_key"].value,
                "enable_memory_research": enable_memory_research,
                "enable_web_research": enable_web_research,
            }
        )
        memory = current.memory.model_copy(
            update={
                "base_url": memory_base_url,
                "api_key": secret_updates["memory_api_key"].value,
            }
        )
        platform = current.platform.model_copy(
            update={
                "base_url": platform_base_url,
                "api_key": secret_updates["platform_api_key"].value,
                "mutation_base_url": mutation_base_url,
                "mutation_model": mutation_model,
                "mutation_api_key": secret_updates["mutation_api_key"].value,
                "validation_base_url": validation_base_url,
                "validation_model": validation_model,
                "validation_api_key": secret_updates["validation_api_key"].value,
            }
        )
        tools = current.tools.model_copy(
            update={
                "web_search_provider": search_provider,
                "web_search_api_key": secret_updates["search_api_key"].value,
            }
        )
        upload = current.upload.model_copy(
            update={"api_key": secret_updates["upload_api_key"].value}
        )
        interactive = current.chat.mode.interactive.model_copy(
            update={"run": StagePolicy(interactive_run)}
        )
        chat_mode = current.chat.mode.model_copy(update={"interactive": interactive})
        chat = current.chat.model_copy(
            update={"default_mode": default_mode, "mode": chat_mode}
        )
        defaults = current.defaults.model_copy(
            update={"ui_language": ui_language, "reduced_motion": reduced_motion}
        )
        candidate = current.model_copy(
            update={
                "mage": mage,
                "memory": memory,
                "platform": platform,
                "upload": upload,
                "tools": tools,
                "chat": chat,
                "defaults": defaults,
            }
        )
        try:
            candidate = CareConfig.model_validate(candidate.model_dump(mode="python"))
        except Exception as exc:
            raise SettingsRequestError(
                "invalid_settings",
                "settings do not satisfy Maestro's configuration schema",
            ) from exc
        audit = candidate.audit_fields()
        if audit:
            raise SettingsRequestError("invalid_settings", audit[0])

        supplied_secrets = tuple(
            value
            for field in _EDITABLE_SECRET_SLOTS
            if isinstance((value := raw.get(field)), str) and value
        )
        changes = tuple(
            redact_sensitive_text(change, supplied_secrets, max_chars=1_024)
            for change in summarize_config_changes(current, candidate)
        )
        if provider != _infer_provider(current.mage.base_url):
            changes = (f"provider: {_infer_provider(current.mage.base_url)} → {provider}", *changes)
        if app_lock_mode is not app_lock.mode:
            changes = (
                f"app lock (next launch): {app_lock.mode.value} → {app_lock_mode.value}",
                *changes,
            )
        digest_payload = dict(raw)
        # Defaults are made explicit so a token cannot be replayed against a
        # semantically different partial update after external config changes.
        digest_payload["__resolved__"] = {
            "provider": provider,
            "base_url": base_url,
            "model": model,
            "generation_mode": generation_mode,
            "default_mode": default_mode,
            "interactive_run": interactive_run,
            "memory_base_url": memory_base_url,
            "platform_base_url": platform_base_url,
            "platform_credential_url": resolve_platform_api_base_url(
                prospective_platform_route
            ),
            "mutation_base_url": mutation_base_url,
            "mutation_model": mutation_model,
            "validation_base_url": validation_base_url,
            "validation_model": validation_model,
            "search_provider": search_provider,
            "upload_url": upload_url,
            "upload_auth_header": str(current.upload.auth_header or ""),
            "ui_language": ui_language,
            "enable_memory_research": enable_memory_research,
            "enable_web_research": enable_web_research,
            "reduced_motion": reduced_motion,
            "app_lock_mode": app_lock_mode.value,
        }
        return _NormalisedUpdate(
            config=candidate,
            provider=provider,
            raw_payload=digest_payload,
            changes=changes or ("No visible values changed; secure storage will be normalised.",),
            secret_updates=secret_updates,
            app_lock=app_lock,
            app_lock_mode=app_lock_mode,
        )

    def _confirm(
        self,
        supplied_token: Any,
        *,
        digest: str,
    ) -> SettingsDispatch | None:
        now = self._clock()
        self._prune_confirmations(now)
        if supplied_token is not None:
            if not isinstance(supplied_token, str) or not supplied_token:
                return _error_dispatch(
                    409,
                    "confirmation_invalid",
                    "confirmation token is invalid or has expired",
                )
            challenge = self._confirmations.pop(supplied_token, None)
            if (
                challenge is None
                or challenge.expires_at <= now
                or not secrets.compare_digest(challenge.digest, digest)
            ):
                return _error_dispatch(
                    409,
                    "confirmation_invalid",
                    "confirmation token is invalid, expired, replayed, or for another settings change",
                )
            return None

        if len(self._confirmations) >= self._max_confirmations:
            oldest = min(
                self._confirmations,
                key=lambda token: self._confirmations[token].expires_at,
            )
            self._confirmations.pop(oldest, None)
        token = self._token_factory()
        while not token or token in self._confirmations:
            token = self._token_factory()
        self._confirmations[token] = _Confirmation(
            digest=digest,
            expires_at=now + self._ttl,
        )
        return SettingsDispatch(
            409,
            {
                "ok": False,
                "error": {
                    "code": "confirmation_required",
                    "message": "Saving settings changes files and secure credential storage.",
                },
                "confirmation": {
                    "token": token,
                    "ttl_seconds": self._ttl,
                    "reason": "Review and confirm the settings save.",
                    "risk": "filesystem_write_or_mutation",
                },
            },
        )

    def _prune_confirmations(self, now: float) -> None:
        expired = [
            token
            for token, challenge in self._confirmations.items()
            if challenge.expires_at <= now
        ]
        for token in expired:
            self._confirmations.pop(token, None)

    def _persist(
        self,
        config: CareConfig,
        *,
        secret_updates: Mapping[str, _SecretUpdate],
        app_lock: DesktopAuthConfig,
        app_lock_mode: DesktopAuthMode,
    ) -> _PersistenceResult:
        slot_updates: dict[tuple[str, str], _SecretUpdate] = {}
        for field, slots in _EDITABLE_SECRET_SLOTS.items():
            slot_updates.update(dict.fromkeys(slots, secret_updates[field]))

        config_snapshot = _snapshot_file(self._config_path)
        env_snapshot = _snapshot_file(self._env_path)
        app_lock_snapshot = _snapshot_file(app_lock.path)
        project_body = self._project_body()
        ownership_conflict = _layer_ownership_conflict(
            project_body=project_body,
            env_body=env_snapshot.body,
            environ=self._environ,
        )
        if ownership_conflict is not None:
            raise SettingsPersistenceError(ownership_conflict)

        # Start from the raw global layer, not the already-merged runtime
        # projection. Only the fields owned by this settings surface are copied
        # from the candidate. This prevents project or process-environment values
        # in unrelated fields from being materialised into global defaults.
        payload = _global_config_payload(config_snapshot.body)
        _copy_paths(
            config.model_dump(mode="json"),
            payload,
            _MANAGED_NONSECRET_PATHS,
        )
        effective_raw = _effective_raw_secret_values(
            config_body=config_snapshot.body,
            project_body=project_body,
            env_body=env_snapshot.body,
            environ=self._environ,
        )
        references_before = _persisted_secret_references(
            config_body=config_snapshot.body,
            env_body=env_snapshot.body,
        )

        stores: list[tuple[tuple[str, str], str, str]] = []
        for slot in _API_KEY_SLOTS:
            parent_name, field_name = slot
            parent = payload.get(parent_name)
            if not isinstance(parent, dict):
                continue
            candidate = parent.get(field_name)
            current = candidate if isinstance(candidate, str) and candidate else None
            update = slot_updates.get(slot, _SecretUpdate("keep", current))
            if update.action == "clear":
                # References disappear from both persisted surfaces. The old
                # immutable key remains available to already-started readers
                # until post-commit cleanup runs below.
                parent[field_name] = None
                continue
            value = update.value
            if update.action == "keep" and slot in effective_raw:
                value = effective_raw[slot]
            if not value:
                parent[field_name] = None
                continue
            if is_keystore_url(value):
                _validate_keystore_reference(value)
                parent[field_name] = value
                continue
            key = _versioned_secret_key(
                parent_name,
                field_name,
                self._secret_key_factory,
            )
            parent[field_name] = make_keystore_url(DEFAULT_SERVICE, key)
            stores.append((slot, key, value))

        keystore: Keystore | None = None
        backend_name = "reference-preserved"
        created_keys: list[str] = []
        try:
            if stores:
                try:
                    keystore = self._keystore_factory()
                except Exception as exc:  # noqa: BLE001
                    raise SettingsPersistenceError(
                        "Secure credential storage is unavailable; no settings were changed."
                    ) from exc
                if (
                    isinstance(keystore, (FileKeystore, MemoryKeystore))
                    or not getattr(keystore, "persistent", False)
                    or getattr(keystore, "name", "") in {"file", "memory"}
                ):
                    raise SettingsPersistenceError(
                        "An OS-native keychain is required; plaintext fallback was refused."
                    )
                backend_name = str(getattr(keystore, "name", ""))
                for _slot, key, value in stores:
                    # Keystore.delete is contractually idempotent. Register the
                    # immutable staging key before store so a backend that commits
                    # and then raises is still cleaned up by rollback.
                    created_keys.append(key)
                    try:
                        keystore.store(DEFAULT_SERVICE, key, value)
                    except Exception as exc:  # noqa: BLE001
                        raise SettingsPersistenceError(
                            "Secure credential storage failed; no settings were changed."
                        ) from exc

            # Validate the persisted reference form before either file changes.
            CareConfig.model_validate(payload)
            config_body = _format_toml(payload).encode("utf-8")
            env_updates = _env_updates(payload)
            env_body = _render_env_update(env_snapshot.body, env_updates)
            _atomic_write(self._config_path, config_body, mode=stat.S_IRUSR | stat.S_IWUSR)
            _atomic_write(self._env_path, env_body, mode=stat.S_IRUSR | stat.S_IWUSR)
            if app_lock_mode is not app_lock.mode:
                save_desktop_auth_mode(
                    app_lock_mode,
                    path=app_lock.path,
                    environ={},
                )
        except Exception as exc:  # noqa: BLE001
            recovery_errors = 0
            for path, snapshot in (
                (self._config_path, config_snapshot),
                (self._env_path, env_snapshot),
                (app_lock.path, app_lock_snapshot),
            ):
                try:
                    _restore_file(path, snapshot)
                except Exception:  # noqa: BLE001
                    recovery_errors += 1
                    _log.error("settings rollback could not restore %s", path)
            if keystore is not None:
                for key in reversed(created_keys):
                    try:
                        keystore.delete(DEFAULT_SERVICE, key)
                    except Exception:  # noqa: BLE001
                        recovery_errors += 1
                        _log.error("settings rollback could not remove a staged secret")
            if isinstance(exc, SettingsPersistenceError):
                raise SettingsPersistenceError(
                    str(exc),
                    recovery_incomplete=bool(recovery_errors),
                ) from exc
            raise SettingsPersistenceError(
                "Settings could not be written; previous files were restored.",
                recovery_incomplete=bool(recovery_errors),
            ) from exc

        warnings: tuple[str, ...] = ()
        try:
            _publish_env_updates(_env_updates(payload), environ=self._environ)
        except Exception as exc:  # noqa: BLE001 - files are already committed
            _log.error(
                "saved settings environment refresh failed (%s)",
                type(exc).__name__,
                exc_info=False,
            )
            warnings += (
                "Settings were saved, but the running app environment could not "
                "be refreshed. Restart MAESTRO before using the new settings.",
            )
        protected_references = {
            value
            for parent_name, field_name in _API_KEY_SLOTS
            if isinstance((parent := payload.get(parent_name)), Mapping)
            and isinstance((value := parent.get(field_name)), str)
            and is_keystore_url(value)
        }
        removed_references = references_before - protected_references
        owned_references = {
            reference
            for reference in removed_references
            if _is_desktop_owned_reference(reference)
        }
        retained_unowned = removed_references - owned_references
        warnings += (
            _retained_reference_warnings(retained_unowned)
            + _purge_old_references(
                owned_references,
                keystore=keystore,
                keystore_factory=self._keystore_factory,
                protected=protected_references,
            )
        )
        return _PersistenceResult(backend_name, warnings)


def _error_dispatch(status: int, code: str, message: str) -> SettingsDispatch:
    return SettingsDispatch(
        status,
        {
            "ok": False,
            "error": {
                "code": code,
                "message": redact_sensitive_text(message, max_chars=2_048),
            },
        },
    )


def _same_path(left: Path, right: Path) -> bool:
    return left.resolve(strict=False) == right.resolve(strict=False)


def _toml_document(body: bytes) -> dict[str, Any]:
    if not body:
        return {}
    try:
        parsed = tomllib.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise SettingsPersistenceError(
            "An existing configuration file is invalid; no settings were changed."
        ) from exc
    return parsed


def _toml_secret_values(body: bytes) -> dict[tuple[str, str], str]:
    parsed = _toml_document(body)
    values: dict[tuple[str, str], str] = {}
    for parent_name, field_name in _API_KEY_SLOTS:
        parent = parsed.get(parent_name)
        value = parent.get(field_name) if isinstance(parent, Mapping) else None
        if isinstance(value, str):
            values[(parent_name, field_name)] = value
    return values


def _env_assignments(body: bytes) -> tuple[tuple[str, str], ...]:
    if not body:
        return ()
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SettingsPersistenceError(
            "The existing .env is not valid UTF-8; no settings were changed."
        ) from exc
    assignments: list[tuple[str, str]] = []
    for raw_line in text.splitlines():
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        body_text = (
            stripped[len("export ") :].lstrip()
            if stripped.startswith("export ")
            else stripped
        )
        key, separator, value = body_text.partition("=")
        key = key.strip()
        if not separator or not key:
            continue
        value = value.strip()
        if (
            len(value) >= 2
            and value[0] == value[-1]
            and value[0] in {'"', "'"}
        ):
            value = value[1:-1]
        assignments.append((key, value))
    return tuple(assignments)


def _env_secret_values(body: bytes) -> dict[tuple[str, str], str]:
    slots_by_key = {
        _secret_env_key(parent_name, field_name): (parent_name, field_name)
        for parent_name, field_name in _API_KEY_SLOTS
    }
    values: dict[tuple[str, str], str] = {}
    for key, value in _env_assignments(body):
        slot = slots_by_key.get(key)
        if slot is not None:
            values[slot] = value
    return values


def _path_present(values: Mapping[str, Any], path: tuple[str, ...]) -> bool:
    cursor: Any = values
    for part in path:
        if not isinstance(cursor, Mapping) or part not in cursor:
            return False
        cursor = cursor[part]
    return True


def _layer_ownership_conflict(
    *,
    project_body: bytes,
    env_body: bytes,
    environ: Mapping[str, str],
) -> str | None:
    project = _toml_document(project_body)
    project_secrets = _toml_secret_values(project_body)
    if any(
        value and not is_keystore_url(value)
        for value in project_secrets.values()
    ):
        return (
            "Project care.toml contains a literal credential; run the "
            "migrate-secrets command before saving native settings."
        )
    for path in _MANAGED_PROJECT_PATHS:
        if _path_present(project, path):
            return (
                "Project care.toml owns a native-settings value; remove the "
                "project override before saving global desktop settings."
            )

    dotenv_values: dict[str, set[str]] = {}
    for key, value in _env_assignments(env_body):
        dotenv_values.setdefault(key, set()).add(value)
    for key in sorted(_MANAGED_ENV_KEYS):
        if key in environ and str(environ[key]) not in dotenv_values.get(key, set()):
            return (
                f"External environment variable {key} overrides native settings; "
                "unset it before saving global desktop settings."
            )
    return None


def _global_config_payload(config_body: bytes) -> dict[str, Any]:
    try:
        config = CareConfig.model_validate(_toml_document(config_body))
    except Exception as exc:  # noqa: BLE001
        raise SettingsPersistenceError(
            "The global configuration is invalid; no settings were changed."
        ) from exc
    return config.model_dump(mode="json")


def _copy_paths(
    source: Mapping[str, Any],
    target: MutableMapping[str, Any],
    paths: tuple[tuple[str, ...], ...],
) -> None:
    for path in paths:
        source_cursor: Any = source
        target_cursor: MutableMapping[str, Any] = target
        for part in path[:-1]:
            if not isinstance(source_cursor, Mapping):
                raise SettingsPersistenceError(
                    "Settings could not be projected safely; no settings were changed."
                )
            source_cursor = source_cursor.get(part)
            nested = target_cursor.get(part)
            if not isinstance(nested, MutableMapping):
                raise SettingsPersistenceError(
                    "Settings could not be projected safely; no settings were changed."
                )
            target_cursor = nested
        leaf = path[-1]
        if not isinstance(source_cursor, Mapping) or leaf not in source_cursor:
            raise SettingsPersistenceError(
                "Settings could not be projected safely; no settings were changed."
            )
        target_cursor[leaf] = source_cursor[leaf]


def _persisted_secret_references(
    *,
    config_body: bytes,
    env_body: bytes,
) -> set[str]:
    references = {
        value
        for value in _toml_secret_values(config_body).values()
        if is_keystore_url(value)
    }
    secret_env_keys = {
        _secret_env_key(parent_name, field_name)
        for parent_name, field_name in _API_KEY_SLOTS
    }
    references.update(
        value
        for key, value in _env_assignments(env_body)
        if key in secret_env_keys and is_keystore_url(value)
    )
    return references


def _effective_raw_secret_values(
    *,
    config_body: bytes,
    project_body: bytes,
    env_body: bytes,
    environ: Mapping[str, str],
) -> dict[tuple[str, str], str]:
    config_values = _toml_secret_values(config_body)
    project_values = _toml_secret_values(project_body)
    dotenv_values = _env_secret_values(env_body)
    effective: dict[tuple[str, str], str] = {}
    for slot in _API_KEY_SLOTS:
        env_key = _secret_env_key(*slot)
        if env_key in environ:
            effective[slot] = str(environ[env_key])
        elif slot in dotenv_values:
            effective[slot] = dotenv_values[slot]
        elif slot in project_values:
            effective[slot] = project_values[slot]
        elif slot in config_values:
            effective[slot] = config_values[slot]
    return effective


def _secret_env_key(parent_name: str, field_name: str) -> str:
    return f"CARE_{parent_name.upper()}__{field_name.upper()}"


def _validate_keystore_reference(value: str) -> None:
    try:
        parse_keystore_url(value)
    except Exception as exc:  # noqa: BLE001
        raise SettingsPersistenceError(
            "An existing credential reference is invalid; no settings were changed."
        ) from exc


def _versioned_secret_key(
    parent_name: str,
    field_name: str,
    factory: Callable[[], str],
) -> str:
    try:
        suffix = factory()
    except Exception as exc:  # noqa: BLE001
        raise SettingsPersistenceError(
            "A secure credential reference could not be created; no settings were changed."
        ) from exc
    if not _valid_secret_suffix(suffix):
        raise SettingsPersistenceError(
            "A secure credential reference could not be created; no settings were changed."
        )
    return f"{parent_name}.{field_name}.{suffix}"


def _valid_secret_suffix(value: Any) -> bool:
    return bool(
        isinstance(value, str)
        and value == value.strip()
        and 16 <= len(value) <= 128
        and all(
            char.isascii() and (char.isalnum() or char in "_-")
            for char in value
        )
    )


def _is_desktop_owned_reference(reference: str) -> bool:
    try:
        service, key = parse_keystore_url(reference)
    except Exception:  # noqa: BLE001
        return False
    if service != DEFAULT_SERVICE:
        return False
    for parent_name, field_name in _API_KEY_SLOTS:
        prefix = f"{parent_name}.{field_name}."
        if key.startswith(prefix):
            return _valid_secret_suffix(key[len(prefix) :])
    return False


def _retained_reference_warnings(references: set[str]) -> tuple[str, ...]:
    if not references:
        return ()
    return (
        "Settings were saved; a legacy or shared Keychain credential was retained "
        "to avoid deleting another project's key.",
    )


def _publish_env_updates(
    updates: Mapping[str, str],
    *,
    environ: Any,
    phase_hook: Callable[[str, Mapping[str, str]], None] | None = None,
) -> None:
    secret_keys = {
        _secret_env_key(parent_name, field_name)
        for parent_name, field_name in _API_KEY_SLOTS
    }
    for key in secret_keys:
        environ[key] = ""
    if phase_hook is not None:
        phase_hook("secrets_cleared", environ)
    for key, value in updates.items():
        if key not in secret_keys:
            environ[key] = value
    if phase_hook is not None:
        phase_hook("nonsecrets_published", environ)
    for key in secret_keys:
        environ[key] = str(updates.get(key, ""))
    if phase_hook is not None:
        phase_hook("secrets_published", environ)


def _purge_old_references(
    references: set[str],
    *,
    keystore: Keystore | None,
    keystore_factory: Callable[[], Keystore],
    protected: set[str],
) -> tuple[str, ...]:
    pending = sorted(references - protected)
    if not pending:
        return ()
    backend = keystore
    if backend is None:
        try:
            backend = keystore_factory()
        except Exception as exc:  # noqa: BLE001
            _log.error(
                "old settings credential cleanup unavailable (%s)",
                type(exc).__name__,
                exc_info=False,
            )
            return (
                "Settings were saved, but an old Keychain credential could not be removed.",
            )
    if (
        isinstance(backend, (FileKeystore, MemoryKeystore))
        or not getattr(backend, "persistent", False)
        or getattr(backend, "name", "") in {"file", "memory"}
    ):
        return (
            "Settings were saved, but an old Keychain credential could not be removed.",
        )
    failed = False
    for reference in pending:
        try:
            service, key = parse_keystore_url(reference)
            delete_noninteractive = getattr(backend, "delete_noninteractive", None)
            if callable(delete_noninteractive):
                delete_noninteractive(service, key)
            else:
                backend.delete(service, key)
        except Exception as exc:  # noqa: BLE001
            failed = True
            _log.error(
                "old settings credential cleanup failed (%s)",
                type(exc).__name__,
                exc_info=False,
            )
    if failed:
        return (
            "Settings were saved, but an old Keychain credential could not be removed.",
        )
    return ()


def _settings_digest(settings: Mapping[str, Any]) -> str:
    body = json.dumps(
        settings,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(body).hexdigest()


def _infer_provider(base_url: str | None) -> str:
    host = (urlsplit(base_url or "").hostname or "").casefold()
    if host == "openrouter.ai":
        return "openrouter"
    if host == "api.deepseek.com":
        return "deepseek"
    if host == "api.openai.com":
        return "openai"
    return "custom"


def _public_text(value: Any) -> str:
    text = redact_sensitive_text(value or "", max_chars=MAX_TEXT_CHARS)
    if text.startswith("eyJ") and text.count(".") == 2:
        return "[redacted]"
    return text


def _validate_provider_url(provider: str, base_url: str) -> None:
    if not base_url and provider == "custom":
        return
    inferred = _infer_provider(base_url)
    if inferred in {"openrouter", "deepseek", "openai"} and provider != inferred:
        raise SettingsRequestError(
            "invalid_settings",
            f"provider must be {inferred} for this hosted endpoint",
        )
    if provider in {"openrouter", "deepseek", "openai"} and inferred != provider:
        expected = {
            "openrouter": "https://openrouter.ai/api/v1",
            "deepseek": "https://api.deepseek.com",
            "openai": "https://api.openai.com/v1",
        }[provider]
        raise SettingsRequestError(
            "invalid_settings",
            f"{provider} provider requires its matching endpoint (for example {expected})",
        )
    parsed = urlsplit(base_url)
    if provider in {"openrouter", "deepseek", "openai"}:
        if parsed.scheme != "https":
            raise SettingsRequestError(
                "invalid_settings",
                f"{provider} credentials require an HTTPS endpoint",
            )
        if parsed.port not in (None, 443):
            raise SettingsRequestError(
                "invalid_settings",
                f"{provider} endpoint must use the canonical HTTPS port",
            )
        expected_paths = {
            "openrouter": {"/api/v1"},
            "deepseek": {"", "/v1"},
            "openai": {"/v1"},
        }[provider]
        if parsed.path.rstrip("/") not in expected_paths:
            raise SettingsRequestError(
                "invalid_settings",
                f"{provider} endpoint path is not supported",
            )
        return
    if parsed.scheme == "http" and not _is_loopback_hostname(parsed.hostname or ""):
        raise SettingsRequestError(
            "invalid_settings",
            "custom HTTP endpoints are allowed only on loopback; use HTTPS for remote hosts",
        )


def _validate_credential_service_url(field: str, value: str) -> None:
    parsed = urlsplit(value)
    if parsed.scheme == "http" and not _is_loopback_hostname(parsed.hostname or ""):
        raise SettingsRequestError(
            "invalid_settings",
            f"{field} allows HTTP only on loopback; use HTTPS for remote hosts",
        )


def _url_origin(value: str) -> tuple[str, str, int | None]:
    parsed = urlsplit(value)
    return parsed.scheme.casefold(), (parsed.hostname or "").casefold(), parsed.port


def _require_secret_rebind_for_endpoint_change(
    *,
    field: str,
    old_value: str,
    new_value: str,
    update: _SecretUpdate,
    present: bool,
    compare_origin: bool = True,
) -> None:
    changed = (
        _url_origin(old_value) != _url_origin(new_value)
        if compare_origin
        else old_value != new_value
    )
    if changed and present and update.action == "keep":
        raise SettingsRequestError(
            "invalid_settings",
            f"{field} must be replaced or cleared when its provider endpoint changes",
        )


def _is_loopback_hostname(hostname: str) -> bool:
    if hostname.casefold() == "localhost":
        return True
    try:
        return ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        return False


def _choice(
    values: Mapping[str, Any],
    field: str,
    default: Any,
    choices: tuple[str, ...],
) -> str:
    value = values.get(field, default)
    if not isinstance(value, str) or value not in choices:
        raise SettingsRequestError(
            "invalid_settings",
            f"{field} must be one of: {', '.join(choices)}",
        )
    return value


def _bool_value(values: Mapping[str, Any], field: str, default: bool) -> bool:
    value = values.get(field, default)
    if not isinstance(value, bool):
        raise SettingsRequestError("invalid_settings", f"{field} must be a boolean")
    return value


def _text_value(
    values: Mapping[str, Any],
    field: str,
    default: str,
    *,
    maximum: int,
    required: bool,
) -> str:
    value = values.get(field, default)
    if not isinstance(value, str):
        raise SettingsRequestError("invalid_settings", f"{field} must be a string")
    value = value.strip()
    if required and not value:
        raise SettingsRequestError("invalid_settings", f"{field} must not be empty")
    if len(value) > maximum or any(
        ord(char) < 32 or ord(char) == 127 for char in value
    ):
        raise SettingsRequestError("invalid_settings", f"{field} is invalid or too long")
    if _public_text(value) != value:
        raise SettingsRequestError(
            "invalid_settings",
            f"{field} looks like a credential; use the matching API key field",
        )
    return value


def _url_value(
    values: Mapping[str, Any],
    field: str,
    default: str,
    *,
    required: bool = True,
) -> str:
    value = _text_value(
        values,
        field,
        default,
        maximum=MAX_TEXT_CHARS,
        required=required,
    )
    if not value and not required:
        return ""
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise SettingsRequestError("invalid_settings", f"{field} must be a valid URL") from exc
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or port is not None and not 0 < port < 65_536
    ):
        raise SettingsRequestError(
            "invalid_settings",
            f"{field} must be an http(s) base URL without credentials, query, or fragment",
        )
    return value.rstrip("/")


def _secret_update(
    values: Mapping[str, Any],
    field: str,
    current: str | None,
) -> _SecretUpdate:
    action_field = f"{field}_action"
    action = values.get(action_field, "replace" if field in values else "keep")
    if not isinstance(action, str) or action not in SECRET_ACTIONS:
        raise SettingsRequestError(
            "invalid_settings",
            f"{action_field} must be one of: {', '.join(SECRET_ACTIONS)}",
        )
    supplied = values.get(field)
    if action == "keep":
        if supplied not in (None, ""):
            raise SettingsRequestError(
                "invalid_settings",
                f"{field} must be omitted when {action_field}=keep",
            )
        return _SecretUpdate("keep", current)
    if action == "clear":
        if supplied not in (None, ""):
            raise SettingsRequestError(
                "invalid_settings",
                f"{field} must be omitted when {action_field}=clear",
            )
        return _SecretUpdate("clear", None)
    if not isinstance(supplied, str) or not supplied.strip():
        raise SettingsRequestError(
            "invalid_settings",
            f"{field} is required when {action_field}=replace",
        )
    if (
        supplied != supplied.strip()
        or len(supplied) > MAX_SECRET_CHARS
        or any(ord(char) < 32 or ord(char) == 127 for char in supplied)
    ):
        raise SettingsRequestError("invalid_settings", f"{field} is invalid or too long")
    return _SecretUpdate("replace", supplied)


def _first_secret(config: CareConfig, slots: tuple[tuple[str, str], ...]) -> str | None:
    for parent_name, field_name in slots:
        parent = getattr(config, parent_name, None)
        value = getattr(parent, field_name, None) if parent is not None else None
        if isinstance(value, str) and value:
            return value
    return None


def _secret_is_present(
    config: CareConfig,
    slots: tuple[tuple[str, str], ...],
    raw_values: Mapping[tuple[str, str], str],
) -> bool:
    return bool(_first_secret(config, slots)) or any(bool(raw_values.get(slot)) for slot in slots)


def _env_updates(payload: Mapping[str, Any]) -> dict[str, str]:
    mage = payload["mage"]
    memory = payload["memory"]
    platform = payload["platform"]
    tools = payload["tools"]
    defaults = payload["defaults"]
    chat = payload["chat"]
    updates = {
        "CARE_MAGE__BASE_URL": str(mage.get("base_url") or ""),
        "CARE_MAGE__MODEL": str(mage.get("model") or ""),
        "CARE_MAGE__MODE": str(mage.get("mode") or "deep"),
        "CARE_MAGE__ENABLE_MEMORY_RESEARCH": _env_bool(mage.get("enable_memory_research")),
        "CARE_MAGE__ENABLE_WEB_RESEARCH": _env_bool(mage.get("enable_web_research")),
        "CARE_MAGE__WEB_SEARCH_PROVIDER": str(mage.get("web_search_provider") or ""),
        "CARE_MEMORY__BASE_URL": str(memory.get("base_url") or ""),
        "CARE_PLATFORM__BASE_URL": str(platform.get("base_url") or ""),
        "CARE_PLATFORM__MUTATION_BASE_URL": str(platform.get("mutation_base_url") or ""),
        "CARE_PLATFORM__MUTATION_MODEL": str(platform.get("mutation_model") or ""),
        "CARE_PLATFORM__VALIDATION_BASE_URL": str(platform.get("validation_base_url") or ""),
        "CARE_PLATFORM__VALIDATION_MODEL": str(platform.get("validation_model") or ""),
        "CARE_TOOLS__WEB_SEARCH_PROVIDER": str(tools.get("web_search_provider") or ""),
        "CARE_DEFAULTS__UI_LANGUAGE": str(defaults.get("ui_language") or "ru"),
        "CARE_DEFAULTS__REDUCED_MOTION": _env_bool(defaults.get("reduced_motion")),
        "CARE_CHAT__DEFAULT_MODE": str(chat.get("default_mode") or "interactive"),
        "CARE_CHAT__MODE__INTERACTIVE__RUN": str(
            ((chat.get("mode") or {}).get("interactive") or {}).get("run") or "ask"
        ),
    }
    for parent_name, field_name in _API_KEY_SLOTS:
        parent = payload.get(parent_name)
        value = parent.get(field_name) if isinstance(parent, Mapping) else None
        updates[f"CARE_{parent_name.upper()}__{field_name.upper()}"] = str(value or "")
    return updates


def _env_bool(value: Any) -> str:
    return "true" if bool(value) else "false"


def _render_env_update(existing: bytes, updates: Mapping[str, str]) -> bytes:
    try:
        existing_text = existing.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SettingsPersistenceError(
            "The existing .env is not valid UTF-8; no settings were changed."
        ) from exc
    remaining = dict(updates)
    written: set[str] = set()
    output: list[str] = []
    for raw_line in existing_text.splitlines():
        stripped = raw_line.strip()
        body = stripped[len("export ") :].lstrip() if stripped.startswith("export ") else stripped
        key, separator, _value = body.partition("=")
        key = key.strip()
        if separator and key in updates and not stripped.startswith("#"):
            if key not in written:
                output.append(f"{key}={_quote_env_value(str(updates[key]))}")
                written.add(key)
                remaining.pop(key, None)
            # Active duplicates are deliberately omitted. Keeping a later
            # plaintext assignment would override the canonical keychain URL
            # in other dotenv implementations.
        else:
            output.append(raw_line)
    if remaining:
        if output and output[-1].strip():
            output.append("")
        output.append("# -- Updated from Maestro desktop settings -------")
        output.extend(
            f"{key}={_quote_env_value(value)}" for key, value in remaining.items()
        )
    return ("\n".join(output) + "\n").encode("utf-8")


def _quote_env_value(value: str) -> str:
    if not value:
        return ""
    if any(char in value for char in (" ", "#", '"', "'", "\t")):
        escaped = value.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    return value


def _snapshot_file(path: Path) -> _FileSnapshot:
    if not path.exists():
        return _FileSnapshot(False, b"", stat.S_IRUSR | stat.S_IWUSR)
    body = path.read_bytes()
    mode = stat.S_IMODE(path.stat().st_mode)
    return _FileSnapshot(True, body, mode)


def _atomic_write(path: Path, body: bytes, *, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=str(path.parent),
    )
    temporary_path = Path(temporary)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(body)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary_path, mode)
        os.replace(temporary_path, path)
    except Exception:
        try:
            temporary_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def _restore_file(path: Path, snapshot: _FileSnapshot) -> None:
    if snapshot.existed:
        _atomic_write(path, snapshot.body, mode=snapshot.mode)
    else:
        path.unlink(missing_ok=True)


def _display_backend(name: str) -> str:
    return {
        "macos-keychain": "macOS Keychain",
        "linux-secret-tool": "secret-tool",
        "reference-preserved": "existing Keychain reference",
    }.get(name, name or "not needed")


__all__ = [
    "DesktopSettingsBridge",
    "SettingsDispatch",
    "SettingsLoadError",
    "SettingsPersistenceError",
    "SettingsRequestError",
]
