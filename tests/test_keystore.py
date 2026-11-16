"""Tests for `care.runtime.keystore` (TODO §1 P0).

Covers every public surface:

* URL helpers — round-trip + rejection of malformed inputs.
* In-memory + file backend — full CRD semantics.
* macOS Keychain backend — exercises mocked Security.framework
  calls (no real Keychain access and no child process).
* Linux `secret-tool` backend — same stub-runner pattern.
* `detect_keystore` — preference order, fallback to file.
* `resolve_secret` / `store_secret` — the public read/write
  helpers `CareConfig` will use.
"""

from __future__ import annotations

import ctypes
import json
import os
import stat
import subprocess
import threading
import time
from dataclasses import dataclass

import pytest

import care.runtime.keystore as keystore_module
from care.runtime.keystore import (
    DEFAULT_FILE_KEYSTORE_PATH,
    DEFAULT_SERVICE,
    FileKeystore,
    KEYSTORE_URL_SCHEME,
    Keystore,
    KeystoreAccessTimeoutError,
    KeystoreError,
    KeystoreInteractionRequiredError,
    LinuxSecretToolKeystore,
    MacOSKeychainKeystore,
    MemoryKeystore,
    detect_keystore,
    is_keystore_url,
    make_keystore_url,
    parse_keystore_url,
    resolve_secret,
    resolve_secret_noninteractive,
    store_secret,
)


# ---------------------------------------------------------------------------
# Fake subprocess runner
# ---------------------------------------------------------------------------


@dataclass
class _FakeProc:
    """Minimal stand-in for `subprocess.CompletedProcess[str]`."""

    returncode: int
    stdout: str = ""
    stderr: str = ""


class _Recorder:
    """Pluggable subprocess runner — records every invocation +
    returns canned responses keyed by the first two args
    (binary + verb). Lets the macOS / Linux backend tests
    exercise the real argv shape without touching the host's
    keychain."""

    def __init__(self) -> None:
        self.calls: list[tuple[list[str], str | None]] = []
        self._responses: list[_FakeProc] = []

    def push(self, *procs: _FakeProc) -> None:
        self._responses.extend(procs)

    def run(
        self,
        args: list[str],
        *,
        input: str | None = None,
        capture_output: bool = True,
        check: bool = False,
        timeout: float = 10.0,
    ) -> subprocess.CompletedProcess[str]:
        self.calls.append((args, input))
        if not self._responses:
            raise AssertionError(
                f"_Recorder ran out of canned responses; "
                f"called with {args!r}"
            )
        proc = self._responses.pop(0)
        # Build a real CompletedProcess so callers that introspect
        # type(proc) still see the right class.
        return subprocess.CompletedProcess(
            args=args,
            returncode=proc.returncode,
            stdout=proc.stdout,
            stderr=proc.stderr,
        )


# ---------------------------------------------------------------------------
# URL helpers
# ---------------------------------------------------------------------------


class TestURL:
    def test_is_keystore_url(self):
        assert is_keystore_url("keystore://care/mage.api_key")
        assert not is_keystore_url("sk-abc")
        assert not is_keystore_url(None)
        assert not is_keystore_url(42)
        assert not is_keystore_url("")

    def test_round_trip(self):
        url = make_keystore_url("care", "mage.api_key")
        assert url == "keystore://care/mage.api_key"
        assert parse_keystore_url(url) == ("care", "mage.api_key")

    def test_make_rejects_empty_or_slashed(self):
        with pytest.raises(KeystoreError):
            make_keystore_url("", "key")
        with pytest.raises(KeystoreError):
            make_keystore_url("care", "")
        with pytest.raises(KeystoreError):
            make_keystore_url("care", "with/slash")

    def test_parse_rejects_bad_shape(self):
        with pytest.raises(KeystoreError):
            parse_keystore_url("sk-abc")
        with pytest.raises(KeystoreError):
            parse_keystore_url("keystore://care")   # no key half
        with pytest.raises(KeystoreError):
            parse_keystore_url("keystore:///key")   # empty service
        with pytest.raises(KeystoreError):
            parse_keystore_url("keystore://care/")  # empty key

    def test_scheme_constant_matches_make(self):
        assert make_keystore_url("a", "b").startswith(KEYSTORE_URL_SCHEME)


# ---------------------------------------------------------------------------
# MemoryKeystore
# ---------------------------------------------------------------------------


class TestMemoryKeystore:
    def test_store_fetch_delete_roundtrip(self):
        ks = MemoryKeystore()
        assert ks.fetch("care", "k") is None
        ks.store("care", "k", "value-1")
        assert ks.fetch("care", "k") == "value-1"
        ks.store("care", "k", "value-2")
        assert ks.fetch("care", "k") == "value-2"
        ks.delete("care", "k")
        assert ks.fetch("care", "k") is None

    def test_delete_missing_is_idempotent(self):
        ks = MemoryKeystore()
        ks.delete("care", "missing")  # must not raise

    def test_empty_value_rejected(self):
        ks = MemoryKeystore()
        with pytest.raises(KeystoreError):
            ks.store("care", "k", "")

    def test_persistent_flag_is_false(self):
        assert MemoryKeystore.persistent is False
        assert MemoryKeystore.available() is True


