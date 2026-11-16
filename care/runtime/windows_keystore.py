"""Current-user Windows Credential Manager; secrets never enter subprocesses."""

import ctypes
import json
import os
from ctypes import wintypes

from care.runtime.keystore import Keystore, KeystoreError


class _Credential(ctypes.Structure):
    _fields_ = [
        ("Flags", wintypes.DWORD),
        ("Type", wintypes.DWORD),
        ("TargetName", wintypes.LPWSTR),
        ("Comment", wintypes.LPWSTR),
        ("LastWritten", wintypes.FILETIME),
        ("CredentialBlobSize", wintypes.DWORD),
        ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)),
        ("Persist", wintypes.DWORD),
        ("AttributeCount", wintypes.DWORD),
        ("Attributes", ctypes.c_void_p),
        ("TargetAlias", wintypes.LPWSTR),
        ("UserName", wintypes.LPWSTR),
    ]


class WindowsCredentialManagerKeystore(Keystore):
    name = "windows-credential-manager"
    persistent = True

    @classmethod
    def available(cls) -> bool:
        return os.name == "nt"

    def __init__(self) -> None:
        self._api = ctypes.WinDLL("advapi32", use_last_error=True)
        pointer = ctypes.POINTER(_Credential)
        self._api.CredWriteW.argtypes = [pointer, wintypes.DWORD]
        self._api.CredWriteW.restype = wintypes.BOOL
        self._api.CredReadW.argtypes = [
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            ctypes.POINTER(pointer),
        ]
        self._api.CredReadW.restype = wintypes.BOOL
        self._api.CredDeleteW.argtypes = [
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
        ]
        self._api.CredDeleteW.restype = wintypes.BOOL
        self._api.CredFree.argtypes = [ctypes.c_void_p]
        self._api.CredFree.restype = None

    @staticmethod
    def _target(service: str, key: str) -> str:
        return "MAESTRO/" + json.dumps(
            [service, key], ensure_ascii=True, separators=(",", ":")
        )

    def store(self, service: str, key: str, value: str) -> None:
        body = value.encode("utf-8")
        if not body or len(body) > 2560:
            raise KeystoreError("Windows credential value has an invalid size")
        buffer = (ctypes.c_ubyte * len(body)).from_buffer_copy(body)
        credential = _Credential(
            Type=1,
            TargetName=self._target(service, key),
            CredentialBlobSize=len(body),
            CredentialBlob=buffer,
            Persist=2,
            UserName="MAESTRO",
        )
        try:
            if not self._api.CredWriteW(ctypes.byref(credential), 0):
                raise KeystoreError("Windows credential storage failed")
        finally:
            ctypes.memset(buffer, 0, len(body))

    def fetch(self, service: str, key: str) -> str | None:
        credential = ctypes.POINTER(_Credential)()
        if not self._api.CredReadW(
            self._target(service, key), 1, 0, ctypes.byref(credential)
        ):
            if ctypes.get_last_error() == 1168:
                return None
            raise KeystoreError("Windows credential lookup failed")
        try:
            data = credential.contents
            if data.CredentialBlobSize > 2560:
                raise KeystoreError("Windows credential size is invalid")
            return (
                ctypes.string_at(data.CredentialBlob, data.CredentialBlobSize).decode(
                    "utf-8"
                )
                or None
            )
        finally:
            self._api.CredFree(credential)

    fetch_noninteractive = fetch

    def delete(self, service: str, key: str) -> None:
        if not self._api.CredDeleteW(self._target(service, key), 1, 0):
            if ctypes.get_last_error() != 1168:
                raise KeystoreError("Windows credential deletion failed")
