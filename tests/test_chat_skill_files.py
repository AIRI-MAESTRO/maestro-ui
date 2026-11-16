"""Tests for ChatScreen's document-skill file helpers — the chat-side of the
"a chain that reads a file ran without one" guard (care.skill_file_inputs)."""

from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from care.screens.chat import ChatScreen, StageOutcome


class TestTaskHasInlineFile:
    def test_plain_task_has_no_inline_file(self):
        assert ChatScreen._task_has_inline_file("summarise the document") is False

    def test_inlined_file_block_detected(self):
        # _read_file_ref wraps @path content in a <file …> block.
        task = 'do it\n<file path="/x/a.txt">\n```\nbody\n```\n</file>\n'
        assert ChatScreen._task_has_inline_file(task) is True

    def test_inlined_image_block_detected(self):
        task = 'caption this\n<image path="/x/a.png" mime="image/png" size="1">'
        assert ChatScreen._task_has_inline_file(task) is True


class TestExtractSkillFileText:
    def test_plain_text(self, tmp_path: Path):
        f = tmp_path / "notes.txt"
        f.write_text("hello world", encoding="utf-8")
        assert ChatScreen._extract_skill_file_text(str(f)) == "hello world"

    def test_docx(self, tmp_path: Path):
        from docx import Document

        f = tmp_path / "report.docx"
        d = Document()
        d.add_paragraph("Quarterly revenue up 20 percent.")
        d.save(str(f))
        out = ChatScreen._extract_skill_file_text(str(f))
        assert out is not None
        assert "Quarterly revenue up 20 percent." in out

    def test_missing_file_returns_none(self):
        assert ChatScreen._extract_skill_file_text("/no/such/file-xyz.docx") is None


class TestGenerationAttachmentContract:
    def test_private_body_safe_projection_and_manifest(self, tmp_path: Path):
        source = tmp_path / "private notes.txt"
        source.write_text("account secret: 12345", encoding="utf-8")
        task = f'Summarise @"{source}" and cite the conclusions.'

        screen = ChatScreen.__new__(ChatScreen)
        screen._post_file_ref_attached = lambda _path: None  # type: ignore[method-assign]

        resolved = screen._resolve_file_refs(task)
        safe_task = screen._safe_task_for_file_refs(task)
        manifest = screen._context_files_for_resolved_task(task, resolved)

        assert "account secret: 12345" in resolved
        assert str(source) in resolved
        assert "account secret: 12345" not in safe_task
        assert str(source) not in safe_task
        assert "[attached file: private notes.txt]" in safe_task
        assert manifest == [
            {
                "path": str(source),
                "sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
                "size_bytes": source.stat().st_size,
                "mime_type": "text/plain",
            }
        ]
        persisted = screen._context_files_for_persistence(manifest)
        assert persisted[0]["path"] == "private notes.txt"
        assert str(tmp_path) not in str(persisted)
        assert persisted[0]["sha256"] == manifest[0]["sha256"]

    def test_unresolved_ref_is_not_added_to_manifest(self, tmp_path: Path):
        missing = tmp_path / "missing.csv"
        task = f'Analyse @"{missing}"'
        screen = ChatScreen.__new__(ChatScreen)

        assert screen._context_files_for_resolved_task(task, task) == []

    def test_session_artifact_origin_accepts_only_safe_task(self):
        captured: dict[str, Any] = {}

        class _Store:
            def append_chain(self, **kwargs: Any) -> Any:
                captured.update(kwargs)
                return SimpleNamespace(id="artifact-1")

        screen = ChatScreen()
        screen._generation_private_context = True
        screen.artifact_store = _Store()  # type: ignore[assignment]
        result = SimpleNamespace(
            chain_dict={"name": "safe", "steps": [{"number": 1}]},
            suggested_display_name="Safe chain",
            suggested_description="",
        )

        artifact_id = screen._stash_generation_artifact(
            safe_task="Analyze [attached file: private notes.txt]",
            iteration=1,
            result=result,
        )

        assert artifact_id == "artifact-1"
        assert captured["origin"]["user_query"] == (
            "Analyze [attached file: private notes.txt]"
        )
        assert captured["origin"]["task"] == captured["origin"]["user_query"]
        assert captured["persist_to_disk"] is False

    def test_production_saves_safe_task_but_executes_private_task(self):
        screen = ChatScreen()
        screen._last_chain_action_payload = None
        screen._save_chain_production = AsyncMock(  # type: ignore[method-assign]
            return_value=("chain-1", "Chain")
        )
        screen._record_production_baseline = AsyncMock(  # type: ignore[method-assign]
            return_value="run-1"
        )
        screen._kickoff_evolution = MagicMock()  # type: ignore[method-assign]
        screen._update_pipeline_stage = MagicMock()  # type: ignore[method-assign]

        async def _resolve(
            _stage: Any, _policy: Any, *, deps_ok: bool, execute: Any, **_kwargs: Any
        ) -> StageOutcome:
            if not deps_ok:
                return StageOutcome.SKIPPED
            value = await execute()
            return StageOutcome.SKIPPED if value is False else StageOutcome.DONE

        screen._resolve_stage = _resolve  # type: ignore[method-assign]
        spec = SimpleNamespace(save="auto", baseline="auto", evolve="auto")
        private_task = "Analyze <file>TOP_SECRET</file>"
        safe_task = "Analyze [attached file: report.csv]"
        refs = [{"path": "report.csv", "sha256": "a" * 64, "size_bytes": 10}]

        asyncio.run(
            screen._drive_production_pipeline(
                spec,
                private_task=private_task,
                safe_task=safe_task,
                context_files=refs,
                mage_result=SimpleNamespace(chain_dict={"steps": []}),
            )
        )

        assert screen._save_chain_production.await_args.kwargs["task"] == safe_task
        assert screen._save_chain_production.await_args.kwargs["context_files"] == refs
        baseline_kwargs = screen._record_production_baseline.await_args.kwargs
        assert baseline_kwargs["task"] == private_task
        assert baseline_kwargs["persisted_task"] == safe_task

    def test_dedup_hash_includes_attachment_content_identity(self):
        safe_task = "Analyze [attached file: report.csv]"
        first = [{"path": "report.csv", "sha256": "a" * 64, "size_bytes": 10}]
        updated = [{"path": "report.csv", "sha256": "b" * 64, "size_bytes": 10}]

        assert ChatScreen._task_hash(
            safe_task, context_files=first
        ) != ChatScreen._task_hash(safe_task, context_files=updated)

    def test_interactive_save_uses_safe_task_and_fingerprints(self):
        screen = ChatScreen()
        private_task = "Analyze <file>TOP_SECRET</file>"
        safe_task = "Analyze [attached file: report.csv]"
        refs = [{"path": "report.csv", "sha256": "a" * 64, "size_bytes": 10}]
        screen._begin_chain_session(
            chain_dict={"name": "chain", "steps": [{"number": 1}]},
            display_name="Chain",
            task=private_task,
            safe_task=safe_task,
            context_files=refs,
        )
        assert screen._chain_session is not None
        payload = screen._chain_session["payload"]
        screen._persist_chain_to_memory = AsyncMock(  # type: ignore[method-assign]
            return_value="chain-1"
        )
        screen._fetch_latest_chain_version = AsyncMock(  # type: ignore[method-assign]
            return_value=("version-1", 1)
        )
        screen._mark_chain_artifact_saved = MagicMock()  # type: ignore[method-assign]

        asyncio.run(
            screen._save_chain_to_library(
                payload=payload,
                chain_dict=payload["chain_dict"],
                display_name="Chain",
            )
        )

        saved = screen._persist_chain_to_memory.await_args.kwargs
        assert saved["query"] == safe_task
        assert "TOP_SECRET" not in saved["query"]
        assert saved["context_files"] == refs
        assert screen._chain_session["task"] == private_task