# ---------------------------------------------------------------------------
# FileKeystore
# ---------------------------------------------------------------------------


class TestFileKeystore:
    def test_round_trip_creates_file_with_0600(self, tmp_path):
        path = tmp_path / "secrets.json"
        ks = FileKeystore(path=path)
        assert ks.fetch("care", "k") is None
        ks.store("care", "k", "v")
        assert path.exists()
        mode = stat.S_IMODE(os.stat(path).st_mode)
        assert mode == 0o600, f"expected 0600, got {oct(mode)}"
        assert ks.fetch("care", "k") == "v"

    def test_per_service_buckets(self, tmp_path):
        path = tmp_path / "s.json"
        ks = FileKeystore(path=path)
        ks.store("svcA", "k", "vA")
        ks.store("svcB", "k", "vB")
        data = json.loads(path.read_text(encoding="utf-8"))
        assert data["svcA"]["k"] == "vA"
        assert data["svcB"]["k"] == "vB"

    def test_delete_empties_service_bucket(self, tmp_path):
        path = tmp_path / "s.json"
        ks = FileKeystore(path=path)
        ks.store("svc", "k", "v")
        ks.delete("svc", "k")
        data = json.loads(path.read_text(encoding="utf-8"))
        assert "svc" not in data

    def test_corrupt_file_surfaces_keystore_error(self, tmp_path):
        path = tmp_path / "s.json"
        path.write_text("not json", encoding="utf-8")
        ks = FileKeystore(path=path)
        with pytest.raises(KeystoreError):
            ks.fetch("care", "k")

    def test_atomic_write_no_leftover_tmp(self, tmp_path):
        path = tmp_path / "s.json"
        ks = FileKeystore(path=path)
        ks.store("svc", "k", "v")
        # The `.tmp` shadow used during write should not persist.
        assert not list(path.parent.glob("*.tmp"))

    def test_unique_temp_is_0600_before_first_write(
        self,
        tmp_path,
        monkeypatch,
    ):
        path = tmp_path / "s.json"
        real_mkstemp = keystore_module.tempfile.mkstemp
        real_fdopen = keystore_module.os.fdopen
        temp_paths = []
        pre_write_modes = []

        def _record_mkstemp(*args, **kwargs):
            fd, name = real_mkstemp(*args, **kwargs)
            temp_paths.append(name)
            return fd, name

        def _verify_fdopen(fd, *args, **kwargs):
            metadata = os.fstat(fd)
            pre_write_modes.append(
                stat.S_IMODE(metadata.st_mode)
            )
            assert metadata.st_size == 0
            return real_fdopen(fd, *args, **kwargs)

        monkeypatch.setattr(
            keystore_module.tempfile,
            "mkstemp",
            _record_mkstemp,
        )
        monkeypatch.setattr(
            keystore_module.os,
            "fdopen",
            _verify_fdopen,
        )
        ks = FileKeystore(path=path)
        ks.store("svc", "k", "first")
        ks.store("svc", "k", "second")

        assert len(set(temp_paths)) == 2
        assert pre_write_modes == [0o600, 0o600]
        assert all(not os.path.exists(tmp) for tmp in temp_paths)
        assert ks.fetch("svc", "k") == "second"

    def test_replace_failure_preserves_target_and_removes_temp(
        self,
        tmp_path,
        monkeypatch,
    ):
        path = tmp_path / "s.json"
        original = '{"svc": {"k": "old"}}\n'
        path.write_text(original, encoding="utf-8")

        def _replace_bomb(*_args):
            raise OSError("synthetic replace failure")

        monkeypatch.setattr(
            keystore_module.os,
            "replace",
            _replace_bomb,
        )
        with pytest.raises(KeystoreError):
            FileKeystore(path=path).store("svc", "k", "new")

        assert path.read_text(encoding="utf-8") == original
        assert not list(tmp_path.glob("*.tmp"))

    def test_replace_interrupt_removes_secret_temp(
        self,
        tmp_path,
        monkeypatch,
    ):
        path = tmp_path / "s.json"
        original = '{"svc": {"k": "old"}}\n'
        path.write_text(original, encoding="utf-8")

        def _replace_interrupt(*_args):
            raise KeyboardInterrupt

        monkeypatch.setattr(
            keystore_module.os,
            "replace",
            _replace_interrupt,
        )
        with pytest.raises(KeyboardInterrupt):
            FileKeystore(path=path).store("svc", "k", "new-secret")

        assert path.read_text(encoding="utf-8") == original
        assert not list(tmp_path.glob("*.tmp"))

    def test_default_path_under_home(self):
        # Smoke — the canonical default lives under ~/.config/care.
        assert ".config/care" in str(DEFAULT_FILE_KEYSTORE_PATH)


# ---------------------------------------------------------------------------
# MacOSKeychainKeystore
# ---------------------------------------------------------------------------


