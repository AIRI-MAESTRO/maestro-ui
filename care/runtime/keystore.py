"""OS-native secret storage for CARE config (TODO §1 P0).

CARE's provider, Memory, Platform, mutation, validation, search, and upload
credentials must not live as plaintext in ``~/.config/care/config.toml``.
This module stores those logical credential slots behind versioned
``keystore://`` references.

This module provides a tiny secret-storage abstraction:

* :class:`Keystore` — the protocol every backend implements.
* :class:`MacOSKeychainKeystore` — calls Apple's
  ``Security.framework`` directly on Darwin. Secrets never pass
  through a child-process argument, stdin stream, or terminal.
* :class:`LinuxSecretToolKeystore` — shells out to
  ``secret-tool`` (libsecret CLI) on Linux when it's on PATH.
* :class:`FileKeystore` — last-resort fallback. Plain JSON at
  ``~/.config/care/secrets.json`` with mode 0600. The caller is
  expected to surface a clear warning toast pointing out that
  the secret is on disk in clear text.
* :class:`MemoryKeystore` — in-memory backend used by tests and
  by callers that explicitly opt into a non-persistent store.

URL format on disk: ``keystore://<service>/<key>``. A config
value that matches that shape is dereferenced through the
detected keystore on read; any other value is returned as-is
(literal). The asymmetry — *literal on read, URL on write* —
keeps the migration cheap: existing literals keep working,
fresh writes upgrade themselves.

Threading: every backend's read/write/delete is synchronous and
re-entrant against the underlying OS call. The OS keychain and
libsecret backend handle concurrency; CARE does not add a
process-local lock.
"""

from __future__ import annotations

import ctypes
import json
import logging
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping, Protocol, TypeVar

_log = logging.getLogger("care.keystore")
_NonInteractiveResult = TypeVar("_NonInteractiveResult")


DEFAULT_SERVICE = "care"
"""Default keychain service name. Scopes CARE entries so they
don't collide with other apps using the same backend. Override
per call when an integration needs a different namespace."""

DEFAULT_FILE_KEYSTORE_PATH = Path.home() / ".config" / "care" / "secrets.json"
"""Where the file-fallback backend writes when the user didn't
pass an explicit path. Mode 0600 enforced on every write."""

KEYSTORE_URL_SCHEME = "keystore://"


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class KeystoreError(RuntimeError):
    """Raised by any backend when the underlying store rejects an
    operation or the backend itself is unusable in the current
    environment (e.g. Security.framework unavailable on macOS —
    should never happen but cheaper to surface than to crash later)."""


class KeystoreInteractionRequiredError(KeystoreError):
    """The OS keystore refused access without user authentication.

    Callers use this narrower signal to stop a batch of credential lookups
    after the first denied, cancelled, or unavailable authorization attempt.
    The exception carries only the non-secret operation and OS status code.
    """

    def __init__(self, operation: str, status: int) -> None:
        self.operation = operation
        self.status = status
        super().__init__(
            f"macOS Keychain {operation} failed: {_format_osstatus(status)}"
        )


class KeystoreAccessTimeoutError(KeystoreError):
    """A non-interactive OS keystore request did not finish in time."""

    def __init__(self, operation: str, timeout: float) -> None:
        self.operation = operation
        self.timeout = timeout
        super().__init__(
            f"macOS Keychain {operation} did not finish within {timeout:g}s"
        )


# ---------------------------------------------------------------------------
# URL helpers
# ---------------------------------------------------------------------------


def is_keystore_url(value: object) -> bool:
    """Cheap detector — does ``value`` look like a keystore
    reference? Falls back to ``False`` for non-strings so
    callers can throw any config value at it."""
    return isinstance(value, str) and value.startswith(KEYSTORE_URL_SCHEME)


def make_keystore_url(service: str, key: str) -> str:
    """Build the canonical reference URL stored in ``config.toml``.

    Service + key are URL-quoted lightly: only ``/`` is forbidden
    inside ``key`` (it'd ambiguate the URL); everything else
    rides through. Empty service/key raises, since both are
    needed for any backend lookup.
    """
    if not service:
        raise KeystoreError("keystore url requires a non-empty service")
    if not key:
        raise KeystoreError("keystore url requires a non-empty key")
    if "/" in key:
        raise KeystoreError(
            f"keystore key cannot contain '/': {key!r}"
        )
    return f"{KEYSTORE_URL_SCHEME}{service}/{key}"


def parse_keystore_url(url: str) -> tuple[str, str]:
    """Inverse of :func:`make_keystore_url`. Raises
    :class:`KeystoreError` when ``url`` isn't a keystore
    reference or is missing the service / key half.
    """
    if not is_keystore_url(url):
        raise KeystoreError(f"not a keystore url: {url!r}")
    rest = url[len(KEYSTORE_URL_SCHEME):]
    if "/" not in rest:
        raise KeystoreError(
            f"keystore url missing key half: {url!r} "
            f"(expected '{KEYSTORE_URL_SCHEME}<service>/<key>')"
        )
    service, _, key = rest.partition("/")
    if not service or not key:
        raise KeystoreError(
            f"keystore url has empty service or key: {url!r}"
        )
    return service, key


# ---------------------------------------------------------------------------
# Backend protocol
# ---------------------------------------------------------------------------


class Keystore(ABC):
    """The minimal CRD surface every secret backend implements.

    Backends are state-free objects — construct one, call
    :meth:`store` / :meth:`fetch` / :meth:`delete`. Concrete
    subclasses MAY raise :class:`KeystoreError` on backend-side
    failure; callers wrap in `try` to fall back to the file
    store or surface a toast.
    """

    name: str = "abstract"
    """Human-readable backend label — surfaces in toasts /
    diagnostic output so the user knows *where* the secret
    lives."""

    persistent: bool = True
    """`True` when the backend keeps secrets across process
    restarts. `False` only for :class:`MemoryKeystore`."""

    @classmethod
    @abstractmethod
    def available(cls) -> bool:
        """Cheap probe — is this backend usable on the host?
        Detector for :func:`detect_keystore`."""

    @abstractmethod
    def store(self, service: str, key: str, value: str) -> None:
        """Persist ``value`` under ``service`` + ``key``.
        Overwrites any prior value. Empty ``value`` is rejected
        — callers can :meth:`delete` instead."""

    @abstractmethod
    def fetch(self, service: str, key: str) -> str | None:
        """Read the previously-stored secret. Returns ``None``
        when the entry doesn't exist (NOT an error — the
        config-loader treats a missing secret as "no key
        configured" and surfaces the matching toast)."""

    @abstractmethod
    def delete(self, service: str, key: str) -> None:
        """Remove the entry. No-op when the key wasn't there —
        idempotent so wizard "reset" flows don't have to check
        first."""


