from __future__ import annotations

import itertools
import json
import threading
from collections.abc import Callable
from typing import Any

import pytest

from care.desktop_command_jobs import (
    CommandJobCapacityError,
    CommandJobClosedError,
    CommandJobNotFound,
    CommandJobResultUnavailable,
    DesktopCommandJobManager,
    raise_if_cancelled,
)
from care.desktop_commands import CommandBridge, CommandDispatch


def _id_factory() -> Callable[[], str]:
    counter = itertools.count()
    return lambda: f"job_{next(counter):08d}"


def _manager(
    runner: Callable[..., Any],
    **kwargs: Any,
) -> DesktopCommandJobManager:
    return DesktopCommandJobManager(
        runner=runner,
        id_factory=_id_factory(),
        **kwargs,
    )


def test_lifecycle_cursor_events_and_one_use_result() -> None:
    def runner(
        command: str,
        *,
        confirmation_token: str | None = None,
    ) -> CommandDispatch:
        assert command == "/catalog"
        assert confirmation_token is None
        return CommandDispatch(
            200,
            {
                "ok": True,
                "stdout": "catalog ready",
                "confirmation": {"token": "fresh-confirmation-capability"},
            },
        )

    manager = _manager(runner, max_jobs=1, max_concurrency=1)
    try:
        submitted = manager.submit("/catalog")
        observed = manager.wait_for_events(after=0, timeout=1, limit=1)
        assert observed["events"][0]["type"] == "command_job.queued"

        terminal = manager.wait(submitted["id"], timeout=2)
        assert terminal == manager.get_job(submitted["id"])
        assert terminal["status"] == "completed"
        assert terminal["result_available"] is True
        assert "payload" not in terminal
        assert "stdout" not in terminal

        first_page = manager.events(after=0, limit=2)
        second_page = manager.events(after=first_page["next_cursor"], limit=10)
        event_types = [
            event["type"] for event in [*first_page["events"], *second_page["events"]]
        ]
        assert event_types == [
            "command_job.queued",
            "command_job.started",
            "command_job.completed",
        ]
        assert all("result" not in event["data"] for event in second_page["events"])

        transcript = manager.take_result(submitted["id"])
        assert transcript["status"] == "completed"
        assert transcript["result"]["http_status"] == 200
        assert transcript["result"]["payload"]["stdout"] == "catalog ready"
        assert (
            transcript["result"]["payload"]["confirmation"]["token"]
            == "fresh-confirmation-capability"
        )
        transcript["result"]["payload"]["stdout"] = "caller mutation"
        with pytest.raises(CommandJobResultUnavailable):
            manager.take_result(submitted["id"])
        assert manager.get_job(submitted["id"])["result_consumed"] is True
    finally:
        manager.shutdown()


def test_bridge_execute_is_the_default_runner() -> None:
    confirmation = "confirmation-capability-for-test"

    class FakeBridge(CommandBridge):
        def __init__(self) -> None:
            self.calls: list[tuple[str, str | None]] = []

        def execute(
            self,
            command: Any,
            *,
            confirmation_token: Any = None,
            transcript: Any = None,
        ) -> CommandDispatch:
            assert transcript is None
            self.calls.append((command, confirmation_token))
            return CommandDispatch(200, {"echoed_confirmation": confirmation_token})

    bridge = FakeBridge()
    manager = DesktopCommandJobManager(
        bridge=bridge,
        id_factory=_id_factory(),
        max_jobs=1,
        max_concurrency=1,
    )
    try:
        job = manager.submit("/run chain-1", confirmation_token=confirmation)
        terminal = manager.wait(job["id"], timeout=2)
        assert terminal["status"] == "completed"
        assert terminal["cooperative"] is False
        assert bridge.calls == [("/run chain-1", confirmation)]
        assert confirmation not in json.dumps(manager.take_result(job["id"]))
    finally:
        manager.shutdown()


def test_cancel_before_start_never_invokes_queued_runner() -> None:
    first_started = threading.Event()
    release_first = threading.Event()
    calls: list[str] = []

    def runner(command: str, *, cancel_event: threading.Event) -> str:
        calls.append(command)
        if command == "first":
            first_started.set()
            assert release_first.wait(2)
        return f"finished {command}"

    manager = _manager(runner, max_jobs=2, max_concurrency=1)
    try:
        first = manager.submit("first")
        assert first_started.wait(2)
        second = manager.submit("second")
        assert second["status"] == "queued"

        cancelled = manager.cancel(second["id"])
        assert cancelled["status"] == "cancelled"
        assert manager.wait(second["id"], timeout=2)["status"] == "cancelled"
        assert calls == ["first"]

        release_first.set()
        assert manager.wait(first["id"], timeout=2)["status"] == "completed"
        transcript = manager.take_result(second["id"])
        assert transcript["cancellation_effective"] is True
        assert transcript["cooperative"] is False
    finally:
        release_first.set()
        manager.shutdown()


