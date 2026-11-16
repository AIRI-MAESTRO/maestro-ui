from __future__ import annotations

import json

from care.desktop_commands import CommandBridge
from care.redaction import redact_sensitive_text
from care.web import RunManager, map_runtime_message


def _payload_text(payload: object) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


def test_cli_parse_error_redacts_positional_provider_key_everywhere() -> None:
    secret = "sk-positional-secret-123456"
    result = CommandBridge().execute(f"catalog {secret}")

    rendered = _payload_text(result.payload)
    assert secret not in rendered
    assert "[redacted]" in rendered


def test_cli_handler_exception_redacts_backend_authorization_value() -> None:
    secret = "backend-secret-123456"

    def fail(_args, _stdout, _stderr) -> int:
        raise RuntimeError(f"Authorization: Bearer {secret}")

    result = CommandBridge(handler_overrides={"catalog": fail}).execute("catalog")

    rendered = _payload_text(result.payload)
    assert secret not in rendered
    assert "Authorization: [redacted]" in rendered


def test_failed_run_snapshot_and_events_redact_runtime_secret() -> None:
    secret = "backend-secret-123456"

    async def fail(_context) -> None:
        raise RuntimeError(f"Authorization: Bearer {secret}")

    manager = RunManager(runner=fail)
    try:
        run_id = manager.submit("safe task", execute=False)["id"]
        snapshot = manager.wait(run_id, timeout=3)
        events = manager.get_events(run_id)
    finally:
        manager.shutdown(wait=True)

    rendered = _payload_text({"snapshot": snapshot, "events": events})
    assert secret not in rendered
    assert "[redacted]" in rendered


def test_runtime_stage_error_and_secret_query_parameter_are_redacted() -> None:
    class StageError:
        stage = "planning"
        error = "failed https://provider.test/v1?api_key=query-secret-123456"

    event_type, payload = map_runtime_message(StageError(), source="generation")

    assert event_type == "generation.stage.error"
    assert "query-secret-123456" not in _payload_text(payload)
    assert redact_sensitive_text(
        "https://x.test?a=1&access_token=query-secret-123456",
    ).endswith("access_token=[redacted]")


def test_step_result_and_fallback_runtime_payloads_redact_nested_secrets() -> None:
    class StepCompleted:
        result = {
            "step_number": 1,
            "error": "Authorization: Bearer step-secret-123456",
            "nested": {"message": "token=another-secret-123456"},
        }

    class UnknownEvent:
        error = RuntimeError(
            "Authorization: Bearer fallback-secret-123456",
        )

    _, completed = map_runtime_message(StepCompleted(), source="execution")
    _, fallback = map_runtime_message(UnknownEvent(), source="execution")
    rendered = _payload_text({"completed": completed, "fallback": fallback})

    for secret in (
        "step-secret-123456",
        "another-secret-123456",
        "fallback-secret-123456",
    ):
        assert secret not in rendered
    assert rendered.count("[redacted]") >= 3