# ---------------------------------------------------------------------------
# In-memory backend (tests + opt-in)
# ---------------------------------------------------------------------------


class MemoryKeystore(Keystore):
    """In-memory backend. Used by tests + by callers that
    explicitly want a non-persistent store (e.g. running CARE in
    a CI sandbox where Keychain isn't available)."""

    name = "memory"
    persistent = False

    def __init__(self) -> None:
        self._store: dict[tuple[str, str], str] = {}

    @classmethod
    def available(cls) -> bool:
        return True

    def store(self, service: str, key: str, value: str) -> None:
        if not value:
            raise KeystoreError("refusing to store an empty value")
        self._store[(service, key)] = value

    def fetch(self, service: str, key: str) -> str | None:
        return self._store.get((service, key))

    def delete(self, service: str, key: str) -> None:
        self._store.pop((service, key), None)


# ---------------------------------------------------------------------------
# File-fallback backend
# ---------------------------------------------------------------------------


class FileKeystore(Keystore):
    """Last-resort backend — plain JSON file with mode 0600.

    Used when neither the OS-native backend nor a custom
    backend is available. The file lives at
    ``~/.config/care/secrets.json`` by default; the caller can
    override for tests / multi-profile setups.
    """

    name = "file"
    persistent = True

    def __init__(self, path: Path | None = None) -> None:
        self.path = Path(path) if path is not None else DEFAULT_FILE_KEYSTORE_PATH

    @classmethod
    def available(cls) -> bool:
        # Always usable — the worst case is a permission error
        # at write time, which we surface as `KeystoreError`.
        return True

    def store(self, service: str, key: str, value: str) -> None:
        if not value:
            raise KeystoreError("refusing to store an empty value")
        data = self._load()
        data.setdefault(service, {})[key] = value
        self._save(data)

    def fetch(self, service: str, key: str) -> str | None:
        data = self._load()
        return data.get(service, {}).get(key)

    def delete(self, service: str, key: str) -> None:
        data = self._load()
        bucket = data.get(service)
        if not bucket or key not in bucket:
            return
        del bucket[key]
        if not bucket:
            del data[service]
        self._save(data)

    def _load(self) -> dict[str, dict[str, str]]:
        if not self.path.exists():
            return {}
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise KeystoreError(
                f"FileKeystore failed to read {self.path}: {exc}"
            ) from exc

    def _save(self, data: dict[str, dict[str, str]]) -> None:
        tmp: Path | None = None
        tmp_fd: int | None = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            body = json.dumps(data, indent=2, sort_keys=True)
            # `mkstemp + replace` gives concurrent writers unique
            # same-directory staging files. Set the restrictive mode
            # on the open descriptor before the first secret byte.
            tmp_fd, tmp_name = tempfile.mkstemp(
                prefix=f".{self.path.name}.",
                suffix=".tmp",
                dir=self.path.parent,
            )
            tmp = Path(tmp_name)
            os.fchmod(tmp_fd, stat.S_IRUSR | stat.S_IWUSR)
            with os.fdopen(tmp_fd, "w", encoding="utf-8") as fp:
                tmp_fd = None
                fp.write(body)
            os.replace(tmp, self.path)
        except OSError as exc:
            raise KeystoreError(
                f"FileKeystore failed to write {self.path}: {exc}"
            ) from exc
        finally:
            if tmp_fd is not None:
                try:
                    os.close(tmp_fd)
                except OSError:
                    pass
            if tmp is not None:
                try:
                    tmp.unlink()
                except OSError:
                    pass


# ---------------------------------------------------------------------------
# macOS Keychain backend
# ---------------------------------------------------------------------------


_SECURITY_FRAMEWORK_PATH = "/System/Library/Frameworks/Security.framework/Security"
_COREFOUNDATION_FRAMEWORK_PATH = (
    "/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation"
)

_ERR_SEC_SUCCESS = 0
_ERR_SEC_PARAM = -50
_ERR_SEC_ALLOCATE = -108
_ERR_SEC_USER_CANCELED = -128
_ERR_SEC_NOT_AVAILABLE = -25291
_ERR_SEC_READ_ONLY = -25292
_ERR_SEC_AUTH_FAILED = -25293
_ERR_SEC_NO_SUCH_KEYCHAIN = -25294
_ERR_SEC_INVALID_KEYCHAIN = -25295
_ERR_SEC_DUPLICATE_KEYCHAIN = -25296
_ERR_SEC_DUPLICATE_ITEM = -25299
_ERR_SEC_ITEM_NOT_FOUND = -25300
_ERR_SEC_INVALID_ITEM_REF = -25304
_ERR_SEC_NO_DEFAULT_KEYCHAIN = -25307
_ERR_SEC_INTERACTION_NOT_ALLOWED = -25308
_ERR_SEC_INTERACTION_REQUIRED = -25315
_ERR_SEC_DECODE = -26275
_ERR_SEC_MISSING_ENTITLEMENT = -34018

_MACOS_INTERACTION_REQUIRED_STATUSES = frozenset(
    {
        _ERR_SEC_USER_CANCELED,
        _ERR_SEC_AUTH_FAILED,
        _ERR_SEC_INTERACTION_NOT_ALLOWED,
        _ERR_SEC_INTERACTION_REQUIRED,
    }
)