def test_cancel_during_cooperative_work_is_effective() -> None:
    started = threading.Event()

    def runner(command: str, *, cancel_event: threading.Event) -> None:
        assert command == "cooperative"
        started.set()
        cancel_event.wait(2)
        raise_if_cancelled(cancel_event)

    manager = _manager(runner, max_jobs=1, max_concurrency=1)
    try:
        job = manager.submit("cooperative")
        assert started.wait(2)
        requested = manager.cancel(job["id"])
        assert requested["status"] in {"cancelling", "cancelled"}

        terminal = manager.wait(job["id"], timeout=2)
        assert terminal["status"] == "cancelled"
        assert terminal["cancellation_requested"] is True
        assert terminal["cancellation_effective"] is True
        assert manager.take_result(job["id"])["cooperative"] is True
    finally:
        manager.shutdown()


def test_non_cooperative_call_cannot_be_force_killed() -> None:
    started = threading.Event()
    release = threading.Event()

    def runner(command: str) -> str:
        started.set()
        assert release.wait(2)
        return f"side effect may have happened for {command}"

    manager = _manager(runner, max_jobs=1, max_concurrency=1)
    try:
        job = manager.submit("non-cooperative")
        assert started.wait(2)
        requested = manager.cancel(job["id"])
        assert requested["status"] == "cancelling"
        assert requested["cooperative"] is False

        release.set()
        terminal = manager.wait(job["id"], timeout=2)
        assert terminal["status"] == "completed"
        assert terminal["cancellation_requested"] is True
        assert terminal["cancellation_effective"] is False
        transcript = manager.take_result(job["id"])
        assert transcript["cancellation_effective"] is False
    finally:
        release.set()
        manager.shutdown()


def test_active_capacity_and_worker_concurrency_are_bounded() -> None:
    release = threading.Event()
    two_started = threading.Event()
    state_lock = threading.Lock()
    active = 0
    peak = 0

    def runner(command: str, *, cancel_event: threading.Event) -> str:
        nonlocal active, peak
        del cancel_event
        with state_lock:
            active += 1
            peak = max(peak, active)
            if active == 2:
                two_started.set()
        try:
            assert release.wait(2), command
            return command
        finally:
            with state_lock:
                active -= 1

    manager = _manager(runner, max_jobs=3, max_concurrency=2, max_history=4)
    jobs: list[dict[str, Any]] = []
    try:
        jobs = [manager.submit(f"job-{index}") for index in range(3)]
        assert two_started.wait(2)
        with pytest.raises(CommandJobCapacityError):
            manager.submit("over-capacity")
        with state_lock:
            assert active == 2
            assert peak == 2

        release.set()
        assert all(
            manager.wait(job["id"], timeout=2)["status"] == "completed" for job in jobs
        )
        assert peak == 2
    finally:
        release.set()
        manager.shutdown()


def test_terminal_history_and_event_buffer_are_bounded() -> None:
    def runner(command: str) -> CommandDispatch:
        return CommandDispatch(200, {"stdout": f"result for {command}"})

    manager = _manager(
        runner,
        max_jobs=1,
        max_concurrency=1,
        max_history=2,
        max_events=3,
    )
    try:
        ids: list[str] = []
        for index in range(3):
            job = manager.submit(f"read-{index}")
            ids.append(job["id"])
            assert manager.wait(job["id"], timeout=2)["status"] == "completed"

        with pytest.raises(CommandJobNotFound):
            manager.get_job(ids[0])
        assert [item["id"] for item in manager.list_jobs()] == list(reversed(ids[1:]))

        event_page = manager.events(after=0)
        assert len(event_page["events"]) == 3
        assert event_page["cursor_gap"] is True
        assert event_page["truncated"] is True
        assert all("stdout" not in json.dumps(item) for item in event_page["events"])
    finally:
        manager.shutdown()


def test_transcript_projection_is_bounded_and_redacts_supplied_secrets() -> None:
    secret = "SYNTHETIC_JOB_SECRET_9f5d"
    old_confirmation = "SYNTHETIC_CONFIRMATION_SECRET_7a1c"

    def runner(
        command: str,
        *,
        confirmation_token: str | None = None,
    ) -> CommandDispatch:
        return CommandDispatch(
            200,
            {
                "stdout": f"echo {command} {confirmation_token} " + ("x" * 100),
                "api_key": secret,
                "credential_once": secret,
                "confirmation": {"token": "new-confirmation-capability"},
            },
        )

    manager = _manager(
        runner,
        max_jobs=1,
        max_concurrency=1,
        max_transcript_chars=512,
    )
    try:
        job = manager.submit(
            f"run --api-key {secret}",
            confirmation_token=old_confirmation,
        )
        terminal = manager.wait(job["id"], timeout=2)
        public_projection = json.dumps(
            {
                "snapshot": terminal,
                "events": manager.events(after=0),
            }
        )
        assert secret not in public_projection
        assert old_confirmation not in public_projection

        transcript = manager.take_result(job["id"])
        serialized = json.dumps(transcript)
        assert secret not in serialized
        assert old_confirmation not in serialized
        assert "[redacted]" in serialized
        assert len(serialized) < 1_500
    finally:
        manager.shutdown()


