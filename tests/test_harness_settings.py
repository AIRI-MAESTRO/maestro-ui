from __future__ import annotations

import json
import stat
import sys
import types
from pathlib import Path

import care.harness_settings as harness_settings
from care.desktop_settings import DesktopSettingsBridge, SettingsDispatch
from care.desktop_auth import APP_LOCK_MODE_ENV
from care.harness_settings import (
    HarnessSettingsApi,
    NativeSecretCaptureRequest,
    NativeSettingsApprovalRequest,
    PyWebViewNativeSettingsApproval,
)
from care.runtime.keystore import Keystore


SECRET = "SETTINGS_FACADE_SECRET_SENTINEL"
SECRET_VERSION = "abcdef0123456789"
SECRET_HANDLE = "opaque-native-secret-handle"


class FakeSecureKeystore(Keystore):
    name = "test-keychain"
    persistent = True

    def __init__(self) -> None:
        self.values: dict[tuple[str, str], str] = {}

    @classmethod
    def available(cls) -> bool:
        return True

    def store(self, service: str, key: str, value: str) -> None:
        self.values[(service, key)] = value

    def fetch(self, service: str, key: str) -> str | None:
        return self.values.get((service, key))

    def delete(self, service: str, key: str) -> None:
        self.values.pop((service, key), None)


def _bridge(
    tmp_path: Path,
    keystore: FakeSecureKeystore,
    *,
    environ: dict[str, str] | None = None,
) -> DesktopSettingsBridge:
    return DesktopSettingsBridge(
        config_path=tmp_path / "config.toml",
        env_path=tmp_path / ".env",
        cwd=tmp_path,
        desktop_auth_path=tmp_path / "desktop-auth.toml",
        keystore_factory=lambda: keystore,
        token_factory=lambda: "bridge-confirmation-token",
        secret_key_factory=lambda: SECRET_VERSION,
        environ={} if environ is None else environ,
    )


def _payload(secret_handle: str | None = None) -> dict[str, object]:
    payload: dict[str, object] = {
        "provider": "deepseek",
        "base_url": "https://api.deepseek.com",
        "model": "deepseek-chat",
        "generation_mode": "deep",
        "api_key_action": "replace" if secret_handle else "keep",
        "app_lock_mode": "touch-id",
    }
    if secret_handle:
        payload["api_key_handle"] = secret_handle
    return payload


def test_preview_cannot_commit_and_snapshot_never_returns_secret(
    tmp_path: Path,
) -> None:
    keystore = FakeSecureKeystore()
    facade = HarnessSettingsApi(
        _bridge(tmp_path, keystore),
        native_secret_capture=lambda _request: SECRET,
        secret_handle_factory=lambda: SECRET_HANDLE,
    )

    initial = facade.maestro_settings_snapshot()
    captured = facade.maestro_settings_capture_secret("api_key")
    payload = _payload(captured["payload"]["secret_handle"])
    preview = facade.maestro_settings_preview(payload)
    denied = facade.maestro_settings_save(
        payload,
        preview["payload"]["confirmation"]["token"],
    )

    assert initial["status"] == 200
    assert initial["payload"]["settings"]["app_lock_mode"] == "disabled"
    assert initial["payload"]["storage"]["existing_secrets_returned"] is False
    assert initial["payload"]["restart_policy"]["after_save"] == "full_app_restart"
    assert captured["status"] == 200
    assert preview["status"] == 409
    assert denied["status"] == 503
    assert denied["payload"]["error"]["code"] == "native_confirmation_unavailable"
    assert not (tmp_path / "config.toml").exists()
    assert not (tmp_path / ".env").exists()
    assert not (tmp_path / "desktop-auth.toml").exists()
    assert keystore.values == {}
    assert SECRET not in json.dumps(initial)
    assert SECRET not in json.dumps(captured)
    assert SECRET not in json.dumps(preview)
    assert SECRET not in json.dumps(denied)


