"""Persistent native-session contract tests."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from care.desktop_sessions import (
    DesktopSessionNotFoundError,
    DesktopSessionStore,
    DesktopSessionValidationError,
)


class _TickClock:
    def __init__(self) -> None:
        self._value = datetime(2026, 8, 31, 12, 0, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        value = self._value
        self._value += timedelta(microseconds=1)
        return value


def _id_factory(*values: str):
    iterator = iter(values)
    return iterator.__next__


def _paths(tmp_path: Path) -> tuple[Path, Path]:
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    return workspace, tmp_path / "state"


def _store(
    tmp_path: Path,
    *,
    ids: tuple[str, ...] = ("session-a", "session-b", "session-c"),
    clock: _TickClock | None = None,
    **kwargs: Any,
) -> DesktopSessionStore:
    workspace, state = _paths(tmp_path)
    return DesktopSessionStore(
        workspace_root=workspace,
        state_dir=state,
        session_id_factory=_id_factory(*ids),
        clock=clock or _TickClock(),
        **kwargs,
    )


def test_session_and_generation_modes_are_distinct_and_survive_restart(
    tmp_path: Path,
) -> None:
    store = _store(
        tmp_path,
        default_session_mode="production",
        default_generation_mode="deep",
    )
    first = store.current_session()
    assert first.session_mode == "production"
    assert first.generation_mode == "deep"

    turn = store.append_user("Build the deployable agent")
    assert turn.session_mode == "production"
    assert turn.generation_mode == "deep"

    workspace, state = _paths(tmp_path)
    restarted = DesktopSessionStore(workspace_root=workspace, state_dir=state)
    restored = restarted.current_session()
    assert restored.session_id == first.session_id
    assert restored.session_mode == "production"
    assert restored.generation_mode == "deep"
    assert [item.text for item in restored.turns] == ["Build the deployable agent"]


def test_set_modes_records_mode_snapshot_per_turn(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.current_session()
    first = store.append_user("one")
    store.set_modes(session_mode="production", generation_mode="deep")
    second = store.append_assistant("two")
    store.set_modes(generation_mode="fast")
    third = store.append_tool("three")

    assert (first.session_mode, first.generation_mode) == ("interactive", "fast")
    assert (second.session_mode, second.generation_mode) == ("production", "deep")
    assert (third.session_mode, third.generation_mode) == ("production", "fast")
    current = store.current_session()
    assert current.session_mode == "production"
    assert current.generation_mode == "fast"


def test_new_archives_current_while_clear_keeps_current_id(tmp_path: Path) -> None:
    store = _store(tmp_path)
    original = store.current_session()
    store.append_user("Keep this archived transcript")

    fresh = store.new_session(session_mode="production", generation_mode="deep")
    assert fresh.session_id != original.session_id
    assert fresh.turns == ()
    archived = store.get_session(original.session_id)
    assert archived.is_current is False
    assert archived.archived_at is not None
    assert [turn.text for turn in archived.turns] == ["Keep this archived transcript"]

    store.append_user("Clear only this transcript")
    cleared = store.clear_current()
    assert cleared.session_id == fresh.session_id
    assert cleared.session_mode == "production"
    assert cleared.generation_mode == "deep"
    assert cleared.turns == ()
    assert cleared.dropped_turns == 0
    assert store.get_session(original.session_id).turns == archived.turns


def test_append_can_target_archived_session_for_in_flight_run(tmp_path: Path) -> None:
    store = _store(tmp_path)
    original = store.current_session()
    store.append_user("slow request", session_id=original.session_id)
    current = store.new_session()

    stored = store.append_assistant(
        "slow result",
        session_id=original.session_id,
        provenance={"run_id": "run-1"},
    )

    assert stored.text == "slow result"
    assert [turn.text for turn in store.get_session(original.session_id).turns] == [
        "slow request",
        "slow result",
    ]
    assert store.current_session().session_id == current.session_id
    assert store.current_session().turns == ()


def test_list_and_resume_support_latest_and_exact_ids(tmp_path: Path) -> None:
    clock = _TickClock()
    store = _store(tmp_path, clock=clock)
    first = store.current_session()
    store.append_user("first prompt")
    second = store.new_session()
    store.append_user("second prompt")

    rows = store.list_sessions()
    assert [row.session_id for row in rows] == [second.session_id, first.session_id]
    assert rows[0].is_current is True
    assert rows[0].first_user_preview == "second prompt"
    assert rows[1].turn_count == 1

    latest = store.resume_session("latest")
    assert latest.session_id == second.session_id
    resumed = store.resume_session(first.session_id)
    assert resumed.session_id == first.session_id
    assert resumed.is_current is True
    assert store.get_session(second.session_id).is_current is False
    with pytest.raises(DesktopSessionNotFoundError):
        store.resume_session("missing-session")


def test_all_roles_round_trip_as_export_ready_transcript(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.append_user("user line")
    store.append_assistant("assistant line")
    store.append_system("system line")
    store.append_tool("tool line", provenance={"step": 2})

    snapshot = store.current_session()
    assert [turn.role for turn in snapshot.turns] == [
        "user",
        "assistant",
        "system",
        "tool",
    ]
    rows = store.transcript()
    assert [row["role"] for row in rows] == [
        "user",
        "assistant",
        "system",
        "tool",
    ]
    assert rows[0]["mode"] == "interactive"
    assert rows[0]["generation_mode"] == "fast"
    assert rows[-1]["provenance"] == {"step": 2}


def test_turn_and_session_bounds_drop_oldest_atomically(tmp_path: Path) -> None:
    store = _store(
        tmp_path,
        max_turns=3,
        max_turn_chars=8,
        max_session_chars=16,
    )
    store.append_user("111111111111")
    store.append_assistant("22222222")
    store.append_system("33333333")
    store.append_tool("44444444")

    snapshot = store.current_session()
    assert [(turn.sequence, turn.text) for turn in snapshot.turns] == [
        (3, "33333333"),
        (4, "44444444"),
    ]
    assert snapshot.dropped_turns == 2


def test_append_redacts_text_and_projects_provenance_safely(tmp_path: Path) -> None:
    store = _store(tmp_path)
    workspace, _state = _paths(tmp_path)
    secret = "opaque-secret-value"
    inside = workspace / "artifacts" / "result.json"
    outside = tmp_path / "outside.txt"

    turn = store.append_assistant(
        f"Authorization: Bearer {secret}; api_key={secret}",
        supplied_secrets=(secret,),
        provenance={
            "api_key": secret,
            "nested": {"message": f"token={secret}"},
            "inside": inside,
            "outside": outside,
            "blob": b"private bytes",
            "object": object(),
        },
    )

    assert secret not in turn.text
    assert turn.provenance["api_key"] == "[redacted]"
    assert secret not in str(turn.provenance)
    assert turn.provenance["inside"] == "artifacts/result.json"
    assert turn.provenance["outside"] == "[external-path]"
    assert turn.provenance["blob"] == "[bytes:13]"
    assert turn.provenance["object"] == "<object>"
    assert secret.encode() not in store.database_path.read_bytes()


def test_oversized_provenance_degrades_to_explicit_marker(tmp_path: Path) -> None:
    store = _store(tmp_path, max_provenance_bytes=64)
    turn = store.append_tool("done", provenance={"body": "x" * 1_000})
    assert turn.provenance == {"_truncated": True}
    assert store.current_session().turns[0].provenance == {"_truncated": True}


def test_append_rolls_back_when_post_insert_step_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path)
    store.append_user("committed")
    original = store._prune_turns_locked

    def fail_after_insert(*_args: Any, **_kwargs: Any) -> None:
        raise RuntimeError("synthetic failure")

    monkeypatch.setattr(store, "_prune_turns_locked", fail_after_insert)
    with pytest.raises(RuntimeError, match="synthetic failure"):
        store.append_assistant("must roll back")
    monkeypatch.setattr(store, "_prune_turns_locked", original)

    assert [turn.text for turn in store.current_session().turns] == ["committed"]
    following = store.append_assistant("next")
    assert following.sequence == 2


def test_concurrent_appends_have_unique_monotonic_sequences(tmp_path: Path) -> None:
    store = _store(tmp_path, max_turns=64)
    store.current_session()
    with ThreadPoolExecutor(max_workers=8) as executor:
        turns = list(executor.map(store.append_tool, [f"event-{i}" for i in range(32)]))

    assert sorted(turn.sequence for turn in turns) == list(range(1, 33))
    snapshot = store.current_session()
    assert [turn.sequence for turn in snapshot.turns] == list(range(1, 33))


def test_workspace_scope_and_injected_state_path_are_isolated(tmp_path: Path) -> None:
    state = tmp_path / "user-state"
    first_workspace = tmp_path / "one"
    second_workspace = tmp_path / "two"
    first_workspace.mkdir()
    second_workspace.mkdir()
    first = DesktopSessionStore(
        workspace_root=first_workspace,
        state_dir=state,
        session_id_factory=lambda: "same-id",
    )
    second = DesktopSessionStore(
        workspace_root=second_workspace,
        state_dir=state,
        session_id_factory=lambda: "same-id",
    )

    first.append_user("workspace one")
    second.append_user("workspace two")
    assert first.database_path != second.database_path
    assert state in first.database_path.parents
    assert [turn.text for turn in first.current_session().turns] == ["workspace one"]
    assert [turn.text for turn in second.current_session().turns] == ["workspace two"]
    assert str(first_workspace) not in str(first.database_path)


def test_max_sessions_retains_current_and_newest_archives(tmp_path: Path) -> None:
    store = _store(
        tmp_path,
        ids=("session-a", "session-b", "session-c", "session-d"),
        max_sessions=2,
    )
    first = store.current_session()
    second = store.new_session()
    third = store.new_session()

    assert [row.session_id for row in store.list_sessions()] == [
        third.session_id,
        second.session_id,
    ]
    with pytest.raises(DesktopSessionNotFoundError):
        store.get_session(first.session_id)


@pytest.mark.parametrize("role", ["", "human", "User", None])
def test_invalid_roles_are_rejected_without_persistence(
    tmp_path: Path,
    role: Any,
) -> None:
    store = _store(tmp_path)
    with pytest.raises(DesktopSessionValidationError):
        store.append_turn(role, "body")
    assert store.current_session().turns == ()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("session_mode", "fast"),
        ("session_mode", "ad-hoc"),
        ("generation_mode", "production"),
        ("generation_mode", "ultra"),
    ],
)
def test_modes_are_strict_and_cannot_be_conflated(
    tmp_path: Path,
    field: str,
    value: Any,
) -> None:
    kwargs = {field: value}
    with pytest.raises(DesktopSessionValidationError):
        DesktopSessionStore(
            workspace_root=_paths(tmp_path)[0],
            state_dir=_paths(tmp_path)[1],
            **{f"default_{field}": value},
        )

    store = _store(tmp_path)
    with pytest.raises(DesktopSessionValidationError):
        store.set_modes(**kwargs)


def test_invalid_ids_limits_and_text_fail_closed(tmp_path: Path) -> None:
    store = _store(tmp_path)
    with pytest.raises(DesktopSessionValidationError):
        store.resume_session("../escape")
    with pytest.raises(DesktopSessionValidationError):
        store.get_session("")
    with pytest.raises(DesktopSessionValidationError):
        store.list_sessions(limit=0)
    with pytest.raises(DesktopSessionValidationError):
        store.append_turn("user", object())  # type: ignore[arg-type]
    with pytest.raises(DesktopSessionValidationError):
        store.set_modes()


def test_to_dict_returns_detached_json_safe_values(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.append_tool("ok", provenance={"nested": {"value": 1}})
    payload = store.current_session().to_dict()
    payload["turns"][0]["provenance"]["nested"]["value"] = 99

    assert store.current_session().turns[0].provenance == {"nested": {"value": 1}}