class TestLtmAutosavePrivacy:
    def test_turn_gate_covers_attachments_and_untrusted_projection(self):
        assert not ChatScreen._ltm_autosave_private_context(
            generation_task="Explain recursion",
            safe_task="Explain recursion",
            context_files=[],
        )
        assert ChatScreen._ltm_autosave_private_context(
            generation_task="Analyze <file>TOP_SECRET</file>",
            safe_task="Analyze [attached file: report.csv]",
            context_files=[{"path": "report.csv"}],
        )
        assert ChatScreen._ltm_autosave_private_context(
            generation_task="Prior private conversation\n\nCurrent question",
            safe_task="Current question",
            context_files=[],
        )

    def test_private_turn_skips_before_answer_reaches_autosave_model(self):
        screen = ChatScreen.__new__(ChatScreen)
        screen._ltm_llm_complete = MagicMock(  # type: ignore[method-assign]
            side_effect=AssertionError("private answer reached LTM autosave")
        )

        asyncio.run(
            screen._maybe_save_ltm(
                query="Analyze [attached file: report.csv]",
                answer="TOP_SECRET derived answer",
                private_context=True,
            )
        )

        screen._ltm_llm_complete.assert_not_called()  # type: ignore[attr-defined]

    def test_plain_text_turn_still_autosaves(
        self,
        monkeypatch: Any,
    ):
        cfg = SimpleNamespace(
            context=SimpleNamespace(
                ltm_autosave=True,
                ltm_inject_max_chars=2000,
            )
        )
        fake_app = SimpleNamespace(config=cfg)
        monkeypatch.setattr(
            ChatScreen,
            "app",
            property(lambda _self: fake_app),
        )
        screen = ChatScreen.__new__(ChatScreen)
        complete = MagicMock()
        screen._ltm_llm_complete = MagicMock(  # type: ignore[method-assign]
            return_value=complete
        )
        ltm = object()
        save_from_turn = MagicMock(return_value=[])
        monkeypatch.setattr("care.memory_ltm.build_ltm", lambda _cfg: ltm)
        monkeypatch.setattr("care.memory_ltm.ltm_session_id", lambda _cfg: "s1")
        monkeypatch.setattr(
            "care.memory_ltm.recall_digest",
            lambda *_args, **_kwargs: "",
        )
        monkeypatch.setattr("care.memory_ltm.save_from_turn", save_from_turn)

        asyncio.run(
            screen._maybe_save_ltm(
                query="I prefer concise answers",
                answer="Understood.",
                private_context=False,
            )
        )

        assert save_from_turn.call_args.kwargs["query"] == "I prefer concise answers"
        assert save_from_turn.call_args.kwargs["answer"] == "Understood."
