"""Private, session-bound credential handoff for the resident Harness.

The native Maestro process is the only process allowed to talk to the operating
system keystore interactively.  DeepSeek Harness starts the MAESTRO MCP server
as a separate Python process, whose ad-hoc code identity is intentionally not
added to every Keychain item ACL.  This module bridges that process boundary
without placing provider secrets in argv, environment variables, or files.

Only the eight credential slots already admitted by :mod:`care.config` can be
requested.  The broker serves one slot per connection over an owner-only Unix
socket, validates the peer uid, and never returns arbitrary Keychain entries.
"""

from __future__ import annotations

import ctypes
from dataclasses import dataclass
import hmac
import json
import logging
import os
from pathlib import Path
import secrets
import socket
import stat
import struct
import sys
import tempfile
import threading
from collections.abc import Callable, Mapping
from typing import Final, Protocol

from care.config import CareConfig, _API_KEY_SLOTS
from care.runtime.keystore import (
    Keystore,
    KeystoreAccessTimeoutError,
    KeystoreInteractionRequiredError,
    detect_keystore,
    is_keystore_url,
    resolve_secret_noninteractive,
)


BROKER_BOOTSTRAP_FD_ENV: Final = "MAESTRO_HARNESS_CREDENTIAL_BOOTSTRAP_FD"
BROKER_PROTOCOL_VERSION: Final = 1
CARE_CREDENTIAL_SLOTS: Final = tuple(
    f"{parent}.{field}" for parent, field in _API_KEY_SLOTS
)
CARE_CREDENTIAL_ENV_NAMES: Final = tuple(
    f"CARE_{parent.upper()}__{field.upper()}"
    for parent, field in _API_KEY_SLOTS
)

_SLOT_PATHS: Final = {
    f"{parent}.{field}": (parent, field)
    for parent, field in _API_KEY_SLOTS
}
_MAX_REQUEST_BYTES: Final = 2_048
_MAX_RESPONSE_BYTES: Final = 65_536
_MAX_SECRET_BYTES: Final = 32_768
_MAX_BOOTSTRAP_BYTES: Final = 4_096
_UNIX_PATH_LIMIT: Final = 103
_BOOTSTRAP_MAGIC: Final = b"MAESTRO-CREDENTIAL-BROKER\x01"
_log = logging.getLogger("care.credential_broker")


class HarnessCredentialBrokerError(RuntimeError):
    """A credential handoff failed without exposing secret material."""


class CredentialProvider(Protocol):
    def __call__(self, slot: str) -> str | None: ...


@dataclass(frozen=True, slots=True, repr=False)
class HarnessCredentialBrokerBootstrap:
    """In-memory capability delivered only through an inherited socket."""

    socket_path: Path
    session_id: str
    token: str
    tcp_port: int | None = None
    host_pid: int | None = None

    def __repr__(self) -> str:
        return "HarnessCredentialBrokerBootstrap(<redacted>)"


class HarnessCredentialResolver:
    """Resolve an allow-listed raw config slot inside the native process.

    Successful and failed resolutions are cached for the application's
    lifetime.  A denied/cancelled Keychain interaction blocks later Keychain
    references in the same process, preventing a row of identical dialogs;
    literal values remain available.
    """

    def __init__(
        self,
        config: CareConfig,
        *,
        keystore_factory: Callable[[], Keystore] | None = None,
    ) -> None:
        self._config = config
        self._keystore_factory = keystore_factory or detect_keystore
        self._keystore: Keystore | None = None
        self._cache: dict[str, str | None] = {}
        self._interaction_blocked = False
        self._lock = threading.RLock()

    def __call__(self, slot: str) -> str | None:
        if slot not in _SLOT_PATHS:
            raise HarnessCredentialBrokerError("credential slot is not allowed")
        with self._lock:
            if slot in self._cache:
                return self._cache[slot]

            parent_name, field_name = _SLOT_PATHS[slot]
            parent = getattr(self._config, parent_name, None)
            raw = getattr(parent, field_name, None) if parent is not None else None
            if not isinstance(raw, str) or not raw:
                self._cache[slot] = None
                return None
            if not is_keystore_url(raw):
                self._cache[slot] = raw
                return raw
            if self._interaction_blocked:
                self._cache[slot] = None
                return None

            try:
                if self._keystore is None:
                    self._keystore = self._keystore_factory()
                value = resolve_secret_noninteractive(
                    raw,
                    keystore=self._keystore,
                )
            except (
                KeystoreAccessTimeoutError,
                KeystoreInteractionRequiredError,
            ):
                self._interaction_blocked = True
                self._cache[slot] = None
                _log.warning(
                    "native Keychain access unavailable for %s; "
                    "remaining Keychain-backed credentials will stay unavailable",
                    slot,
                )
                return None
            except Exception as exc:  # noqa: BLE001 - never forward backend detail
                self._cache[slot] = None
                _log.warning(
                    "native credential resolution failed for %s (%s)",
                    slot,
                    type(exc).__name__,
                )
                return None

            if not isinstance(value, str) or not value:
                value = None
            self._cache[slot] = value
            return value

    def close(self) -> None:
        """Drop cached references when the native app shuts down."""

        with self._lock:
            self._cache.clear()
            self._keystore = None
            self._config = CareConfig()