_OSSTATUS_NAMES = {
    _ERR_SEC_SUCCESS: "errSecSuccess",
    _ERR_SEC_PARAM: "errSecParam",
    _ERR_SEC_ALLOCATE: "errSecAllocate",
    _ERR_SEC_USER_CANCELED: "errSecUserCanceled",
    _ERR_SEC_NOT_AVAILABLE: "errSecNotAvailable",
    _ERR_SEC_READ_ONLY: "errSecReadOnly",
    _ERR_SEC_AUTH_FAILED: "errSecAuthFailed",
    _ERR_SEC_NO_SUCH_KEYCHAIN: "errSecNoSuchKeychain",
    _ERR_SEC_INVALID_KEYCHAIN: "errSecInvalidKeychain",
    _ERR_SEC_DUPLICATE_KEYCHAIN: "errSecDuplicateKeychain",
    _ERR_SEC_DUPLICATE_ITEM: "errSecDuplicateItem",
    _ERR_SEC_ITEM_NOT_FOUND: "errSecItemNotFound",
    _ERR_SEC_INVALID_ITEM_REF: "errSecInvalidItemRef",
    _ERR_SEC_NO_DEFAULT_KEYCHAIN: "errSecNoDefaultKeychain",
    _ERR_SEC_INTERACTION_NOT_ALLOWED: "errSecInteractionNotAllowed",
    _ERR_SEC_INTERACTION_REQUIRED: "errSecInteractionRequired",
    _ERR_SEC_DECODE: "errSecDecode",
    _ERR_SEC_MISSING_ENTITLEMENT: "errSecMissingEntitlement",
}

_UINT32_MAX = (1 << 32) - 1


def _format_osstatus(status: int) -> str:
    name = _OSSTATUS_NAMES.get(status, "unknown OSStatus")
    return f"{name} ({status})"


def _raise_macos_status(operation: str, status: int) -> None:
    if status in _MACOS_INTERACTION_REQUIRED_STATUSES:
        raise KeystoreInteractionRequiredError(operation, status)
    raise KeystoreError(
        f"macOS Keychain {operation} failed: {_format_osstatus(status)}"
    )


def _encode_keychain_text(value: str, *, field: str) -> bytes:
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise KeystoreError(f"macOS Keychain {field} is not valid UTF-8 text") from exc
    if len(encoded) > _UINT32_MAX:
        raise KeystoreError(
            f"macOS Keychain {field} exceeds the UInt32 byte-length limit"
        )
    return encoded


def _encode_secret(value: str) -> bytearray:
    try:
        encoded = bytearray(value, "utf-8")
    except UnicodeEncodeError:
        # Do not chain the codec exception: it retains the original string.
        raise KeystoreError(
            "macOS Keychain secret is not valid UTF-8 text"
        ) from None
    if len(encoded) > _UINT32_MAX:
        _zero_bytearray(encoded)
        raise KeystoreError(
            "macOS Keychain secret exceeds the UInt32 byte-length limit"
        )
    return encoded


def _zero_bytearray(value: bytearray) -> None:
    """Best-effort in-place wipe for temporary mutable secret copies.

    Python strings owned by the caller cannot be wiped, but every mutable
    buffer created by this backend is cleared before it leaves our control.
    """

    if not value:
        return
    buffer = (ctypes.c_ubyte * len(value)).from_buffer(value)
    ctypes.memset(ctypes.addressof(buffer), 0, len(value))


class _MacOSKeychainAPI(Protocol):
    """Narrow seam around Security.framework for hermetic tests."""

    def add_generic_password(
        self,
        service: bytes,
        account: bytes,
        password: bytearray,
    ) -> tuple[int, object | None]: ...

    def find_item(
        self,
        service: bytes,
        account: bytes,
    ) -> tuple[int, object | None]: ...

    def copy_password(
        self,
        service: bytes,
        account: bytes,
    ) -> tuple[int, bytearray | None]: ...

    def copy_password_noninteractive(
        self,
        service: bytes,
        account: bytes,
    ) -> tuple[int, bytearray | None]: ...

    def delete_generic_password_noninteractive(
        self,
        service: bytes,
        account: bytes,
    ) -> int: ...

    def modify_item(self, item: object, password: bytearray) -> int: ...

    def delete_item(self, item: object) -> int: ...

    def release_item(self, item: object) -> None: ...


