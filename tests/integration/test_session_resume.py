"""验证会话、Run、事务和 Provider checkpoint 的保守恢复。"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from rivet.storage.sessions import (
    PendingToolCall,
    SessionCheckpoint,
    SessionStage,
    SessionStatus,
    SessionStore,
    ToolRecoveryStatus,
)


def test_interrupted_tool_is_unknown_and_requires_explicit_retry(
    tmp_path: Path,
) -> None:
    store = SessionStore(tmp_path)
    checkpoint = SessionCheckpoint(
        session_id="session_resume_one",
        run_id="run_resume_one",
        transaction_id="tx_resume_one",
        command="fix",
        query="修复问题",
        status=SessionStatus.RUNNING,
        provider_state={"reasoning_content": "opaque"},
        pending_tools=(
            PendingToolCall(
                tool_call_id="call_resume_one",
                tool_name="file.write_transaction",
                status=ToolRecoveryStatus.RUNNING,
            ),
        ),
    )
    store.save(checkpoint)

    recovered = store.resume("session_resume_one")

    assert recovered.status is SessionStatus.INTERRUPTED
    assert recovered.pending_tools[0].status is ToolRecoveryStatus.UNKNOWN
    assert recovered.pending_tools[0].next_action == "RETRY"
    assert recovered.provider_state == {"reasoning_content": "opaque"}
    assert recovered.transaction_id == "tx_resume_one"


def test_completed_session_resume_is_idempotent(tmp_path: Path) -> None:
    store = SessionStore(tmp_path)
    checkpoint = SessionCheckpoint(
        session_id="session_resume_done",
        run_id="run_resume_done",
        command="ask",
        query="解释",
        status=SessionStatus.COMPLETED,
        stage=SessionStage.TERMINAL,
    )
    store.save(checkpoint)

    first = store.resume(checkpoint.session_id)
    second = store.resume(checkpoint.session_id)

    assert first == checkpoint
    assert second == checkpoint


def test_session_store_roundtrips_messages_and_budget_for_continuation(
    tmp_path: Path,
) -> None:
    from datetime import UTC, datetime

    from rivet.contracts.messages import UserMessage

    checkpoint = SessionCheckpoint(
        session_id="session_resume_history",
        run_id="run_resume_history",
        command="ask",
        query="继续分析",
        status=SessionStatus.INTERRUPTED,
        model="deepseek-v4-pro",
        messages=(
            UserMessage(
                content="继续分析",
                created_at=datetime(2026, 8, 28, tzinfo=UTC),
            ),
        ),
        round_count=2,
        tool_call_count=3,
        prompt_tokens=100,
        completion_tokens=20,
    )
    store = SessionStore(tmp_path)
    store.save(checkpoint)

    loaded = store.load(checkpoint.session_id)

    assert loaded.messages == checkpoint.messages
    assert loaded.round_count == 2
    assert loaded.tool_call_count == 3
    assert loaded.prompt_tokens + loaded.completion_tokens == 120


def test_session_store_rejects_tamper_and_unknown_id(tmp_path: Path) -> None:
    store = SessionStore(tmp_path)
    with pytest.raises(KeyError):
        store.load("session_missing")

    sessions = tmp_path / ".rivet" / "sessions"
    sessions.mkdir(parents=True)
    (sessions / "session_tampered.json").write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError, match="checkpoint"):
        store.load("session_tampered")


def test_session_store_rejects_symlink_state_directory(tmp_path: Path) -> None:
    external = tmp_path / "external"
    external.mkdir()
    (tmp_path / ".rivet").symlink_to(external, target_is_directory=True)
    store = SessionStore(tmp_path)

    with pytest.raises(ValueError, match="符号链接"):
        store.load("session_missing")
    with pytest.raises(ValueError, match="符号链接"):
        store.save(
            SessionCheckpoint(
                session_id="session_symlink",
                run_id="run_symlink",
                command="ask",
                query="检查链接",
                status=SessionStatus.RUNNING,
            )
        )


def test_session_store_lists_only_valid_recent_checkpoints(tmp_path: Path) -> None:
    store = SessionStore(tmp_path)
    for session_id in ("session_older", "session_newer"):
        store.save(
            SessionCheckpoint(
                session_id=session_id,
                run_id=f"run_{session_id}",
                command="ask",
                query="解释",
                status=SessionStatus.COMPLETED,
            )
        )
    sessions = tmp_path / ".rivet" / "sessions"
    os.utime(sessions / "session_older.json", ns=(1, 1))
    os.utime(sessions / "session_newer.json", ns=(2, 2))
    (sessions / "session_broken.json").write_text("{}", encoding="utf-8")

    assert store.list_recent_ids() == ("session_newer", "session_older")
    assert store.list_recent_ids(limit=1) == ("session_newer",)
    with pytest.raises(ValueError, match="上限"):
        store.list_recent_ids(limit=0)
