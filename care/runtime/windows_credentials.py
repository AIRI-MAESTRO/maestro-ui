"""Windows credential IPC authenticated against kernel TCP/process ownership.

Only the exact resident MCP helper, launched directly by this session's live
Harness process, may bootstrap or resolve credentials. The child checks the
server against its native-host grandparent. Ports/PIDs are public coordinates;
capabilities and credentials travel only over the authenticated loopback stream.
"""

from __future__ import annotations

import os
import socket
import threading
import time
from pathlib import Path

import psutil

from care.runtime.credential_broker import (
    _MAX_BOOTSTRAP_BYTES,
    _MAX_REQUEST_BYTES,
    BROKER_PROTOCOL_VERSION,
    HarnessCredentialBrokerBootstrap,
    HarnessCredentialBrokerError,
    HarnessCredentialBrokerServer,
    _recv_frame,
    _send_frame,
)

BROKER_PORT_ENV = "MAESTRO_HARNESS_CREDENTIAL_BROKER_PORT"
BROKER_PID_ENV = "MAESTRO_HARNESS_CREDENTIAL_BROKER_PID"
MCP_ARGUMENTS = ["-P", "-s", "-m", "care.harness_mcp"]


def connection_pid(local: tuple[str, int], remote: tuple[str, int]) -> int:
    """Resolve one established TCP endpoint through Windows' owner-PID table."""
    deadline = time.monotonic() + 0.5
    while True:
        matches = {
            item.pid
            for item in psutil.net_connections(kind="tcp4")
            if tuple(item.laddr) == local
            and tuple(item.raddr) == remote
            and item.status == psutil.CONN_ESTABLISHED
            and item.pid is not None
        }
        if len(matches) == 1:
            return matches.pop()
        if time.monotonic() >= deadline:
            raise HarnessCredentialBrokerError("credential peer identity unavailable")
        time.sleep(0.01)


def validate_client(pid: int, harness: psutil.Process, helper: Path) -> None:
    try:
        child = psutil.Process(pid)
        if (
            not harness.is_running()
            or child.ppid() != harness.pid
            or child.create_time() < harness.create_time()
            or os.path.normcase(child.exe()) != os.path.normcase(str(helper))
            or child.cmdline()[1:] != MCP_ARGUMENTS
        ):
            raise HarnessCredentialBrokerError("credential broker peer is not trusted")
    except psutil.Error:
        raise HarnessCredentialBrokerError(
            "credential broker peer is not trusted"
        ) from None


class WindowsCredentialBrokerServer(HarnessCredentialBrokerServer):
    def __init__(self, *, session_id: str, provider, helper: Path) -> None:
        super().__init__(session_id=session_id, provider=provider)
        self._helper = helper.resolve()
        self._harness: psutil.Process | None = None
        self._bound = threading.Event()
        self._port = 0

    @property
    def bootstrap(self) -> HarnessCredentialBrokerBootstrap:
        return HarnessCredentialBrokerBootstrap(
            socket_path=Path("windows-loopback"),
            session_id=self._session_id,
            token=self._token,
            tcp_port=self._port,
            host_pid=os.getpid(),
        )

    def start(self) -> Path:
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            listener.bind(("127.0.0.1", 0))
            listener.listen(8)
            listener.settimeout(0.2)
        except BaseException:
            listener.close()
            raise
        self._port = listener.getsockname()[1]
        self._listener = listener
        self._socket_path = Path("windows-loopback")
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._serve, daemon=True, name="maestro-windows-credential-broker"
        )
        self._thread.start()
        return self._socket_path

    def bind_harness(self, process) -> None:
        if process.poll() is not None:
            raise HarnessCredentialBrokerError("resident Harness exited before handoff")
        self._harness = psutil.Process(process.pid)
        # Cache the creation time now; is_running also guards against PID reuse.
        self._harness.create_time()
        self._bound.set()

    def _handle(self, connection: socket.socket) -> None:
        if not self._bound.wait(timeout=3) or self._harness is None:
            raise HarnessCredentialBrokerError("resident Harness identity unavailable")
        try:
            pid = connection_pid(connection.getpeername(), connection.getsockname())
            validate_client(pid, self._harness, self._helper)
        except (OSError, psutil.Error):
            raise HarnessCredentialBrokerError(
                "credential broker peer is not trusted"
            ) from None
        request = _recv_frame(connection, maximum=_MAX_REQUEST_BYTES)
        if request == {"version": BROKER_PROTOCOL_VERSION, "operation": "bootstrap"}:
            _send_frame(
                connection,
                {
                    "version": BROKER_PROTOCOL_VERSION,
                    "session_id": self._session_id,
                    "token": self._token,
                    "tcp_port": self._port,
                    "host_pid": os.getpid(),
                },
                maximum=_MAX_BOOTSTRAP_BYTES,
            )
        else:
            self._resolve_request(connection, request)


def connect_to_host(port: int, host_pid: int, *, timeout: float) -> socket.socket:
    connection = socket.create_connection(("127.0.0.1", port), timeout=timeout)
    try:
        own = psutil.Process()
        harness = own.parent()
        native = harness.parent() if harness is not None else None
        if (
            native is None
            or native.pid != host_pid
            or not native.is_running()
            or connection_pid(connection.getpeername(), connection.getsockname())
            != host_pid
        ):
            raise HarnessCredentialBrokerError(
                "credential broker server is not trusted"
            )
        return connection
    except BaseException:
        connection.close()
        raise


def peek_windows_bootstrap() -> HarnessCredentialBrokerBootstrap | None:
    raw_port = os.environ.get(BROKER_PORT_ENV)
    if not raw_port:
        return None
    try:
        port = int(raw_port)
        host_pid = int(os.environ[BROKER_PID_ENV])
        if not 1 <= port <= 65535 or host_pid <= 0:
            raise ValueError
        with connect_to_host(port, host_pid, timeout=3) as connection:
            _send_frame(
                connection,
                {"version": BROKER_PROTOCOL_VERSION, "operation": "bootstrap"},
                maximum=_MAX_REQUEST_BYTES,
            )
            response = _recv_frame(connection, maximum=_MAX_BOOTSTRAP_BYTES)
        if (
            response.get("version") != BROKER_PROTOCOL_VERSION
            or response.get("tcp_port") != port
            or response.get("host_pid") != host_pid
            or not isinstance(response.get("session_id"), str)
            or not response["session_id"]
            or not isinstance(response.get("token"), str)
            or len(response["token"]) < 32
        ):
            raise ValueError
        return HarnessCredentialBrokerBootstrap(
            socket_path=Path("windows-loopback"),
            session_id=response["session_id"],
            token=response["token"],
            tcp_port=port,
            host_pid=host_pid,
        )
    except (OSError, ValueError, KeyError, psutil.Error):
        raise HarnessCredentialBrokerError(
            "private credential handoff was invalid"
        ) from None