class _FakeMacOSAPI:
    """In-memory Security.framework seam with scripted OSStatus results."""

    def __init__(
        self,
        *,
        add_results=None,
        find_results=None,
        copy_results=None,
        noninteractive_copy_results=None,
        noninteractive_delete_results=None,
        modify_results=None,
        delete_results=None,
    ) -> None:
        self.add_results = list(
            [(0, "added-item")] if add_results is None else add_results
        )
        self.find_results = list([] if find_results is None else find_results)
        self.copy_results = list([] if copy_results is None else copy_results)
        self.noninteractive_copy_results = list(
            []
            if noninteractive_copy_results is None
            else noninteractive_copy_results
        )
        self.noninteractive_delete_results = list(
            []
            if noninteractive_delete_results is None
            else noninteractive_delete_results
        )
        self.modify_results = list([] if modify_results is None else modify_results)
        self.delete_results = list([] if delete_results is None else delete_results)
        self.calls: list[tuple] = []
        self.password_snapshots: list[bytes] = []
        self.password_buffers: list[bytearray] = []
        self.released: list[object] = []

    @staticmethod
    def _next(results, operation):
        if not results:
            raise AssertionError(f"no scripted {operation} result")
        return results.pop(0)

    def add_generic_password(self, service, account, password):
        self.calls.append(("add", service, account))
        self.password_snapshots.append(bytes(password))
        self.password_buffers.append(password)
        return self._next(self.add_results, "add")

    def find_item(self, service, account):
        self.calls.append(("find", service, account))
        return self._next(self.find_results, "find")

    def copy_password(self, service, account):
        self.calls.append(("copy", service, account))
        return self._next(self.copy_results, "copy")

    def copy_password_noninteractive(self, service, account):
        self.calls.append(("copy-noninteractive", service, account))
        return self._next(
            self.noninteractive_copy_results,
            "non-interactive copy",
        )

    def delete_generic_password_noninteractive(self, service, account):
        self.calls.append(("delete-noninteractive", service, account))
        return self._next(
            self.noninteractive_delete_results,
            "non-interactive delete",
        )

    def modify_item(self, item, password):
        self.calls.append(("modify", item))
        self.password_snapshots.append(bytes(password))
        self.password_buffers.append(password)
        return self._next(self.modify_results, "modify")

    def delete_item(self, item):
        self.calls.append(("delete", item))
        return self._next(self.delete_results, "delete")

    def release_item(self, item):
        self.calls.append(("release", item))
        self.released.append(item)