def apply_harness_credentials(
    config: CareConfig,
    credentials: Mapping[str, str],
) -> None:
    """Hydrate only allow-listed credential fields on a validated config."""

    validated: list[tuple[tuple[str, str], str]] = []
    for slot, value in credentials.items():
        path = _SLOT_PATHS.get(slot)
        if path is None:
            raise HarnessCredentialBrokerError("credential response named an unknown slot")
        if not isinstance(value, str) or not value:
            raise HarnessCredentialBrokerError("credential response contained no value")
        validated.append((path, value))
    # Raw config loading deliberately preserves references and plaintext legacy
    # values so the main process can resolve precedence. Never leave either in
    # the child: an unavailable broker slot must be genuinely absent, not the
    # truthy string ``keystore://...`` handed to a provider by mistake.
    for parent_name, field_name in _API_KEY_SLOTS:
        setattr(getattr(config, parent_name), field_name, None)
    for (parent_name, field_name), value in validated:
        setattr(getattr(config, parent_name), field_name, value)


def _encode_frame(payload: Mapping[str, object], *, maximum: int) -> bytearray:
    try:
        body = bytearray(
            json.dumps(
                dict(payload),
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
        )
    except (TypeError, UnicodeError) as exc:
        raise HarnessCredentialBrokerError("credential broker payload is invalid") from exc
    if not body or len(body) > maximum:
        for index in range(len(body)):
            body[index] = 0
        raise HarnessCredentialBrokerError("credential broker payload size is invalid")
    return body


def _recv_exact(connection: socket.socket, size: int) -> bytes:
    chunks = bytearray()
    while len(chunks) < size:
        chunk = connection.recv(size - len(chunks))
        if not chunk:
            raise HarnessCredentialBrokerError("credential broker frame was truncated")
        chunks.extend(chunk)
    return bytes(chunks)


def _recv_frame(connection: socket.socket, *, maximum: int) -> dict[str, object]:
    header = _recv_exact(connection, 4)
    (size,) = struct.unpack("!I", header)
    if size <= 0 or size > maximum:
        raise HarnessCredentialBrokerError("credential broker frame size is invalid")
    body = bytearray(_recv_exact(connection, size))
    try:
        decoded = json.loads(body.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise HarnessCredentialBrokerError("credential broker frame is invalid") from exc
    finally:
        for index in range(len(body)):
            body[index] = 0
    if not isinstance(decoded, dict):
        raise HarnessCredentialBrokerError("credential broker frame must be an object")
    return decoded


def _send_frame(
    connection: socket.socket,
    payload: Mapping[str, object],
    *,
    maximum: int,
) -> None:
    body = _encode_frame(payload, maximum=maximum)
    try:
        connection.sendall(struct.pack("!I", len(body)))
        connection.sendall(body)
    finally:
        for index in range(len(body)):
            body[index] = 0


def encode_harness_credential_bootstrap(
    bootstrap: HarnessCredentialBrokerBootstrap,
) -> bytearray:
    """Encode the capability frame written before DeepSeek Harness starts."""

    body = _encode_frame(
        {
            "version": BROKER_PROTOCOL_VERSION,
            "socket_path": str(bootstrap.socket_path),
            "session_id": bootstrap.session_id,
            "token": bootstrap.token,
        },
        maximum=_MAX_BOOTSTRAP_BYTES - len(_BOOTSTRAP_MAGIC) - 4,
    )
    try:
        return bytearray(_BOOTSTRAP_MAGIC) + struct.pack("!I", len(body)) + body
    finally:
        for index in range(len(body)):
            body[index] = 0


def peek_harness_credential_bootstrap(
    fd: int = 2,
) -> HarnessCredentialBrokerBootstrap | None:
    """Peek a bootstrap capability without consuming it across MCP reconnects.

    The host preloads one end of a Unix ``socketpair`` before spawning DSH. The
    MCP transport maps the inherited descriptor onto each child's stderr. A
    non-consuming peek lets every replacement child recover the same capability
    while normal stderr writes continue in the opposite socket direction.
    """

    if os.name == "nt":
        from care.runtime.windows_credentials import peek_windows_bootstrap

        return peek_windows_bootstrap()
    try:
        duplicate = os.dup(fd)
    except OSError:
        return None
    buffer = bytearray(_MAX_BOOTSTRAP_BYTES)
    try:
        try:
            channel = socket.socket(fileno=duplicate)
        except OSError:
            return None
        with channel:
            duplicate = -1
            channel.settimeout(0.25)
            try:
                received = channel.recv_into(buffer, _MAX_BOOTSTRAP_BYTES, socket.MSG_PEEK)
            except (OSError, TimeoutError):
                return None
        if received < len(_BOOTSTRAP_MAGIC) + 4:
            return None
        view = memoryview(buffer)[:received]
        if bytes(view[: len(_BOOTSTRAP_MAGIC)]) != _BOOTSTRAP_MAGIC:
            return None
        offset = len(_BOOTSTRAP_MAGIC)
        (size,) = struct.unpack("!I", view[offset : offset + 4])
        body_start = offset + 4
        body_end = body_start + size
        if size <= 0 or body_end > received:
            raise HarnessCredentialBrokerError(
                "credential broker bootstrap frame is incomplete"
            )
        try:
            decoded = json.loads(bytes(view[body_start:body_end]).decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise HarnessCredentialBrokerError(
                "credential broker bootstrap frame is invalid"
            ) from exc
        if not isinstance(decoded, dict):
            raise HarnessCredentialBrokerError(
                "credential broker bootstrap frame must be an object"
            )
        socket_path = decoded.get("socket_path")
        session_id = decoded.get("session_id")
        token = decoded.get("token")
        if (
            decoded.get("version") != BROKER_PROTOCOL_VERSION
            or not isinstance(socket_path, str)
            or not socket_path
            or not isinstance(session_id, str)
            or not session_id
            or not isinstance(token, str)
            or len(token) < 32
        ):
            raise HarnessCredentialBrokerError(
                "credential broker bootstrap frame was rejected"
            )
        return HarnessCredentialBrokerBootstrap(
            socket_path=Path(socket_path),
            session_id=session_id,
            token=token,
        )
    finally:
        if duplicate >= 0:
            os.close(duplicate)
        for index in range(len(buffer)):
            buffer[index] = 0


def _peer_uid(connection: socket.socket) -> int:
    if sys.platform == "darwin":
        libc = ctypes.CDLL(None, use_errno=True)
        getpeereid = libc.getpeereid
        getpeereid.argtypes = [
            ctypes.c_int,
            ctypes.POINTER(ctypes.c_uint),
            ctypes.POINTER(ctypes.c_uint),
        ]
        getpeereid.restype = ctypes.c_int
        uid = ctypes.c_uint()
        gid = ctypes.c_uint()
        if getpeereid(connection.fileno(), ctypes.byref(uid), ctypes.byref(gid)) != 0:
            raise HarnessCredentialBrokerError("could not authenticate broker peer")
        return int(uid.value)
    if hasattr(socket, "SO_PEERCRED"):
        raw = connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)
        _pid, uid, _gid = struct.unpack("3i", raw)
        return uid
    raise HarnessCredentialBrokerError("peer credentials are unavailable")


def _validate_socket_path(path: Path) -> None:
    if not path.is_absolute() or len(os.fsencode(path)) > _UNIX_PATH_LIMIT:
        raise HarnessCredentialBrokerError("credential broker socket path is invalid")
    try:
        parent_stat = path.parent.stat(follow_symlinks=False)
        socket_stat = path.stat(follow_symlinks=False)
    except OSError as exc:
        raise HarnessCredentialBrokerError(
            "credential broker socket is unavailable"
        ) from exc
    if (
        not stat.S_ISDIR(parent_stat.st_mode)
        or parent_stat.st_uid != os.getuid()
        or parent_stat.st_mode & 0o077
        or not stat.S_ISSOCK(socket_stat.st_mode)
        or socket_stat.st_uid != os.getuid()
        or socket_stat.st_mode & 0o177
    ):
        raise HarnessCredentialBrokerError("credential broker socket is not private")


class HarnessCredentialBrokerServer:
    """Small owner-only Unix-socket server owned by the native process."""

    def __init__(
        self,
        *,
        session_id: str,
        provider: CredentialProvider,
        socket_root: str | os.PathLike[str] | None = None,
    ) -> None:
        if not session_id or len(session_id) > 128:
            raise ValueError("credential broker session id is invalid")
        self._session_id = session_id
        self._provider = provider
        self._socket_root = (
            Path("/private/tmp")
            if socket_root is None and sys.platform == "darwin"
            else Path(tempfile.gettempdir() if socket_root is None else socket_root)
        ).resolve()
        self._temporary: tempfile.TemporaryDirectory[str] | None = None
        self._listener: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._socket_path: Path | None = None
        self._token = secrets.token_urlsafe(32)
        self._connections: set[socket.socket] = set()
        self._connections_lock = threading.Lock()
        self._provider_close_lock = threading.Lock()
        self._provider_closed = False

    @property
    def socket_path(self) -> Path:
        if self._socket_path is None:
            raise HarnessCredentialBrokerError("credential broker is not running")
        return self._socket_path

    @property
    def bootstrap(self) -> HarnessCredentialBrokerBootstrap:
        return HarnessCredentialBrokerBootstrap(
            socket_path=self.socket_path,
            session_id=self._session_id,
            token=self._token,
        )

    def start(self) -> Path:
        if self._listener is not None:
            return self.socket_path
        temporary = tempfile.TemporaryDirectory(prefix="mcr-", dir=self._socket_root)
        directory = Path(temporary.name)
        listener: socket.socket | None = None
        try:
            os.chmod(directory, 0o700)
            path = directory / "c.sock"
            if len(os.fsencode(path)) > _UNIX_PATH_LIMIT:
                raise HarnessCredentialBrokerError(
                    "credential broker socket path is too long"
                )
            listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            listener.bind(str(path))
            os.chmod(path, 0o600)
            listener.listen(8)
            listener.settimeout(0.2)
        except BaseException:
            if listener is not None:
                listener.close()
            temporary.cleanup()
            raise
        self._temporary = temporary
        self._listener = listener
        self._socket_path = path
        self._stop.clear()
        thread = threading.Thread(
            target=self._serve,
            name="maestro-credential-broker",
            daemon=True,
        )
        self._thread = thread
        thread.start()
        return path

    def _serve(self) -> None:
        listener = self._listener
        if listener is None:  # pragma: no cover - guarded by start
            return
        try:
            while not self._stop.is_set():
                try:
                    connection, _address = listener.accept()
                except TimeoutError:
                    continue
                except OSError:
                    if self._stop.is_set():
                        return
                    continue
                with self._connections_lock:
                    self._connections.add(connection)
                try:
                    with connection:
                        connection.settimeout(3.0)
                        try:
                            self._handle(connection)
                        except Exception as exc:  # noqa: BLE001 - protocol stays redacted
                            _log.warning(
                                "credential broker rejected a request (%s)",
                                type(exc).__name__,
                            )
                finally:
                    with self._connections_lock:
                        self._connections.discard(connection)
        finally:
            if self._stop.is_set():
                self._close_provider()

    def _close_provider(self) -> None:
        with self._provider_close_lock:
            if self._provider_closed:
                return
            self._provider_closed = True
        close = getattr(self._provider, "close", None)
        if callable(close):
            try:
                close()
            except Exception as exc:  # noqa: BLE001 - teardown is best-effort
                _log.warning(
                    "credential provider cleanup failed (%s)",
                    type(exc).__name__,
                )

    def _handle(self, connection: socket.socket) -> None:
        if _peer_uid(connection) != os.getuid():
            raise HarnessCredentialBrokerError("credential broker peer is not trusted")
        request = _recv_frame(connection, maximum=_MAX_REQUEST_BYTES)
        self._resolve_request(connection, request)

    def _resolve_request(self, connection: socket.socket, request: dict) -> None:
        if (
            request.get("version") != BROKER_PROTOCOL_VERSION
            or request.get("operation") != "resolve"
            or request.get("session_id") != self._session_id
        ):
            raise HarnessCredentialBrokerError("credential broker request was rejected")
        token = request.get("token")
        if not isinstance(token, str) or not hmac.compare_digest(token, self._token):
            raise HarnessCredentialBrokerError("credential broker capability was rejected")
        slot = request.get("slot")
        if not isinstance(slot, str) or slot not in _SLOT_PATHS:
            raise HarnessCredentialBrokerError("credential broker slot was rejected")
        try:
            value = self._provider(slot)
        except Exception as exc:  # noqa: BLE001 - provider details stay native-only
            _log.warning(
                "credential provider failed for %s (%s)",
                slot,
                type(exc).__name__,
            )
            value = None
        if value is None:
            response: dict[str, object] = {
                "version": BROKER_PROTOCOL_VERSION,
                "status": "unavailable",
            }
        elif not isinstance(value, str) or not value:
            response = {
                "version": BROKER_PROTOCOL_VERSION,
                "status": "unavailable",
            }
        elif len(value.encode("utf-8")) > _MAX_SECRET_BYTES:
            response = {
                "version": BROKER_PROTOCOL_VERSION,
                "status": "unavailable",
            }
        else:
            response = {
                "version": BROKER_PROTOCOL_VERSION,
                "status": "ok",
                "value": value,
            }
        _send_frame(connection, response, maximum=_MAX_RESPONSE_BYTES)

    def close(self) -> None:
        self._stop.set()
        listener = self._listener
        thread = self._thread
        temporary = self._temporary
        self._listener = None
        self._thread = None
        self._temporary = None
        self._socket_path = None
        self._token = ""
        if listener is not None:
            try:
                listener.close()
            except OSError:
                pass
        with self._connections_lock:
            active_connections = tuple(self._connections)
        for connection in active_connections:
            try:
                connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=1.0)
        if temporary is not None:
            temporary.cleanup()
        if thread is None or not thread.is_alive():
            self._close_provider()


def fetch_harness_credential(
    bootstrap: HarnessCredentialBrokerBootstrap,
    *,
    slot: str,
    timeout: float = 3.0,
) -> str | None:
    """Fetch one allow-listed slot from the native broker."""

    if slot not in _SLOT_PATHS:
        raise HarnessCredentialBrokerError("credential slot is not allowed")
    path = bootstrap.socket_path
    if bootstrap.tcp_port is None:
        _validate_socket_path(path)
    request = {
        "version": BROKER_PROTOCOL_VERSION,
        "operation": "resolve",
        "session_id": bootstrap.session_id,
        "token": bootstrap.token,
        "slot": slot,
    }
    try:
        if bootstrap.tcp_port is not None:
            from care.runtime.windows_credentials import connect_to_host

            connection = connect_to_host(bootstrap.tcp_port, bootstrap.host_pid, timeout=timeout)
        else:
            connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            connection.settimeout(timeout)
            connection.connect(str(path))
        with connection:
            _send_frame(connection, request, maximum=_MAX_REQUEST_BYTES)
            response = _recv_frame(connection, maximum=_MAX_RESPONSE_BYTES)
    except HarnessCredentialBrokerError:
        raise
    except (OSError, TimeoutError) as exc:
        raise HarnessCredentialBrokerError(
            "native credential broker is unavailable"
        ) from exc
    if response.get("version") != BROKER_PROTOCOL_VERSION:
        raise HarnessCredentialBrokerError("credential broker response is invalid")
    status = response.get("status")
    if status == "unavailable":
        return None
    value = response.get("value")
    if status != "ok" or not isinstance(value, str) or not value:
        raise HarnessCredentialBrokerError("credential broker response is invalid")
    if len(value.encode("utf-8")) > _MAX_SECRET_BYTES:
        raise HarnessCredentialBrokerError("credential broker response is too large")
    return value


__all__ = [
    "BROKER_BOOTSTRAP_FD_ENV",
    "BROKER_PROTOCOL_VERSION",
    "CARE_CREDENTIAL_ENV_NAMES",
    "CARE_CREDENTIAL_SLOTS",
    "CredentialProvider",
    "HarnessCredentialBrokerError",
    "HarnessCredentialBrokerBootstrap",
    "HarnessCredentialBrokerServer",
    "HarnessCredentialResolver",
    "apply_harness_credentials",
    "encode_harness_credential_bootstrap",
    "fetch_harness_credential",
    "peek_harness_credential_bootstrap",
]
