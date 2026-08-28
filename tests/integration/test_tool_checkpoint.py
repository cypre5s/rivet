"""验证工具副作用执行前后的耐久状态与保守恢复。"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import BaseModel, ConfigDict

from rivet.contracts.guard import (
    AuthorizationDecision,
    AuthorizationStatus,
    Permission,
    PermissionScope,
)
from rivet.contracts.tools import SideEffectClass, ToolCall, ToolExecutionStatus
from rivet.storage.sessions import (
    PendingToolCall,
    SessionCheckpoint,
    SessionStatus,
    SessionStore,
)
from rivet.tools.registry import (
    RawToolOutput,
    RegisteredTool,
    ToolCheckpointTransition,
    ToolInvocationContext,
    ToolRegistry,
)
from rivet.trace.paths import RuntimePaths
from rivet.trace.store import TraceStore

NOW = datetime(2026, 8, 28, tzinfo=UTC)


class _WriteArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    path: str


@pytest.mark.parametrize(
    ("side_effect", "next_action"),
    (
        (SideEffectClass.READ_ONLY, "RETRY"),
        (SideEffectClass.TRANSACTIONAL_WRITE, "ABORT"),
        (SideEffectClass.LOCAL_PROCESS, "ABORT"),
        (SideEffectClass.EXTERNAL_SIDE_EFFECT, "ABORT"),
    ),
)
def test_executing_tool_recovers_as_unknown_without_assuming_success(
    tmp_path: Path,
    side_effect: SideEffectClass,
    next_action: str,
) -> None:
    store = SessionStore(tmp_path)
    store.save(
        SessionCheckpoint(
            session_id="session_durable_tool",
            run_id="run_durable_tool",
            transaction_id="tx_durable_tool",
            command="fix",
            query="修改 tracked.txt",
            status=SessionStatus.RUNNING,
            pending_tools=(
                PendingToolCall(
                    tool_call_id="call_durable_tool",
                    run_id="run_durable_tool",
                    session_id="session_durable_tool",
                    transaction_id="tx_durable_tool",
                    tool_name="file.write_transaction",
                    arguments_hash="sha256:" + ("a" * 64),
                    side_effect_class=side_effect,
                    status=ToolExecutionStatus.EXECUTING,
                    started_at=NOW,
                    retry_policy="NEVER_AUTOMATIC",
                ),
            ),
        )
    )

    recovered = store.resume("session_durable_tool")
    tool = recovered.pending_tools[0]

    assert tool.status is ToolExecutionStatus.UNKNOWN
    assert tool.next_action == next_action
    assert tool.result_hash is None
    assert recovered.status is SessionStatus.INTERRUPTED


def test_completed_tool_checkpoint_retains_result_hash_and_observation(
    tmp_path: Path,
) -> None:
    store = SessionStore(tmp_path)
    completed = PendingToolCall(
        tool_call_id="call_completed_tool",
        run_id="run_completed_tool",
        session_id="session_completed_tool",
        tool_name="file.read_text",
        arguments_hash="sha256:" + ("b" * 64),
        side_effect_class=SideEffectClass.READ_ONLY,
        status=ToolExecutionStatus.COMPLETED,
        started_at=NOW,
        completed_at=NOW,
        result_hash="sha256:" + ("c" * 64),
        result_text="最新内容",
        retry_policy="AUTO_REPLAY_READ_ONLY",
    )
    store.save(
        SessionCheckpoint(
            session_id="session_completed_tool",
            run_id="run_completed_tool",
            command="ask",
            query="读取文件",
            status=SessionStatus.RUNNING,
            pending_tools=(completed,),
        )
    )

    recovered = store.resume("session_completed_tool")

    assert recovered.pending_tools == (completed,)
    assert recovered.pending_tools[0].status is ToolExecutionStatus.COMPLETED


@pytest.mark.asyncio
async def test_registry_checkpoints_every_state_before_returning_observation(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    trace = TraceStore(
        RuntimePaths.for_repository(
            repository,
            environment={"XDG_CACHE_HOME": str(tmp_path / "cache")},
        )
    )
    await trace.start()
    transitions: list[ToolCheckpointTransition] = []

    async def handler(arguments: BaseModel) -> RawToolOutput:
        values = _WriteArguments.model_validate(arguments.model_dump())
        (repository / values.path).write_text("once\n", encoding="utf-8")
        return RawToolOutput(stdout=b"written")

    async def checkpoint(transition: ToolCheckpointTransition) -> None:
        transitions.append(transition)

    registry = ToolRegistry(
        authorizer=lambda _request: AuthorizationDecision(
            status=AuthorizationStatus.ALLOWED,
            code="guard.test_allowed",
            summary="测试批准",
        )
    )
    registry.register(
        RegisteredTool.from_model(
            name="file.fixture_write",
            capability_id="file.fixture_write",
            description="写入一次",
            input_model=_WriteArguments,
            handler=handler,
            permission=Permission.WRITE,
            permission_scope=PermissionScope.SPECIFIC_PATHS,
            path_argument="path",
        )
    )
    context = ToolInvocationContext(
        run_id="run_checkpoint_order",
        session_id="session_checkpoint_order",
        transaction_id="tx_checkpoint_order",
        trace=trace,
        checkpoint=checkpoint,
    )

    view = await registry.invoke(
        ToolCall(
            tool_call_id="call_checkpoint_order",
            tool_name="file.fixture_write",
            arguments={"path": "result.txt"},
        ),
        context=context,
    )

    assert view.result.success
    assert [transition.status for transition in transitions] == [
        ToolExecutionStatus.PREPARED,
        ToolExecutionStatus.AUTHORIZED,
        ToolExecutionStatus.EXECUTING,
        ToolExecutionStatus.COMPLETED,
    ]
    assert transitions[-1].result_hash is not None
    assert transitions[-1].side_effect_class is SideEffectClass.TRANSACTIONAL_WRITE
    assert (repository / "result.txt").read_text(encoding="utf-8") == "once\n"
    await trace.close()