class TestMacOSKeychain:
    def test_store_adds_nul_safe_utf8_and_scrubs_mutable_buffer(self):
        api = _FakeMacOSAPI()
        ks = MacOSKeychainKeystore(api=api)

        ks.store("ca\x00ré", "k\x00y", "v\x00\n")

        assert api.calls == [
            ("add", "ca\x00ré".encode(), b"k\x00y"),
            ("release", "added-item"),
        ]
        assert api.password_snapshots == [b"v\x00\n"]
        assert api.password_buffers[0] == bytearray(b"\x00" * 3)

    def test_store_duplicate_finds_and_modifies_existing_item(self):
        api = _FakeMacOSAPI(
            add_results=[(-25299, None)],
            find_results=[(0, "existing-item")],
            modify_results=[0],
        )
        ks = MacOSKeychainKeystore(api=api)

        ks.store("care", "k", "updated")

        assert [call[0] for call in api.calls] == [
            "add",
            "find",
            "modify",
            "release",
        ]
        assert api.released == ["existing-item"]
        assert all(not any(buffer) for buffer in api.password_buffers)

    def test_store_retries_add_after_duplicate_lookup_race(self):
        api = _FakeMacOSAPI(
            add_results=[(-25299, None), (0, "new-item")],
            find_results=[(-25300, None)],
        )
        ks = MacOSKeychainKeystore(api=api)

        ks.store("care", "k", "value")

        assert [call[0] for call in api.calls] == [
            "add",
            "find",
            "add",
            "release",
        ]
        assert api.released == ["new-item"]

    @pytest.mark.parametrize("race_status", [-25300, -25304])
    def test_store_retries_after_modify_race(self, race_status):
        api = _FakeMacOSAPI(
            add_results=[(-25299, None), (-25299, None)],
            find_results=[(0, "stale-item"), (0, "current-item")],
            modify_results=[race_status, 0],
        )
        ks = MacOSKeychainKeystore(api=api)

        ks.store("care", "k", "value")

        assert api.released == ["stale-item", "current-item"]
        assert [call[0] for call in api.calls].count("add") == 2

    def test_fetch_preserves_newlines_and_nul_then_scrubs_framework_copy(self):
        returned = bytearray(b"secret\x00line\n")
        api = _FakeMacOSAPI(copy_results=[(0, returned)])
        ks = MacOSKeychainKeystore(api=api)

        assert ks.fetch("care", "k") == "secret\x00line\n"
        assert returned == bytearray(len(returned))

    def test_fetch_missing_returns_none(self):
        api = _FakeMacOSAPI(copy_results=[(-25300, None)])
        ks = MacOSKeychainKeystore(api=api)
        assert ks.fetch("care", "k") is None

    def test_noninteractive_fetch_uses_distinct_per_read_api_and_scrubs_copy(self):
        returned = bytearray(b"background-secret")
        api = _FakeMacOSAPI(noninteractive_copy_results=[(0, returned)])
        ks = MacOSKeychainKeystore(api=api)

        assert ks.fetch_noninteractive("care", "mage.api_key") == "background-secret"
        assert api.calls == [
            ("copy-noninteractive", b"care", b"mage.api_key"),
        ]
        assert returned == bytearray(len(returned))

    @pytest.mark.parametrize("status", [-128, -25293, -25308, -25315])
    def test_noninteractive_fetch_maps_authentication_statuses(self, status):
        api = _FakeMacOSAPI(noninteractive_copy_results=[(status, None)])
        ks = MacOSKeychainKeystore(api=api)

        with pytest.raises(KeystoreInteractionRequiredError):
            ks.fetch_noninteractive("care", "mage.api_key")

        assert not any(call[0] == "copy" for call in api.calls)

    def test_noninteractive_fetch_times_out_and_scrubs_late_secret(self):
        release = threading.Event()
        returned = bytearray(b"late-background-secret")

        class BlockingAPI(_FakeMacOSAPI):
            def copy_password_noninteractive(self, service, account):
                self.calls.append(("copy-noninteractive", service, account))
                release.wait(timeout=1.0)
                return 0, returned

        api = BlockingAPI()
        ks = MacOSKeychainKeystore(
            api=api,
            noninteractive_timeout=0.01,
        )

        with pytest.raises(KeystoreAccessTimeoutError, match="within 0.01s"):
            ks.fetch_noninteractive("care", "mage.api_key")

        release.set()
        deadline = time.monotonic() + 1.0
        while any(returned) and time.monotonic() < deadline:
            time.sleep(0.01)
        assert returned == bytearray(len(returned))

    @pytest.mark.parametrize(
        ("status", "name"),
        [
            (-128, "errSecUserCanceled"),
            (-25293, "errSecAuthFailed"),
            (-25308, "errSecInteractionNotAllowed"),
            (-25315, "errSecInteractionRequired"),
        ],
    )
    def test_fetch_authorization_and_interaction_fail_closed(self, status, name):
        api = _FakeMacOSAPI(copy_results=[(status, None)])
        ks = MacOSKeychainKeystore(api=api)
        with pytest.raises(KeystoreInteractionRequiredError) as error:
            ks.fetch("care", "k")
        assert name in str(error.value)
        assert str(status) in str(error.value)

    def test_fetch_rejects_non_utf8_and_scrubs_copy(self):
        returned = bytearray(b"\xffsecret")
        api = _FakeMacOSAPI(copy_results=[(0, returned)])
        ks = MacOSKeychainKeystore(api=api)

        with pytest.raises(KeystoreError, match="not valid UTF-8"):
            ks.fetch("care", "k")
        assert returned == bytearray(len(returned))

    def test_delete_missing_is_idempotent_only_for_framework_not_found(self):
        api = _FakeMacOSAPI(find_results=[(-25300, None)])
        ks = MacOSKeychainKeystore(api=api)
        ks.delete("care", "k")  # no raise
        assert api.calls == [("find", b"care", b"k")]

    def test_delete_releases_item_on_success(self):
        api = _FakeMacOSAPI(
            find_results=[(0, "item")],
            delete_results=[0],
        )
        ks = MacOSKeychainKeystore(api=api)
        ks.delete("care", "k")
        assert api.calls == [
            ("find", b"care", b"k"),
            ("delete", "item"),
            ("release", "item"),
        ]

    @pytest.mark.parametrize("race_status", [-25300, -25304])
    def test_delete_post_lookup_race_is_idempotent(self, race_status):
        api = _FakeMacOSAPI(
            find_results=[(0, "stale-item")],
            delete_results=[race_status],
        )
        ks = MacOSKeychainKeystore(api=api)

        ks.delete("care", "k")

        assert api.released == ["stale-item"]

    def test_delete_auth_failure_fails_closed_and_releases_item(self):
        api = _FakeMacOSAPI(
            find_results=[(0, "item")],
            delete_results=[-25293],
        )
        ks = MacOSKeychainKeystore(api=api)
        with pytest.raises(KeystoreError, match="errSecAuthFailed"):
            ks.delete("care", "k")
        assert api.released == ["item"]

    def test_noninteractive_delete_uses_distinct_per_query_api(self):
        api = _FakeMacOSAPI(noninteractive_delete_results=[0])
        ks = MacOSKeychainKeystore(api=api)

        ks.delete_noninteractive("care", "mage.api_key")

        assert api.calls == [
            ("delete-noninteractive", b"care", b"mage.api_key"),
        ]

    def test_noninteractive_delete_missing_is_idempotent(self):
        api = _FakeMacOSAPI(noninteractive_delete_results=[-25300])
        MacOSKeychainKeystore(api=api).delete_noninteractive("care", "missing")

    @pytest.mark.parametrize("status", [-128, -25293, -25308, -25315])
    def test_noninteractive_delete_maps_authentication_statuses(self, status):
        api = _FakeMacOSAPI(noninteractive_delete_results=[status])

        with pytest.raises(KeystoreInteractionRequiredError):
            MacOSKeychainKeystore(api=api).delete_noninteractive("care", "key")

        assert not any(call[0] in {"find", "delete"} for call in api.calls)

    def test_noninteractive_delete_times_out_without_interactive_fallback(self):
        release = threading.Event()

        class BlockingAPI(_FakeMacOSAPI):
            def delete_generic_password_noninteractive(self, service, account):
                self.calls.append(("delete-noninteractive", service, account))
                release.wait(timeout=1.0)
                return 0

        api = BlockingAPI()
        ks = MacOSKeychainKeystore(
            api=api,
            noninteractive_timeout=0.01,
        )
        try:
            with pytest.raises(KeystoreAccessTimeoutError, match="within 0.01s"):
                ks.delete_noninteractive("care", "mage.api_key")
        finally:
            release.set()

        assert not any(call[0] in {"find", "delete"} for call in api.calls)

    def test_empty_value_rejected(self):
        api = _FakeMacOSAPI(add_results=[])
        ks = MacOSKeychainKeystore(api=api)
        with pytest.raises(KeystoreError):
            ks.store("care", "k", "")
        assert api.calls == []

    def test_never_calls_subprocess_or_security_cli(self, monkeypatch):
        def _subprocess_bomb(*_args, **_kwargs):
            raise AssertionError("macOS Keychain must not launch subprocesses")

        monkeypatch.setattr(keystore_module.subprocess, "run", _subprocess_bomb)
        api = _FakeMacOSAPI(
            add_results=[(0, "added")],
            copy_results=[(0, bytearray(b"value"))],
            find_results=[(0, "item")],
            delete_results=[0],
        )
        ks = MacOSKeychainKeystore(api=api)
        ks.store("care", "k", "value")
        assert ks.fetch("care", "k") == "value"
        ks.delete("care", "k")

    def test_store_error_is_mapped_without_secret_or_raw_backend_text(self):
        api = _FakeMacOSAPI(add_results=[(-25293, "unexpected-item")])
        ks = MacOSKeychainKeystore(api=api)

        with pytest.raises(KeystoreError) as error:
            ks.store("care", "k", "never-print-this")

        message = str(error.value)
        assert "errSecAuthFailed" in message
        assert "-25293" in message
        assert "never-print-this" not in message
        assert api.released == ["unexpected-item"]

    def test_available_loads_framework_without_cli_path_probe(self, monkeypatch):
        loaded = []

        def _load(_cls):
            loaded.append(True)
            return object()

        def _which_bomb(_name):
            raise AssertionError("macOS backend must not probe for security CLI")

        monkeypatch.setattr(keystore_module.sys, "platform", "darwin")
        monkeypatch.setattr(
            keystore_module._MacOSSecurityAPI,
            "load",
            classmethod(_load),
        )
        monkeypatch.setattr(keystore_module.shutil, "which", _which_bomb)

        assert MacOSKeychainKeystore.available() is True
        assert loaded == [True]