def test_memory_editor_document_transport_never_reaches_job_surfaces() -> None:
    encoded_document = (
        "IyBDQVJFLm1kCgpTWU5USEVUSUNfUFJJVkFURV9ET0NVTUVOVF9DT05URU5UXzlmNWQ"
    )
    base_sha256 = "a" * 64

    def runner(command: str) -> CommandDispatch:
        return CommandDispatch(
            200,
            {
                "stdout": f"processed {command}",
                "payload": {"echo": command},
            },
        )

    manager = _manager(runner, max_jobs=1, max_concurrency=1)
    try:
        job = manager.submit(
            f"/memory edit save {base_sha256} {encoded_document}"
        )
        terminal = manager.wait(job["id"], timeout=2)
        public = json.dumps(
            {
                "submitted": job,
                "terminal": terminal,
                "jobs": manager.list_jobs(),
                "events": manager.events(after=0),
            }
        )
        assert encoded_document not in public
        assert "[redacted]" in public

        transcript = json.dumps(manager.take_result(job["id"]))
        assert encoded_document not in transcript
        assert "[redacted]" in transcript
    finally:
        manager.shutdown()


def test_transcript_bound_includes_hostile_mapping_keys() -> None:
    hostile_payload = {
        f"field-{index:03d}-" + ("k" * 120): index for index in range(200)
    }

    def runner(command: str) -> CommandDispatch:
        del command
        return CommandDispatch(200, hostile_payload)

    manager = _manager(
        runner,
        max_jobs=1,
        max_concurrency=1,
        max_transcript_chars=256,
    )
    try:
        job = manager.submit("/catalog")
        assert manager.wait(job["id"], timeout=2)["status"] == "completed"
        transcript = manager.take_result(job["id"])
        assert transcript == {
            "status": "completed",
            "error": {"code": "command_result_truncated"},
            "truncated": True,
        }
        assert len(json.dumps(transcript, ensure_ascii=True, sort_keys=True)) <= 256
    finally:
        manager.shutdown()


def test_ephemeral_credential_is_available_only_in_one_use_transcript() -> None:
    credential = "agent-credential-once-1234567890"

    def runner(command: str) -> CommandDispatch:
        assert command == "/deploy chain-1"
        return CommandDispatch(
            200,
            {
                "ok": True,
                "ephemeral_secret": {
                    "kind": "credential",
                    "value": credential,
                    "expires_in_seconds": 60,
                },
            },
        )

    manager = _manager(runner, max_jobs=1, max_concurrency=1)
    try:
        job = manager.submit("/deploy chain-1")
        terminal = manager.wait(job["id"], timeout=2)
        public_projection = json.dumps(
            {
                "snapshot": terminal,
                "jobs": manager.list_jobs(),
                "events": manager.events(after=0),
            }
        )
        assert credential not in public_projection

        transcript = manager.take_result(job["id"])
        assert (
            transcript["result"]["payload"]["ephemeral_secret"]["value"] == credential
        )
        with pytest.raises(CommandJobResultUnavailable):
            manager.take_result(job["id"])
        assert credential not in json.dumps(manager.get_job(job["id"]))
        assert credential not in json.dumps(manager.events(after=0))
    finally:
        manager.shutdown()


def test_worker_failure_is_stable_and_redacts_command_secret() -> None:
    secret = "SYNTHETIC_FAILURE_SECRET_e43b"

    def runner(command: str) -> None:
        del command
        raise RuntimeError(f"provider rejected {secret}")

    manager = _manager(runner, max_jobs=1, max_concurrency=1)
    try:
        job = manager.submit(f"run --api-key={secret}")
        terminal = manager.wait(job["id"], timeout=2)
        assert terminal["status"] == "failed"
        transcript = manager.take_result(job["id"])
        serialized = json.dumps(transcript)
        assert secret not in serialized
        assert transcript["error"]["code"] == "command_job_failed"
    finally:
        manager.shutdown()


def test_shutdown_cancels_running_cooperative_and_queued_jobs() -> None:
    started = threading.Event()

    def runner(command: str, *, cancel_event: threading.Event) -> None:
        del command
        started.set()
        cancel_event.wait(2)
        raise_if_cancelled(cancel_event)

    manager = _manager(runner, max_jobs=2, max_concurrency=1)
    first = manager.submit("running")
    assert started.wait(2)
    second = manager.submit("queued")

    manager.shutdown(wait=True, cancel_pending=True)
    assert manager.closed is True
    assert manager.get_job(first["id"])["status"] == "cancelled"
    assert manager.get_job(second["id"])["status"] == "cancelled"
    assert any(
        event["type"] == "command_jobs.shutdown"
        for event in manager.events(after=0)["events"]
    )
    with pytest.raises(CommandJobClosedError):
        manager.submit("too late")
    manager.shutdown(wait=True, cancel_pending=True)