def test_native_approved_save_round_trips_care_keychain_and_app_lock(
    tmp_path: Path,
) -> None:
    keystore = FakeSecureKeystore()
    approval_requests: list[NativeSettingsApprovalRequest] = []
    capture_requests: list[NativeSecretCaptureRequest] = []

    def approve(request: NativeSettingsApprovalRequest) -> bool:
        approval_requests.append(request)
        return True

    def capture(request: NativeSecretCaptureRequest) -> str:
        capture_requests.append(request)
        return SECRET

    facade = HarnessSettingsApi(
        _bridge(tmp_path, keystore),
        native_approval=approve,
        native_secret_capture=capture,
        secret_handle_factory=lambda: SECRET_HANDLE,
    )
    captured = facade.maestro_settings_capture_secret("api_key")
    payload = _payload(captured["payload"]["secret_handle"])
    preview = facade.maestro_settings_preview(payload)
    preview_token = preview["payload"]["confirmation"]["token"]
    preview_grant = facade._previews[preview_token]  # noqa: SLF001
    assert SECRET not in repr(preview_grant)
    assert captured["payload"]["secret_handle"] in repr(preview_grant)
    result = facade.maestro_settings_save(
        payload,
        preview_token,
    )

    assert result["status"] == 200
    assert result["payload"]["settings"]["provider"] == "deepseek"
    assert result["payload"]["settings"]["model"] == "deepseek-chat"
    assert result["payload"]["settings"]["api_key_present"] is True
    assert result["payload"]["settings"]["app_lock_mode"] == "touch-id"
    assert result["payload"]["restart_required"] is True
    assert result["payload"]["restart_scope"] == "full_app"
    assert "current agent" in result["payload"]["restart_message"]
    assert len(capture_requests) == 1
    assert capture_requests[0].field == "api_key"
    assert SECRET not in json.dumps(captured)
    assert len(approval_requests) == 1
    approval_text = json.dumps(
        {
            "title": approval_requests[0].title,
            "message": approval_requests[0].message,
            "changes": approval_requests[0].changes,
        }
    )
    assert SECRET not in approval_text
    assert "app lock (next launch)" in approval_text

    config_body = (tmp_path / "config.toml").read_text(encoding="utf-8")
    env_body = (tmp_path / ".env").read_text(encoding="utf-8")
    reference = f"keystore://care/mage.api_key.{SECRET_VERSION}"
    assert SECRET not in config_body
    assert SECRET not in env_body
    assert reference in config_body
    assert reference in env_body
    assert keystore.fetch("care", f"mage.api_key.{SECRET_VERSION}") == SECRET
    assert (tmp_path / "desktop-auth.toml").read_text() == 'mode = "touch-id"\n'
    assert stat.S_IMODE((tmp_path / "desktop-auth.toml").stat().st_mode) == 0o600

    reopened = HarnessSettingsApi(_bridge(tmp_path, keystore))
    snapshot = reopened.maestro_settings_snapshot()
    assert snapshot["status"] == 200
    assert snapshot["payload"]["settings"]["provider"] == "deepseek"
    assert snapshot["payload"]["settings"]["model"] == "deepseek-chat"
    assert snapshot["payload"]["settings"]["api_key_present"] is True
    assert snapshot["payload"]["settings"]["app_lock_mode"] == "touch-id"
    assert SECRET not in json.dumps(snapshot)


def test_save_requires_the_exact_preview_and_native_rejection_consumes_it(
    tmp_path: Path,
) -> None:
    keystore = FakeSecureKeystore()
    approvals: list[NativeSettingsApprovalRequest] = []

    def reject(request: NativeSettingsApprovalRequest) -> bool:
        approvals.append(request)
        return False

    facade = HarnessSettingsApi(
        _bridge(tmp_path, keystore),
        native_approval=reject,
        native_secret_capture=lambda _request: SECRET,
        secret_handle_factory=lambda: SECRET_HANDLE,
    )
    captured = facade.maestro_settings_capture_secret("api_key")
    payload = _payload(captured["payload"]["secret_handle"])
    preview = facade.maestro_settings_preview(payload)
    token = preview["payload"]["confirmation"]["token"]
    different = {**payload, "model": "different-model"}

    mismatch = facade.maestro_settings_save(different, token)
    assert mismatch["status"] == 409
    assert approvals == []

    rejected = facade.maestro_settings_save(payload, token)
    replay = facade.maestro_settings_save(payload, token)

    assert rejected["status"] == 403
    assert len(approvals) == 1
    assert replay["status"] == 409
    assert len(approvals) == 1
    assert not (tmp_path / "config.toml").exists()
    assert keystore.values == {}


def test_plaintext_secret_is_rejected_before_the_settings_bridge(
    tmp_path: Path,
) -> None:
    facade = HarnessSettingsApi(_bridge(tmp_path, FakeSecureKeystore()))
    payload = _payload()
    payload.update({"api_key_action": "replace", "api_key": SECRET})

    result = facade.maestro_settings_preview(payload)

    assert result["status"] == 400
    assert result["payload"]["error"]["code"] == "plaintext_secret_forbidden"
    assert SECRET not in json.dumps(result)
    assert not (tmp_path / "config.toml").exists()


