"""验证会话、Run、事务和 Provider checkpoint 的保守恢复。"""

from __future__ import annotations

from pathlib import Path

import pytest

from rivet.storage.sessions import (
    PendingToolCall,
    SessionCheckpoint,
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
    )
    store.save(checkpoint)

    first = store.resume(checkpoint.session_id)
    second = store.resume(checkpoint.session_id)

    assert first == checkpoint
    assert second == checkpoint


def test_session_store_rejects_tamper_and_unknown_id(tmp_path: Path) -> None:
    store = SessionStore(tmp_path)
    with pytest.raises(KeyError):
        store.load("session_missing")

    sessions = tmp_path / ".rivet" / "sessions"
    sessions.mkdir(parents=True)
    (sessions / "session_tampered.json").write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError, match="checkpoint"):
        store.load("session_tampered")