class _MacOSSecurityAPI:
    """Typed ctypes bindings for the generic-password Keychain API.

    Fixed framework paths are intentional: they remain valid in a py2app
    bundle and avoid relying on ``PATH`` or an external executable.
    """

    def __init__(
        self,
        *,
        security: object | None = None,
        core_foundation: object | None = None,
        secitem_symbols: Mapping[str, int] | None = None,
    ) -> None:
        self._security = security or ctypes.CDLL(_SECURITY_FRAMEWORK_PATH)
        self._core_foundation = core_foundation or ctypes.CDLL(
            _COREFOUNDATION_FRAMEWORK_PATH
        )
        # Test seam for the exported CFTypeRef constants used by the modern
        # SecItem query API. Production resolves them directly from Apple's
        # frameworks; no PyObjC dependency is needed or bundled.
        self._injected_secitem_symbols = dict(secitem_symbols or {})
        self._secitem_configured = False
        self._configure_signatures()

    @classmethod
    def load(cls) -> _MacOSSecurityAPI:
        return cls()

    def _configure_signatures(self) -> None:
        void_p = ctypes.c_void_p
        uint32 = ctypes.c_uint32
        osstatus = ctypes.c_int32

        add = self._security.SecKeychainAddGenericPassword
        add.argtypes = [
            void_p,
            uint32,
            void_p,
            uint32,
            void_p,
            uint32,
            void_p,
            ctypes.POINTER(void_p),
        ]
        add.restype = osstatus

        find = self._security.SecKeychainFindGenericPassword
        find.argtypes = [
            void_p,
            uint32,
            void_p,
            uint32,
            void_p,
            ctypes.POINTER(uint32),
            ctypes.POINTER(void_p),
            ctypes.POINTER(void_p),
        ]
        find.restype = osstatus

        modify = self._security.SecKeychainItemModifyAttributesAndData
        modify.argtypes = [void_p, void_p, uint32, void_p]
        modify.restype = osstatus

        delete = self._security.SecKeychainItemDelete
        delete.argtypes = [void_p]
        delete.restype = osstatus

        free_content = self._security.SecKeychainItemFreeContent
        free_content.argtypes = [void_p, void_p]
        free_content.restype = osstatus

        set_interaction = self._security.SecKeychainSetUserInteractionAllowed
        set_interaction.argtypes = [ctypes.c_ubyte]
        set_interaction.restype = osstatus

        release = self._core_foundation.CFRelease
        release.argtypes = [void_p]
        release.restype = None

    def _configure_secitem_signatures(self) -> None:
        """Lazily bind the per-query, non-interactive SecItem API.

        Keeping this separate preserves the small legacy Keychain ABI used by
        normal Settings reads/writes, and gives old test doubles no obligation
        to implement APIs they do not exercise.
        """

        if self._secitem_configured:
            return
        void_p = ctypes.c_void_p
        cf_index = ctypes.c_long

        copy_matching = self._security.SecItemCopyMatching
        copy_matching.argtypes = [void_p, ctypes.POINTER(void_p)]
        copy_matching.restype = ctypes.c_int32

        item_delete = self._security.SecItemDelete
        item_delete.argtypes = [void_p]
        item_delete.restype = ctypes.c_int32

        dictionary_create = self._core_foundation.CFDictionaryCreateMutable
        dictionary_create.argtypes = [void_p, cf_index, void_p, void_p]
        dictionary_create.restype = void_p

        dictionary_set = self._core_foundation.CFDictionarySetValue
        dictionary_set.argtypes = [void_p, void_p, void_p]
        dictionary_set.restype = None

        string_create = self._core_foundation.CFStringCreateWithBytes
        string_create.argtypes = [
            void_p,
            void_p,
            cf_index,
            ctypes.c_uint32,
            ctypes.c_ubyte,
        ]
        string_create.restype = void_p

        get_type_id = self._core_foundation.CFGetTypeID
        get_type_id.argtypes = [void_p]
        get_type_id.restype = ctypes.c_ulong

        data_get_type_id = self._core_foundation.CFDataGetTypeID
        data_get_type_id.argtypes = []
        data_get_type_id.restype = ctypes.c_ulong

        data_get_length = self._core_foundation.CFDataGetLength
        data_get_length.argtypes = [void_p]
        data_get_length.restype = cf_index

        data_get_bytes = self._core_foundation.CFDataGetBytePtr
        data_get_bytes.argtypes = [void_p]
        data_get_bytes.restype = ctypes.POINTER(ctypes.c_ubyte)

        self._secitem_configured = True

    def _secitem_symbol(self, name: str) -> ctypes.c_void_p:
        injected = self._injected_secitem_symbols.get(name)
        if injected is not None:
            return ctypes.c_void_p(injected)
        try:
            return ctypes.c_void_p.in_dll(self._security, name)
        except (ValueError, AttributeError):
            return ctypes.c_void_p.in_dll(self._core_foundation, name)

    def _secitem_callbacks(self, name: str) -> ctypes.c_void_p:
        injected = self._injected_secitem_symbols.get(name)
        if injected is not None:
            return ctypes.c_void_p(injected)
        callback = ctypes.c_byte.in_dll(self._core_foundation, name)
        return ctypes.c_void_p(ctypes.addressof(callback))

    def _cf_string(self, value: bytes) -> ctypes.c_void_p:
        # kCFStringEncodingUTF8. Explicit length preserves embedded NUL bytes.
        buffer = self._text_buffer(value)
        result = self._core_foundation.CFStringCreateWithBytes(
            None,
            ctypes.cast(buffer, ctypes.c_void_p),
            len(value),
            0x08000100,
            0,
        )
        if not result:
            raise KeystoreError("macOS Keychain could not allocate query text")
        return ctypes.c_void_p(result)

    @staticmethod
    def _text_buffer(value: bytes) -> ctypes.Array[ctypes.c_ubyte]:
        # Allocate at least one byte so empty service/account strings still
        # have a stable pointer. The API receives the exact (possibly zero)
        # byte length separately and never relies on C-string termination.
        buffer = (ctypes.c_ubyte * max(1, len(value)))()
        if value:
            ctypes.memmove(buffer, value, len(value))
        return buffer

    @staticmethod
    def _password_buffer(
        value: bytearray,
    ) -> ctypes.Array[ctypes.c_ubyte]:
        return (ctypes.c_ubyte * len(value)).from_buffer(value)

    def add_generic_password(
        self,
        service: bytes,
        account: bytes,
        password: bytearray,
    ) -> tuple[int, object | None]:
        service_buffer = self._text_buffer(service)
        account_buffer = self._text_buffer(account)
        password_buffer = self._password_buffer(password)
        item = ctypes.c_void_p()
        status = int(
            self._security.SecKeychainAddGenericPassword(
                None,
                len(service),
                ctypes.cast(service_buffer, ctypes.c_void_p),
                len(account),
                ctypes.cast(account_buffer, ctypes.c_void_p),
                len(password),
                ctypes.cast(password_buffer, ctypes.c_void_p),
                ctypes.byref(item),
            )
        )
        return status, item if item.value else None

    def find_item(
        self,
        service: bytes,
        account: bytes,
    ) -> tuple[int, object | None]:
        service_buffer = self._text_buffer(service)
        account_buffer = self._text_buffer(account)
        item = ctypes.c_void_p()
        status = int(
            self._security.SecKeychainFindGenericPassword(
                None,
                len(service),
                ctypes.cast(service_buffer, ctypes.c_void_p),
                len(account),
                ctypes.cast(account_buffer, ctypes.c_void_p),
                None,
                None,
                ctypes.byref(item),
            )
        )
        return status, item if item.value else None

    def copy_password(
        self,
        service: bytes,
        account: bytes,
    ) -> tuple[int, bytearray | None]:
        service_buffer = self._text_buffer(service)
        account_buffer = self._text_buffer(account)
        password_length = ctypes.c_uint32()
        password_data = ctypes.c_void_p()
        secret: bytearray | None = None

        status = int(
            self._security.SecKeychainFindGenericPassword(
                None,
                len(service),
                ctypes.cast(service_buffer, ctypes.c_void_p),
                len(account),
                ctypes.cast(account_buffer, ctypes.c_void_p),
                ctypes.byref(password_length),
                ctypes.byref(password_data),
                None,
            )
        )
        try:
            if status == _ERR_SEC_SUCCESS:
                length = password_length.value
                if length and not password_data.value:
                    raise KeystoreError(
                        "macOS Keychain returned an invalid password buffer"
                    )
                secret = bytearray(length)
                if length:
                    destination = (ctypes.c_ubyte * length).from_buffer(secret)
                    ctypes.memmove(destination, password_data, length)
            return status, secret
        finally:
            if password_data.value:
                # SecKeychainItemFreeContent owns this allocation. Wipe it
                # before handing it back to Security.framework.
                if password_length.value:
                    ctypes.memset(
                        password_data,
                        0,
                        password_length.value,
                    )
                free_status = int(
                    self._security.SecKeychainItemFreeContent(
                        None,
                        password_data,
                    )
                )
                if free_status != _ERR_SEC_SUCCESS:
                    _log.warning(
                        "macOS Keychain password-buffer cleanup failed: %s",
                        _format_osstatus(free_status),
                    )

    def copy_password_noninteractive(
        self,
        service: bytes,
        account: bytes,
    ) -> tuple[int, bytearray | None]:
        """Copy a generic password while forbidding authentication UI.

        ``kSecUseAuthenticationUIFail`` applies to this query only. Unlike
        ``SecKeychainSetUserInteractionAllowed(false)``, it does not mutate a
        process-global policy, so explicit Settings operations can keep using
        the ordinary interactive Keychain path in the same desktop process.
        Omitting ``kSecUseDataProtectionKeychain`` intentionally keeps legacy
        ``keystore://care/...`` items in the default/login Keychain visible.
        """

        self._configure_secitem_signatures()
        query = self._core_foundation.CFDictionaryCreateMutable(
            None,
            0,
            self._secitem_callbacks("kCFTypeDictionaryKeyCallBacks"),
            self._secitem_callbacks("kCFTypeDictionaryValueCallBacks"),
        )
        if not query:
            raise KeystoreError("macOS Keychain could not allocate query")

        service_string: ctypes.c_void_p | None = None
        account_string: ctypes.c_void_p | None = None
        result = ctypes.c_void_p()
        secret: bytearray | None = None
        try:
            service_string = self._cf_string(service)
            account_string = self._cf_string(account)
            query_values = (
                ("kSecClass", "kSecClassGenericPassword"),
                ("kSecAttrService", service_string),
                ("kSecAttrAccount", account_string),
                ("kSecReturnData", self._secitem_symbol("kCFBooleanTrue")),
                ("kSecMatchLimit", "kSecMatchLimitOne"),
                (
                    "kSecUseAuthenticationUI",
                    "kSecUseAuthenticationUIFail",
                ),
            )
            for key_name, raw_value in query_values:
                value = (
                    self._secitem_symbol(raw_value)
                    if isinstance(raw_value, str)
                    else raw_value
                )
                self._core_foundation.CFDictionarySetValue(
                    query,
                    self._secitem_symbol(key_name),
                    value,
                )

            status = int(
                self._security.SecItemCopyMatching(
                    query,
                    ctypes.byref(result),
                )
            )
            if status != _ERR_SEC_SUCCESS:
                return status, None
            if not result.value:
                raise KeystoreError(
                    "macOS Keychain non-interactive fetch returned no data"
                )
            if (
                self._core_foundation.CFGetTypeID(result)
                != self._core_foundation.CFDataGetTypeID()
            ):
                raise KeystoreError(
                    "macOS Keychain non-interactive fetch returned invalid data"
                )
            length = int(self._core_foundation.CFDataGetLength(result))
            if length < 0:
                raise KeystoreError(
                    "macOS Keychain non-interactive fetch returned invalid data"
                )
            source = self._core_foundation.CFDataGetBytePtr(result)
            if length and not source:
                raise KeystoreError(
                    "macOS Keychain non-interactive fetch returned invalid data"
                )
            secret = bytearray(length)
            if length:
                destination = (ctypes.c_ubyte * length).from_buffer(secret)
                ctypes.memmove(destination, source, length)
            return status, secret
        finally:
            if result.value:
                self._core_foundation.CFRelease(result)
            if service_string is not None:
                self._core_foundation.CFRelease(service_string)
            if account_string is not None:
                self._core_foundation.CFRelease(account_string)
            self._core_foundation.CFRelease(query)

    def delete_generic_password_noninteractive(
        self,
        service: bytes,
        account: bytes,
    ) -> int:
        """Delete a generic password while forbidding authentication UI."""

        self._configure_secitem_signatures()
        query = self._core_foundation.CFDictionaryCreateMutable(
            None,
            0,
            self._secitem_callbacks("kCFTypeDictionaryKeyCallBacks"),
            self._secitem_callbacks("kCFTypeDictionaryValueCallBacks"),
        )
        if not query:
            raise KeystoreError("macOS Keychain could not allocate delete query")

        service_string: ctypes.c_void_p | None = None
        account_string: ctypes.c_void_p | None = None
        try:
            service_string = self._cf_string(service)
            account_string = self._cf_string(account)
            query_values = (
                ("kSecClass", "kSecClassGenericPassword"),
                ("kSecAttrService", service_string),
                ("kSecAttrAccount", account_string),
                (
                    "kSecUseAuthenticationUI",
                    "kSecUseAuthenticationUIFail",
                ),
            )
            for key_name, raw_value in query_values:
                value = (
                    self._secitem_symbol(raw_value)
                    if isinstance(raw_value, str)
                    else raw_value
                )
                self._core_foundation.CFDictionarySetValue(
                    query,
                    self._secitem_symbol(key_name),
                    value,
                )
            return int(self._security.SecItemDelete(query))
        finally:
            if service_string is not None:
                self._core_foundation.CFRelease(service_string)
            if account_string is not None:
                self._core_foundation.CFRelease(account_string)
            self._core_foundation.CFRelease(query)

    def modify_item(self, item: object, password: bytearray) -> int:
        password_buffer = self._password_buffer(password)
        return int(
            self._security.SecKeychainItemModifyAttributesAndData(
                item,
                None,
                len(password),
                ctypes.cast(password_buffer, ctypes.c_void_p),
            )
        )

    def delete_item(self, item: object) -> int:
        return int(self._security.SecKeychainItemDelete(item))

    def release_item(self, item: object) -> None:
        self._core_foundation.CFRelease(item)

    def set_user_interaction_allowed(self, allowed: bool) -> int:
        """Set the legacy Keychain UI policy for this dedicated process."""

        return int(
            self._security.SecKeychainSetUserInteractionAllowed(
                1 if allowed else 0,
            )
        )