class _FakeCFunction:
    def __init__(self, callback):
        self.callback = callback
        self.argtypes = None
        self.restype = None

    def __call__(self, *args):
        return self.callback(*args)


class _FakeSecurityFramework:
    """Raw C-callable-shaped fake used to verify ctypes ABI boundaries."""

    def __init__(self, copied_password: bytearray) -> None:
        self.add_call = None
        self.modify_call = None
        self.freed_bytes = None
        self.interaction_states: list[bool] = []
        self._copied_password = copied_password
        self._copied_buffer = (ctypes.c_ubyte * len(copied_password)).from_buffer(
            copied_password
        )
        self.SecKeychainAddGenericPassword = _FakeCFunction(self._add)
        self.SecKeychainFindGenericPassword = _FakeCFunction(self._find)
        self.SecKeychainItemModifyAttributesAndData = _FakeCFunction(self._modify)
        self.SecKeychainItemDelete = _FakeCFunction(lambda _item: 0)
        self.SecKeychainItemFreeContent = _FakeCFunction(self._free)
        self.SecKeychainSetUserInteractionAllowed = _FakeCFunction(
            self._set_interaction,
        )

    def _add(
        self,
        _keychain,
        service_length,
        service_data,
        account_length,
        account_data,
        password_length,
        password_data,
        item_out,
    ):
        self.add_call = (
            service_length,
            ctypes.string_at(service_data, service_length),
            account_length,
            ctypes.string_at(account_data, account_length),
            password_length,
            ctypes.string_at(password_data, password_length),
        )
        ctypes.cast(item_out, ctypes.POINTER(ctypes.c_void_p))[0] = ctypes.c_void_p(
            0xCAFE
        )
        return 0

    def _find(
        self,
        _keychain,
        _service_length,
        _service_data,
        _account_length,
        _account_data,
        password_length_out,
        password_data_out,
        item_out,
    ):
        if password_length_out is not None:
            ctypes.cast(
                password_length_out,
                ctypes.POINTER(ctypes.c_uint32),
            )[0] = len(self._copied_password)
            ctypes.cast(
                password_data_out,
                ctypes.POINTER(ctypes.c_void_p),
            )[0] = ctypes.cast(self._copied_buffer, ctypes.c_void_p)
        if item_out is not None:
            ctypes.cast(item_out, ctypes.POINTER(ctypes.c_void_p))[0] = ctypes.c_void_p(
                0xBEEF
            )
        return 0

    def _modify(self, item, _attributes, password_length, password_data):
        self.modify_call = (
            item,
            password_length,
            ctypes.string_at(password_data, password_length),
        )
        return 0

    def _free(self, _attributes, password_data):
        self.freed_bytes = ctypes.string_at(
            password_data,
            len(self._copied_password),
        )
        return 0

    def _set_interaction(self, state):
        self.interaction_states.append(bool(state))
        return 0