def test_secret_handles_are_field_bound_expiring_and_consumed_by_preview(
    tmp_path: Path,
) -> None:
    now = [100.0]
    handles = iter(
        [
            "opaque-native-secret-handle-1",
            "opaque-native-secret-handle-2",
        ]
    )
    facade = HarnessSettingsApi(
        _bridge(tmp_path, FakeSecureKeystore()),
        native_secret_capture=lambda _request: SECRET,
        clock=lambda: now[0],
        secret_handle_factory=lambda: next(handles),
        secret_handle_ttl_seconds=10.0,
    )

    first = facade.maestro_settings_capture_secret("api_key")
    first_handle = first["payload"]["secret_handle"]
    wrong_field = _payload()
    wrong_field.update(
        {
            "memory_api_key_action": "replace",
            "memory_api_key_handle": first_handle,
        }
    )
    wrong = facade.maestro_settings_preview(wrong_field)
    preview = facade.maestro_settings_preview(_payload(first_handle))
    replay = facade.maestro_settings_preview(_payload(first_handle))

    second = facade.maestro_settings_capture_secret("api_key")
    now[0] = 111.0
    expired = facade.maestro_settings_preview(
        _payload(second["payload"]["secret_handle"])
    )

    assert wrong["status"] == 409
    assert wrong["payload"]["error"]["code"] == "secret_handle_invalid"
    assert preview["status"] == 409
    assert preview["payload"]["error"]["code"] == "confirmation_required"
    assert replay["status"] == 409
    assert replay["payload"]["error"]["code"] == "secret_handle_invalid"
    assert expired["status"] == 409
    assert expired["payload"]["error"]["code"] == "secret_handle_invalid"
    for response in (first, wrong, preview, replay, second, expired):
        assert SECRET not in json.dumps(response)


def test_invalid_preview_restores_the_native_handle_for_correction(
    tmp_path: Path,
) -> None:
    facade = HarnessSettingsApi(
        _bridge(tmp_path, FakeSecureKeystore()),
        native_secret_capture=lambda _request: SECRET,
        secret_handle_factory=lambda: SECRET_HANDLE,
    )
    captured = facade.maestro_settings_capture_secret("api_key")
    handle = captured["payload"]["secret_handle"]
    invalid = _payload(handle)
    invalid["base_url"] = "not-a-url"

    rejected = facade.maestro_settings_preview(invalid)
    corrected = facade.maestro_settings_preview(_payload(handle))

    assert rejected["status"] == 400
    assert corrected["status"] == 409
    assert corrected["payload"]["error"]["code"] == "confirmation_required"


def test_preview_grants_are_bounded_and_contain_only_opaque_handles() -> None:
    class PreviewBridge:
        def __init__(self) -> None:
            self.calls = 0

        def update(
            self,
            _settings: object,
            *,
            confirmation_token: object = None,
        ) -> SettingsDispatch:
            assert confirmation_token is None
            self.calls += 1
            return SettingsDispatch(
                409,
                {
                    "ok": False,
                    "error": {
                        "code": "confirmation_required",
                        "message": "Review required.",
                    },
                    "confirmation": {
                        "token": f"preview-token-{self.calls}",
                        "ttl_seconds": 90,
                    },
                    "changes": [f"change {self.calls}"],
                },
            )

    bridge = PreviewBridge()
    facade = HarnessSettingsApi(bridge, max_previews=64)  # type: ignore[arg-type]

    for index in range(100):
        response = facade.maestro_settings_preview({"model": f"model-{index}"})
        assert response["status"] == 409

    assert bridge.calls == 100
    assert len(facade._previews) == 64  # noqa: SLF001 - security capacity invariant
    assert "preview-token-1" not in facade._previews  # noqa: SLF001
    assert "preview-token-100" in facade._previews  # noqa: SLF001
    assert SECRET not in repr(tuple(facade._previews.values()))  # noqa: SLF001


def test_native_secret_cancel_and_failure_never_expose_backend_text(
    tmp_path: Path,
    caplog: object,
) -> None:
    cancelled = HarnessSettingsApi(
        _bridge(tmp_path, FakeSecureKeystore()),
        native_secret_capture=lambda _request: None,
    ).maestro_settings_capture_secret("api_key")

    def fail(_request: NativeSecretCaptureRequest) -> str:
        raise RuntimeError(f"native failure included {SECRET}")

    failed = HarnessSettingsApi(
        _bridge(tmp_path, FakeSecureKeystore()),
        native_secret_capture=fail,
    ).maestro_settings_capture_secret("api_key")

    assert cancelled["status"] == 403
    assert cancelled["payload"]["error"]["code"] == "native_secret_input_cancelled"
    assert failed["status"] == 503
    assert failed["payload"]["error"]["code"] == "native_secret_input_unavailable"
    assert SECRET not in json.dumps(cancelled)
    assert SECRET not in json.dumps(failed)
    assert SECRET not in getattr(caplog, "text", "")