def disable_macos_keychain_interaction() -> None:
    """Prevent legacy Keychain calls from opening system UI in this process.

    ``SecKeychainSetUserInteractionAllowed`` is process-global.  Use it only in
    a process whose product policy forbids Keychain UI, including the native
    MAESTRO harness. Reads that require authentication then fail with
    ``errSecInteractionNotAllowed`` and the existing config boundary treats the
    affected credential as unavailable instead of opening a dialog.
    """

    if sys.platform != "darwin":
        return
    try:
        api = _MacOSSecurityAPI.load()
    except Exception as exc:  # noqa: BLE001
        raise KeystoreError(
            "macOS Security.framework could not be loaded for the "
            "non-interactive worker guard"
        ) from exc
    status = api.set_user_interaction_allowed(False)
    if status != _ERR_SEC_SUCCESS:
        _raise_macos_status("disable user interaction", status)


@dataclass(frozen=True)
class _Subprocess:
    """Tiny seam so tests can inject a synthetic subprocess
    invoker without monkey-patching the real one. The default
    instance just forwards to :func:`subprocess.run` with the
    arguments the OS-native backends rely on."""

    def run(
        self,
        args: list[str],
        *,
        input: str | None = None,
        capture_output: bool = True,
        check: bool = False,
        timeout: float = 10.0,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            args,
            input=input,
            capture_output=capture_output,
            text=True,
            check=check,
            timeout=timeout,
        )