class _FakeCoreFoundation:
    def __init__(self) -> None:
        self.released = []
        self.CFRelease = _FakeCFunction(self.released.append)


def _pointer_value(value) -> int | None:
    if value is None:
        return None
    if isinstance(value, int):
        return value
    return ctypes.cast(value, ctypes.c_void_p).value


class _FakeSecItemSecurityFramework(_FakeSecurityFramework):
    def __init__(self, copied_password: bytearray) -> None:
        super().__init__(copied_password)
        self.query = None
        self.delete_query = None
        self.SecItemCopyMatching = _FakeCFunction(self._copy_matching)
        self.SecItemDelete = _FakeCFunction(self._delete_item)

    def _copy_matching(self, query, result_out):
        self.query = _pointer_value(query)
        ctypes.cast(result_out, ctypes.POINTER(ctypes.c_void_p))[0] = (
            ctypes.c_void_p(0xDA7A)
        )
        return 0

    def _delete_item(self, query):
        self.delete_query = _pointer_value(query)
        return 0


class _FakeSecItemCoreFoundation(_FakeCoreFoundation):
    def __init__(self, copied_password: bytearray) -> None:
        super().__init__()
        self.dictionary_callbacks = None
        self.dictionary_values: dict[int, int] = {}
        self.strings: dict[int, bytes] = {}
        self._next_string = 0x5000
        self._copied_password = copied_password
        self._copied_buffer = (
            ctypes.c_ubyte * len(copied_password)
        ).from_buffer(copied_password)
        self.CFDictionaryCreateMutable = _FakeCFunction(self._create_dictionary)
        self.CFDictionarySetValue = _FakeCFunction(self._set_dictionary_value)
        self.CFStringCreateWithBytes = _FakeCFunction(self._create_string)
        self.CFGetTypeID = _FakeCFunction(lambda _value: 7)
        self.CFDataGetTypeID = _FakeCFunction(lambda: 7)
        self.CFDataGetLength = _FakeCFunction(
            lambda _value: len(self._copied_password),
        )
        self.CFDataGetBytePtr = _FakeCFunction(
            lambda _value: ctypes.cast(
                self._copied_buffer,
                ctypes.POINTER(ctypes.c_ubyte),
            ),
        )

    def _create_dictionary(self, _allocator, _capacity, key_callbacks, value_callbacks):
        self.dictionary_callbacks = (
            _pointer_value(key_callbacks),
            _pointer_value(value_callbacks),
        )
        return 0xD1C7

    def _set_dictionary_value(self, _dictionary, key, value):
        self.dictionary_values[_pointer_value(key)] = _pointer_value(value)

    def _create_string(self, _allocator, value, length, encoding, external):
        assert encoding == 0x08000100
        assert external == 0
        pointer = self._next_string
        self._next_string += 1
        self.strings[pointer] = ctypes.string_at(value, length)
        return pointer