def test_appkit_secure_prompt_clears_its_native_text_field(
    monkeypatch: object,
) -> None:
    fields: list[object] = []

    class Field:
        def __init__(self) -> None:
            self.value = SECRET
            self.assignments: list[str] = []

        def setPlaceholderString_(self, _value: str) -> None:
            pass

        def setStringValue_(self, value: str) -> None:
            self.assignments.append(value)
            if len(self.assignments) > 1:
                self.value = value

        def stringValue(self) -> str:
            return self.value

    class FieldFactory:
        @classmethod
        def alloc(cls) -> FieldFactory:
            return cls()

        def initWithFrame_(self, _frame: object) -> Field:
            field = Field()
            fields.append(field)
            return field

    class Alert:
        @classmethod
        def alloc(cls) -> Alert:
            return cls()

        def init(self) -> Alert:
            return self

        def setMessageText_(self, _value: str) -> None:
            pass

        def setInformativeText_(self, _value: str) -> None:
            pass

        def addButtonWithTitle_(self, _value: str) -> None:
            pass

        def setAccessoryView_(self, _value: object) -> None:
            pass

        def runModal(self) -> int:
            return 1000

    class Application:
        @classmethod
        def sharedApplication(cls) -> Application:
            return cls()

        def activateIgnoringOtherApps_(self, _value: bool) -> None:
            pass

    appkit = types.ModuleType("AppKit")
    appkit.NSAlert = Alert
    appkit.NSAlertFirstButtonReturn = 1000
    appkit.NSApplication = Application
    appkit.NSSecureTextField = FieldFactory
    foundation = types.ModuleType("Foundation")
    foundation.NSThread = types.SimpleNamespace(isMainThread=lambda: True)
    foundation.NSOperationQueue = object
    monkeypatch.setitem(sys.modules, "AppKit", appkit)  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "Foundation", foundation)  # type: ignore[attr-defined]

    result = harness_settings._run_appkit_secure_prompt(  # noqa: SLF001
        NativeSecretCaptureRequest(
            field="api_key",
            title="Set key",
            message="Native only",
            label="API key",
        ),
        timeout_seconds=1,
    )

    assert result == SECRET
    assert len(fields) == 1
    assert fields[0].assignments[-1] == ""  # type: ignore[attr-defined]
    assert fields[0].value == ""  # type: ignore[attr-defined]


def test_pywebview_approval_is_unavailable_until_bound_and_uses_native_dialog() -> None:
    approval = PyWebViewNativeSettingsApproval()
    request = NativeSettingsApprovalRequest(
        title="Save MAESTRO settings?",
        message="Safe message",
        changes=("model: old → new",),
    )

    try:
        approval(request)
    except RuntimeError as exc:
        assert "not bound" in str(exc)
    else:  # pragma: no cover - explicit fail-closed assertion
        raise AssertionError("unbound approval unexpectedly succeeded")

    calls: list[tuple[str, str]] = []

    class Window:
        def create_confirmation_dialog(self, title: str, message: str) -> bool:
            calls.append((title, message))
            return True

    approval.bind_window(Window())

    assert approval(request) is True
    assert calls == [
        (
            "Save MAESTRO settings?",
            "Safe message\n\n• model: old → new",
        )
    ]


def test_native_approval_never_hides_changes_after_the_sixteenth_row() -> None:
    approval = PyWebViewNativeSettingsApproval()
    calls: list[tuple[str, str]] = []

    class Window:
        def create_confirmation_dialog(self, title: str, message: str) -> bool:
            calls.append((title, message))
            return True

    approval.bind_window(Window())
    changes = tuple(
        [f"ordinary setting {index}: old → new" for index in range(16)]
        + ["provider endpoint: trusted.example → critical.example"]
    )

    assert approval(
        NativeSettingsApprovalRequest(
            title="Save MAESTRO settings?",
            message="Review every change.",
            changes=changes,
        )
    ) is True
    assert len(calls) == 1
    assert "ordinary setting 15" in calls[0][1]
    assert "provider endpoint: trusted.example → critical.example" in calls[0][1]
    assert "…and" not in calls[0][1]


def test_environment_owned_app_lock_is_visible_but_not_mutable(
    tmp_path: Path,
) -> None:
    keystore = FakeSecureKeystore()
    facade = HarnessSettingsApi(
        _bridge(
            tmp_path,
            keystore,
            environ={APP_LOCK_MODE_ENV: "touch-id"},
        ),
        native_approval=lambda _request: True,
    )

    snapshot = facade.maestro_settings_snapshot()
    payload = _payload()
    payload["app_lock_mode"] = "device-owner"
    preview = facade.maestro_settings_preview(payload)

    assert snapshot["status"] == 200
    assert snapshot["payload"]["settings"]["app_lock_mode"] == "touch-id"
    assert snapshot["payload"]["storage"]["app_lock_overridden_by_environment"] is True
    assert preview["status"] == 409
    assert preview["payload"]["error"]["code"] == "settings_source_conflict"
    assert "confirmation" not in preview["payload"]
    assert not (tmp_path / "desktop-auth.toml").exists()