_DEFAULT_SUBPROCESS = _Subprocess()


class MacOSKeychainKeystore(Keystore):
    """macOS Keychain backend.

    Calls Security.framework in-process. Each entry is a generic password
    keyed by ``service`` + ``key`` (account, in Keychain parlance). No secret
    is sent through argv, stdin, a subprocess, or an interactive terminal.
    """

    name = "macos-keychain"
    persistent = True

    def __init__(
        self,
        *,
        api: _MacOSKeychainAPI | None = None,
        noninteractive_timeout: float = 2.0,
    ) -> None:
        if noninteractive_timeout <= 0:
            raise ValueError("noninteractive_timeout must be positive")
        self._noninteractive_timeout = float(noninteractive_timeout)
        if api is not None:
            self._api = api
            return
        try:
            self._api = _MacOSSecurityAPI.load()
        except Exception as exc:  # noqa: BLE001
            # Do not forward a raw backend exception. Some callers print this
            # message, and backend exceptions are not part of the stable API.
            raise KeystoreError(
                f"macOS Security.framework could not be loaded ({type(exc).__name__})"
            ) from exc

    def _run_noninteractive_call(
        self,
        operation: str,
        call: Callable[[], "_NonInteractiveResult"],
        *,
        discard: Callable[["_NonInteractiveResult"], None] | None = None,
    ) -> "_NonInteractiveResult":
        """Run a Security.framework query without letting it stall the app.

        ``kSecUseAuthenticationUIFail`` prevents this query from creating UI,
        but macOS can still serialize it behind an already-open Keychain
        authorization dialog.  A daemon worker keeps that system stall outside
        the resident agent's request path.  If the late result contains secret
        bytes, ``discard`` wipes them before the worker exits.
        """

        completed = threading.Event()
        lock = threading.Lock()
        results: list[_NonInteractiveResult] = []
        failures: list[Exception] = []
        abandoned = False

        def invoke() -> None:
            nonlocal abandoned
            try:
                value = call()
            except Exception as exc:  # noqa: BLE001 - re-raised on the caller
                with lock:
                    if not abandoned:
                        failures.append(exc)
                        completed.set()
                return
            with lock:
                if abandoned:
                    if discard is not None:
                        try:
                            discard(value)
                        except Exception as exc:  # noqa: BLE001 - best-effort wipe
                            _log.warning(
                                "late macOS Keychain result cleanup failed (%s)",
                                type(exc).__name__,
                            )
                    return
                results.append(value)
                completed.set()

        worker = threading.Thread(
            target=invoke,
            name="care-keychain-noninteractive",
            daemon=True,
        )
        worker.start()
        timed_out = not completed.wait(self._noninteractive_timeout)
        with lock:
            # Close the race where the worker completed immediately after
            # Event.wait() returned False but before this lock was acquired.
            if timed_out and not completed.is_set():
                abandoned = True
                raise KeystoreAccessTimeoutError(
                    operation,
                    self._noninteractive_timeout,
                )
            if failures:
                raise failures[0]
            if not results:
                raise KeystoreError(
                    f"macOS Keychain {operation} returned no result"
                )
            return results[0]

    @staticmethod
    def _discard_password_copy(
        result: tuple[int, bytearray | None],
    ) -> None:
        _status, password = result
        if password is not None:
            _zero_bytearray(password)

    @classmethod
    def available(cls) -> bool:
        if sys.platform != "darwin":
            return False
        try:
            _MacOSSecurityAPI.load()
        except Exception:  # noqa: BLE001
            return False
        return True

    def _labels(self, service: str, key: str) -> tuple[bytes, bytes]:
        return (
            _encode_keychain_text(service, field="service"),
            _encode_keychain_text(key, field="account"),
        )

    def _release(self, item: object | None) -> None:
        if item is None:
            return
        try:
            self._api.release_item(item)
        except Exception as exc:  # noqa: BLE001
            # A CFRelease failure is not actionable for the caller. Keep the
            # primary Keychain result, but leave a sanitized diagnostic.
            _log.warning(
                "macOS Keychain item release failed (%s)",
                type(exc).__name__,
            )

    def store(self, service: str, key: str, value: str) -> None:
        if not value:
            raise KeystoreError("refusing to store an empty value")
        service_bytes, account_bytes = self._labels(service, key)
        password = _encode_secret(value)
        try:
            # A concurrent delete can land between duplicate detection and
            # lookup/modify. Retry the add path once so upsert remains useful
            # without turning a persistent authorization failure into a loop.
            for attempt in range(2):
                added_item: object | None = None
                try:
                    status, added_item = self._api.add_generic_password(
                        service_bytes,
                        account_bytes,
                        password,
                    )
                    if status == _ERR_SEC_SUCCESS:
                        return
                    if status != _ERR_SEC_DUPLICATE_ITEM:
                        _raise_macos_status("store", status)
                finally:
                    self._release(added_item)

                # SecKeychainAddGenericPassword has no upsert flag. Resolve
                # the existing item, then replace only its data in-place.
                existing_item: object | None = None
                status, existing_item = self._api.find_item(
                    service_bytes,
                    account_bytes,
                )
                try:
                    if status == _ERR_SEC_ITEM_NOT_FOUND and attempt == 0:
                        continue
                    if status != _ERR_SEC_SUCCESS:
                        _raise_macos_status("update lookup", status)
                    if existing_item is None:
                        raise KeystoreError(
                            "macOS Keychain update lookup returned no item"
                        )
                    status = self._api.modify_item(existing_item, password)
                    if (
                        status in (_ERR_SEC_ITEM_NOT_FOUND, _ERR_SEC_INVALID_ITEM_REF)
                        and attempt == 0
                    ):
                        continue
                    if status != _ERR_SEC_SUCCESS:
                        _raise_macos_status("update", status)
                    return
                finally:
                    self._release(existing_item)
        finally:
            _zero_bytearray(password)

    def fetch(self, service: str, key: str) -> str | None:
        """Read through the ordinary, potentially interactive Settings path."""

        service_bytes, account_bytes = self._labels(service, key)
        status, password = self._api.copy_password(
            service_bytes,
            account_bytes,
        )
        return self._decode_copied_password(
            status,
            password,
            operation="fetch",
        )

    def fetch_noninteractive(self, service: str, key: str) -> str | None:
        """Read without ever allowing macOS to show authentication UI."""

        service_bytes, account_bytes = self._labels(service, key)
        status, password = self._run_noninteractive_call(
            "non-interactive fetch",
            lambda: self._api.copy_password_noninteractive(
                service_bytes,
                account_bytes,
            ),
            discard=self._discard_password_copy,
        )
        return self._decode_copied_password(
            status,
            password,
            operation="non-interactive fetch",
        )

    @staticmethod
    def _decode_copied_password(
        status: int,
        password: bytearray | None,
        *,
        operation: str,
    ) -> str | None:
        if status == _ERR_SEC_ITEM_NOT_FOUND:
            if password is not None:
                _zero_bytearray(password)
            return None
        if status != _ERR_SEC_SUCCESS:
            if password is not None:
                _zero_bytearray(password)
            _raise_macos_status(operation, status)
        if password is None:
            raise KeystoreError("macOS Keychain fetch returned no password buffer")
        try:
            try:
                return password.decode("utf-8")
            except UnicodeDecodeError:
                raise KeystoreError(
                    "macOS Keychain password is not valid UTF-8 text"
                ) from None
        finally:
            _zero_bytearray(password)

    def delete(self, service: str, key: str) -> None:
        service_bytes, account_bytes = self._labels(service, key)
        status, item = self._api.find_item(service_bytes, account_bytes)
        try:
            if status == _ERR_SEC_ITEM_NOT_FOUND:
                return
            if status != _ERR_SEC_SUCCESS:
                _raise_macos_status("delete lookup", status)
            if item is None:
                raise KeystoreError("macOS Keychain delete lookup returned no item")
            status = self._api.delete_item(item)
            # Preserve idempotency if another process removed it after find.
            if status not in (
                _ERR_SEC_SUCCESS,
                _ERR_SEC_ITEM_NOT_FOUND,
                _ERR_SEC_INVALID_ITEM_REF,
            ):
                _raise_macos_status("delete", status)
        finally:
            self._release(item)

    def delete_noninteractive(self, service: str, key: str) -> None:
        """Delete without allowing an ACL/authentication dialog to appear."""

        service_bytes, account_bytes = self._labels(service, key)
        status = self._run_noninteractive_call(
            "non-interactive delete",
            lambda: self._api.delete_generic_password_noninteractive(
                service_bytes,
                account_bytes,
            ),
        )
        if status in (_ERR_SEC_SUCCESS, _ERR_SEC_ITEM_NOT_FOUND):
            return
        _raise_macos_status("non-interactive delete", status)