class TestMacOSSecurityFrameworkBindings:
    def test_explicit_lengths_are_nul_safe_and_framework_copy_is_wiped(self):
        allocated_password = bytearray(b"out\x00\n")
        security = _FakeSecurityFramework(allocated_password)
        core_foundation = _FakeCoreFoundation()
        api = keystore_module._MacOSSecurityAPI(
            security=security,
            core_foundation=core_foundation,
        )
        input_password = bytearray(b"in\x00\n")

        status, item = api.add_generic_password(
            b"s\x00ervice",
            b"a\x00ccount",
            input_password,
        )
        copy_status, copied = api.copy_password(b"s\x00ervice", b"a\x00ccount")
        interaction_status = api.set_user_interaction_allowed(False)
        api.release_item(item)

        assert status == 0
        assert copy_status == 0
        assert interaction_status == 0
        assert security.interaction_states == [False]
        assert copied == bytearray(b"out\x00\n")
        assert security.add_call == (
            8,
            b"s\x00ervice",
            8,
            b"a\x00ccount",
            4,
            b"in\x00\n",
        )
        assert allocated_password == bytearray(b"\x00" * 5)
        assert security.freed_bytes == b"\x00" * 5
        assert core_foundation.released

    def test_secitem_read_sets_per_query_ui_fail_and_supports_legacy_items(self):
        allocated_password = bytearray(b"legacy-secret")
        security = _FakeSecItemSecurityFramework(allocated_password)
        core_foundation = _FakeSecItemCoreFoundation(allocated_password)
        symbols = {
            "kCFTypeDictionaryKeyCallBacks": 0x1001,
            "kCFTypeDictionaryValueCallBacks": 0x1002,
            "kSecClass": 0x2001,
            "kSecClassGenericPassword": 0x2002,
            "kSecAttrService": 0x2003,
            "kSecAttrAccount": 0x2004,
            "kSecReturnData": 0x2005,
            "kCFBooleanTrue": 0x2006,
            "kSecMatchLimit": 0x2007,
            "kSecMatchLimitOne": 0x2008,
            "kSecUseAuthenticationUI": 0x2009,
            "kSecUseAuthenticationUIFail": 0x2010,
            "kSecUseDataProtectionKeychain": 0x2011,
        }
        api = keystore_module._MacOSSecurityAPI(
            security=security,
            core_foundation=core_foundation,
            secitem_symbols=symbols,
        )
        ks = MacOSKeychainKeystore(api=api)

        assert ks.fetch_noninteractive("care", "mage.api_key") == "legacy-secret"

        assert security.query == 0xD1C7
        assert core_foundation.dictionary_callbacks == (0x1001, 0x1002)
        assert core_foundation.dictionary_values[symbols["kSecClass"]] == symbols[
            "kSecClassGenericPassword"
        ]
        assert core_foundation.dictionary_values[
            symbols["kSecUseAuthenticationUI"]
        ] == symbols["kSecUseAuthenticationUIFail"]
        assert (
            symbols["kSecUseDataProtectionKeychain"]
            not in core_foundation.dictionary_values
        )
        assert sorted(core_foundation.strings.values()) == [
            b"care",
            b"mage.api_key",
        ]

    def test_secitem_delete_sets_per_query_ui_fail(self):
        allocated_password = bytearray(b"unused")
        security = _FakeSecItemSecurityFramework(allocated_password)
        core_foundation = _FakeSecItemCoreFoundation(allocated_password)
        symbols = {
            "kCFTypeDictionaryKeyCallBacks": 0x1001,
            "kCFTypeDictionaryValueCallBacks": 0x1002,
            "kSecClass": 0x2001,
            "kSecClassGenericPassword": 0x2002,
            "kSecAttrService": 0x2003,
            "kSecAttrAccount": 0x2004,
            "kSecUseAuthenticationUI": 0x2009,
            "kSecUseAuthenticationUIFail": 0x2010,
        }
        api = keystore_module._MacOSSecurityAPI(
            security=security,
            core_foundation=core_foundation,
            secitem_symbols=symbols,
        )

        status = api.delete_generic_password_noninteractive(
            b"care",
            b"mage.api_key",
        )

        assert status == 0
        assert security.delete_query == 0xD1C7
        assert core_foundation.dictionary_values[
            symbols["kSecUseAuthenticationUI"]
        ] == symbols["kSecUseAuthenticationUIFail"]
        assert sorted(core_foundation.strings.values()) == [
            b"care",
            b"mage.api_key",
        ]

    def test_background_guard_disables_interaction_without_touching_keychain(
        self,
        monkeypatch,
    ):
        calls: list[bool] = []

        class _API:
            def set_user_interaction_allowed(self, allowed: bool) -> int:
                calls.append(allowed)
                return 0

        monkeypatch.setattr(keystore_module.sys, "platform", "darwin")
        monkeypatch.setattr(
            keystore_module._MacOSSecurityAPI,
            "load",
            classmethod(lambda _cls: _API()),
        )

        keystore_module.disable_macos_keychain_interaction()

        assert calls == [False]

    def test_background_guard_fails_closed_when_framework_rejects_policy(
        self,
        monkeypatch,
    ):
        class _API:
            @staticmethod
            def set_user_interaction_allowed(_allowed: bool) -> int:
                return -25291

        monkeypatch.setattr(keystore_module.sys, "platform", "darwin")
        monkeypatch.setattr(
            keystore_module._MacOSSecurityAPI,
            "load",
            classmethod(lambda _cls: _API()),
        )

        with pytest.raises(KeystoreError, match="errSecNotAvailable"):
            keystore_module.disable_macos_keychain_interaction()


# ---------------------------------------------------------------------------
# LinuxSecretToolKeystore
# ---------------------------------------------------------------------------


class TestLinuxSecretTool:
    def test_store_sends_value_via_stdin(self):
        rec = _Recorder()
        rec.push(_FakeProc(returncode=0))
        ks = LinuxSecretToolKeystore(runner=rec)
        ks.store("care", "k", "v")
        args, stdin = rec.calls[0]
        assert args[:2] == ["secret-tool", "store"]
        assert "--label" in args
        assert "service" in args and args[args.index("service") + 1] == "care"
        assert "key" in args and args[args.index("key") + 1] == "k"
        assert stdin == "v"  # never via argv

    def test_lookup_miss_returns_none(self):
        rec = _Recorder()
        rec.push(_FakeProc(returncode=1, stdout=""))
        ks = LinuxSecretToolKeystore(runner=rec)
        assert ks.fetch("care", "k") is None

    def test_lookup_hit_strips_trailing_newline(self):
        rec = _Recorder()
        rec.push(_FakeProc(returncode=0, stdout="found-secret\n"))
        ks = LinuxSecretToolKeystore(runner=rec)
        assert ks.fetch("care", "k") == "found-secret"

    def test_clear_invokes_secret_tool(self):
        rec = _Recorder()
        rec.push(_FakeProc(returncode=0))
        ks = LinuxSecretToolKeystore(runner=rec)
        ks.delete("care", "k")
        args, _stdin = rec.calls[0]
        assert args[:2] == ["secret-tool", "clear"]