# ---------------------------------------------------------------------------
# Linux libsecret backend
# ---------------------------------------------------------------------------


class LinuxSecretToolKeystore(Keystore):
    """Linux libsecret backend via the ``secret-tool`` CLI.

    Entries are keyed by two attributes: ``service`` and ``key``,
    matching the macOS namespace shape so the same
    `(service, key)` tuple looks up the same secret regardless
    of host OS.
    """

    name = "linux-secret-tool"
    persistent = True

    def __init__(self, *, runner: _Subprocess | None = None) -> None:
        self._runner = runner or _DEFAULT_SUBPROCESS

    @classmethod
    def available(cls) -> bool:
        return sys.platform.startswith("linux") and shutil.which("secret-tool") is not None

    def store(self, service: str, key: str, value: str) -> None:
        if not value:
            raise KeystoreError("refusing to store an empty value")
        # `secret-tool store` reads the value from stdin so it
        # doesn't appear in `ps`.
        proc = self._runner.run(
            [
                "secret-tool", "store",
                "--label", f"CARE secret: {service}/{key}",
                "service", service,
                "key", key,
            ],
            input=value,
        )
        if proc.returncode != 0:
            raise KeystoreError(
                f"secret-tool store failed (rc={proc.returncode}): "
                f"{(proc.stderr or '').strip()}"
            )

    def fetch(self, service: str, key: str) -> str | None:
        proc = self._runner.run(
            [
                "secret-tool", "lookup",
                "service", service,
                "key", key,
            ],
        )
        # `secret-tool lookup` returns 1 + empty stdout on miss.
        if proc.returncode == 1 and not (proc.stdout or "").strip():
            return None
        if proc.returncode != 0:
            raise KeystoreError(
                f"secret-tool lookup failed (rc={proc.returncode}): "
                f"{(proc.stderr or '').strip()}"
            )
        return (proc.stdout or "").rstrip("\n") or None

    def delete(self, service: str, key: str) -> None:
        proc = self._runner.run(
            [
                "secret-tool", "clear",
                "service", service,
                "key", key,
            ],
        )
        # `secret-tool clear` returns 0 even on miss — already
        # idempotent.
        if proc.returncode != 0:
            raise KeystoreError(
                f"secret-tool clear failed (rc={proc.returncode}): "
                f"{(proc.stderr or '').strip()}"
            )


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------


def detect_keystore(
    *,
    file_path: Path | None = None,
    prefer: list[type[Keystore]] | None = None,
) -> Keystore:
    """Pick the best available backend for the host.

    Order:

    * Caller-provided ``prefer`` list (each class is probed via
      :meth:`Keystore.available`).
    * macOS Keychain — when running on Darwin and the system
      Security.framework can be loaded.
    * Linux ``secret-tool`` — when running on Linux with
      ``secret-tool`` on PATH.
    * File fallback — always usable.

    The file fallback path is configurable for tests / multi-
    profile setups via ``file_path``.

    Surfaces a one-line log entry at WARNING when falling back
    to the file backend so the missing-backend signal lands in
    the app log even if the caller forgets to toast.
    """
    candidates: list[type[Keystore]] = list(prefer or [])
    if os.name == "nt":
        from care.runtime.windows_keystore import WindowsCredentialManagerKeystore

        candidates.append(WindowsCredentialManagerKeystore)
    candidates.extend([MacOSKeychainKeystore, LinuxSecretToolKeystore])
    for cls in candidates:
        try:
            if cls.available():
                return cls()
        except Exception as exc:  # noqa: BLE001
            _log.warning(
                "keystore backend %s probe failed: %s",
                getattr(cls, "name", cls.__name__), exc,
            )
    _log.warning(
        "no OS-native keystore available; falling back to "
        "FileKeystore at %s — secrets will be stored in plain "
        "JSON",
        file_path or DEFAULT_FILE_KEYSTORE_PATH,
    )
    return FileKeystore(path=file_path)


# ---------------------------------------------------------------------------
# Public read / write helpers
# ---------------------------------------------------------------------------


def resolve_secret(
    raw: str | None,
    *,
    keystore: Keystore | None = None,
) -> str | None:
    """Resolve a config value to its concrete secret.

    Three cases:

    * ``raw is None`` — return ``None`` (matches "no secret
      configured").
    * ``raw`` is a keystore URL — dereference through
      ``keystore`` (auto-detected when unset). Returns the
      stored secret or ``None`` when the entry doesn't exist.
    * ``raw`` is any other string — return it verbatim
      (literal value already in ``config.toml``).

    Designed to be the single point of dereference for every
    ``CareConfig.*_api_key`` field. Raises
    :class:`KeystoreError` only when the URL is malformed; a
    missing-entry case is intentionally not an error so a
    user who deletes the keychain entry sees an empty
    config-level api_key + the friendly "not configured"
    toast instead of a crash.
    """
    if raw is None:
        return None
    if not is_keystore_url(raw):
        return raw
    ks = keystore or detect_keystore()
    service, key = parse_keystore_url(raw)
    return ks.fetch(service, key)


def resolve_secret_noninteractive(
    raw: str | None,
    *,
    keystore: Keystore | None = None,
) -> str | None:
    """Resolve a secret while suppressing per-read macOS Keychain UI.

    Literal values and non-macOS backends retain the behavior of
    :func:`resolve_secret`. The native macOS backend uses a SecItem query with
    ``kSecUseAuthenticationUIFail``; an ACL-protected item therefore raises
    :class:`KeystoreInteractionRequiredError` instead of opening a dialog.
    """

    if raw is None:
        return None
    if not is_keystore_url(raw):
        return raw
    ks = keystore or detect_keystore()
    service, key = parse_keystore_url(raw)
    fetch_noninteractive = getattr(ks, "fetch_noninteractive", None)
    if callable(fetch_noninteractive):
        return fetch_noninteractive(service, key)
    return ks.fetch(service, key)


def store_secret(
    value: str,
    *,
    key: str,
    service: str = DEFAULT_SERVICE,
    keystore: Keystore | None = None,
) -> str:
    """Persist ``value`` under ``service`` + ``key`` and return
    the canonical keystore URL the caller writes to
    ``config.toml``. Auto-detects the backend when ``keystore``
    is unset.

    Reverse of :func:`resolve_secret`: caller passes the
    literal value the user typed in the wizard, we store it +
    hand back a URL the loader will dereference on next boot.
    """
    if not value:
        raise KeystoreError("refusing to store an empty value")
    ks = keystore or detect_keystore()
    ks.store(service, key, value)
    return make_keystore_url(service, key)


__all__ = [
    "DEFAULT_FILE_KEYSTORE_PATH",
    "DEFAULT_SERVICE",
    "FileKeystore",
    "KEYSTORE_URL_SCHEME",
    "Keystore",
    "KeystoreAccessTimeoutError",
    "KeystoreError",
    "KeystoreInteractionRequiredError",
    "LinuxSecretToolKeystore",
    "MacOSKeychainKeystore",
    "MemoryKeystore",
    "detect_keystore",
    "disable_macos_keychain_interaction",
    "is_keystore_url",
    "make_keystore_url",
    "parse_keystore_url",
    "resolve_secret",
    "resolve_secret_noninteractive",
    "store_secret",
]