# ---------------------------------------------------------------------------
# detect_keystore
# ---------------------------------------------------------------------------


class _NeverAvailable(Keystore):
    name = "never"
    persistent = True

    @classmethod
    def available(cls) -> bool:
        return False

    def store(self, service, key, value):
        raise AssertionError("never reached")

    def fetch(self, service, key):
        raise AssertionError("never reached")

    def delete(self, service, key):
        raise AssertionError("never reached")


class _AlwaysAvailable(Keystore):
    name = "always"
    persistent = True

    @classmethod
    def available(cls) -> bool:
        return True

    def store(self, service, key, value):
        pass

    def fetch(self, service, key):
        return None

    def delete(self, service, key):
        pass


class TestDetectKeystore:
    def test_prefers_caller_supplied_backend(self):
        ks = detect_keystore(prefer=[_AlwaysAvailable])
        assert isinstance(ks, _AlwaysAvailable)

    def test_skips_unavailable_and_falls_back_to_file(self, tmp_path, monkeypatch):
        # Force the OS-native backends "unavailable" so the test
        # is stable regardless of which host runs it (macOS dev
        # boxes would otherwise pick MacOSKeychainKeystore here).
        monkeypatch.setattr(
            MacOSKeychainKeystore, "available", classmethod(lambda cls: False),
        )
        monkeypatch.setattr(
            LinuxSecretToolKeystore, "available", classmethod(lambda cls: False),
        )
        path = tmp_path / "s.json"
        ks = detect_keystore(prefer=[_NeverAvailable], file_path=path)
        assert isinstance(ks, FileKeystore)
        assert ks.path == path

    def test_default_path_when_no_override(self, monkeypatch):
        # Force OS-native backends "unavailable" by patching the
        # classmethod, then assert the fallback FileKeystore uses
        # the canonical default path.
        monkeypatch.setattr(MacOSKeychainKeystore, "available", classmethod(lambda cls: False))
        monkeypatch.setattr(LinuxSecretToolKeystore, "available", classmethod(lambda cls: False))
        ks = detect_keystore()
        assert isinstance(ks, FileKeystore)
        assert ks.path == DEFAULT_FILE_KEYSTORE_PATH


# ---------------------------------------------------------------------------
# resolve_secret / store_secret
# ---------------------------------------------------------------------------


class TestPublicHelpers:
    def test_resolve_literal_passthrough(self):
        assert resolve_secret("sk-1234") == "sk-1234"

    def test_resolve_none_returns_none(self):
        assert resolve_secret(None) is None

    def test_resolve_dereferences_url(self):
        ks = MemoryKeystore()
        ks.store(DEFAULT_SERVICE, "mage.api_key", "sk-from-store")
        url = make_keystore_url(DEFAULT_SERVICE, "mage.api_key")
        assert resolve_secret(url, keystore=ks) == "sk-from-store"

    def test_resolve_missing_entry_returns_none(self):
        ks = MemoryKeystore()
        url = make_keystore_url(DEFAULT_SERVICE, "absent")
        assert resolve_secret(url, keystore=ks) is None

    def test_noninteractive_resolve_uses_mac_query_without_interactive_fetch(self):
        api = _FakeMacOSAPI(
            noninteractive_copy_results=[(0, bytearray(b"sk-background"))],
        )
        ks = MacOSKeychainKeystore(api=api)
        url = make_keystore_url(DEFAULT_SERVICE, "mage.api_key")

        assert (
            resolve_secret_noninteractive(url, keystore=ks)
            == "sk-background"
        )
        assert api.calls == [
            ("copy-noninteractive", b"care", b"mage.api_key"),
        ]

    def test_resolve_malformed_url_raises(self):
        with pytest.raises(KeystoreError):
            resolve_secret("keystore://no-key-half", keystore=MemoryKeystore())

    def test_store_secret_returns_url_and_persists(self):
        ks = MemoryKeystore()
        url = store_secret("sk-new", key="mage.api_key", keystore=ks)
        assert url == make_keystore_url(DEFAULT_SERVICE, "mage.api_key")
        assert ks.fetch(DEFAULT_SERVICE, "mage.api_key") == "sk-new"

    def test_store_secret_rejects_empty(self):
        ks = MemoryKeystore()
        with pytest.raises(KeystoreError):
            store_secret("", key="k", keystore=ks)


# ---------------------------------------------------------------------------
# Runtime export
# ---------------------------------------------------------------------------


class TestRuntimeExport:
    def test_top_level_exports_resolve(self):
        from care import runtime as r
        assert r.Keystore is Keystore
        assert r.MemoryKeystore is MemoryKeystore
        assert r.FileKeystore is FileKeystore
        assert r.MacOSKeychainKeystore is MacOSKeychainKeystore
        assert r.LinuxSecretToolKeystore is LinuxSecretToolKeystore
        assert r.detect_keystore is detect_keystore
        assert r.resolve_secret is resolve_secret
        assert r.resolve_secret_noninteractive is resolve_secret_noninteractive
        assert r.store_secret is store_secret
        assert r.DEFAULT_KEYSTORE_SERVICE == DEFAULT_SERVICE
