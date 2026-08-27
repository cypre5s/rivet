"""验证工具注册、capability 唯一绑定和模型/TUI 视图。"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from pydantic import BaseModel, ConfigDict

from rivet.contracts.tools import ToolCall, ToolDefinition
from rivet.kernel.agent_tools import AgentTool
from rivet.kernel.resources import ResourceScope
from rivet.tools.paths import WorkspaceBoundary
from rivet.tools.registry import ToolInvocationContext
from rivet.tools.toolset import WORKSPACE_TOOL_NAMES, build_workspace_tool_registry
from rivet.trace.paths import RuntimePaths
from rivet.trace.store import TraceStore


class StrictArguments(BaseModel):
    """提供最小严格工具输入。"""

    model_config = ConfigDict(extra="forbid", strict=True)


class LooseArguments(BaseModel):
    """制造缺少 extra forbid 的非法工具输入。"""


async def _plain_executor(arguments: BaseModel) -> str:
    """提供不执行副作用的普通工具函数。"""
    return arguments.model_dump_json()


async def _call_executor(call: ToolCall, arguments: BaseModel) -> str:
    """提供保留 call id 的无副作用工具函数。"""
    return call.tool_call_id + arguments.model_dump_json()


def _definition() -> ToolDefinition:
    return ToolDefinition(
        name="test.registry",
        description="测试定义",
        input_schema={"type": "object", "properties": {}},
    )


async def _start_trace(tmp_path: Path, repository: Path) -> TraceStore:
    """为每个实际 Registry 调用启动真实本地 Trace。"""
    paths = RuntimePaths.for_repository(
        repository,
        environment={"XDG_CACHE_HOME": str(tmp_path / "cache")},
    )
    trace = TraceStore(paths)
    await trace.start()
    return trace


def test_agent_tool_rejects_ambiguous_or_loose_executor_contract() -> None:
    definition = _definition()

    with pytest.raises(ValueError, match="一个执行器"):
        AgentTool(definition=definition, input_model=StrictArguments)
    with pytest.raises(ValueError, match="一个执行器"):
        AgentTool(
            definition=definition,
            input_model=StrictArguments,
            executor=_plain_executor,
            call_executor=_call_executor,
        )
    with pytest.raises(ValueError, match="extra"):
        AgentTool.from_model(
            name="test.registry",
            description="测试定义",
            input_model=LooseArguments,
            executor=_plain_executor,
        )
    with pytest.raises(ValueError, match="extra"):
        AgentTool.from_call_model(
            definition=definition,
            input_model=LooseArguments,
            executor=_call_executor,
        )


@pytest.mark.asyncio
async def test_registry_exposes_every_phase_five_tool_and_unique_capability(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repository"
    transaction = tmp_path / "transaction"
    repository.mkdir()
    transaction.mkdir()
    scope = ResourceScope("tools.registry")
    registry = build_workspace_tool_registry(
        WorkspaceBoundary(repository, transaction), scope=scope
    )

    assert registry.names == WORKSPACE_TOOL_NAMES
    assert len(registry.capabilities) == len(WORKSPACE_TOOL_NAMES)
    assert len(registry.definitions) == len(WORKSPACE_TOOL_NAMES)
    assert registry.resolve_capability("file.read_text").definition.name == (
        "file.read_text"
    )
    await scope.close()


@pytest.mark.asyncio
async def test_registry_records_trace_and_keeps_model_view_smaller(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repository"
    transaction = tmp_path / "transaction"
    repository.mkdir()
    transaction.mkdir()
    trace = await _start_trace(tmp_path, repository)
    scope = ResourceScope("tools.trace")
    registry = build_workspace_tool_registry(
        WorkspaceBoundary(repository, transaction),
        scope=scope,
        model_preview_chars=32,
        tui_preview_chars=256,
    )
    call = ToolCall(
        tool_call_id="call_process_trace",
        tool_name="process.run",
        arguments={
            "argv": [sys.executable, "-c", "print('x' * 200)"],
            "timeout_seconds": 2.0,
        },
    )
    context = ToolInvocationContext(
        run_id="run_tool_trace",
        session_id="session_tool_trace",
        trace=trace,
    )

    view = await registry.invoke(call, context=context)
    replay = trace.replay("run_tool_trace")

    assert view.result.success
    assert view.result.output.stdout_truncated
    assert len(view.model_text) < len(view.tui_text)
    assert view.output_capture.stdout.artifact.size_bytes == 201
    assert [record.event.event_type for record in replay.events] == [
        "tool.started",
        "tool.completed",
    ]
    await trace.close()
    await scope.close()


@pytest.mark.asyncio
async def test_registry_redacts_secret_from_all_views_and_artifact(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repository"
    transaction = tmp_path / "transaction"
    repository.mkdir()
    transaction.mkdir()
    trace = await _start_trace(tmp_path, repository)
    scope = ResourceScope("tools.redaction")
    registry = build_workspace_tool_registry(
        WorkspaceBoundary(repository, transaction), scope=scope
    )
    secret = "sk-" + ("z" * 32)
    call = ToolCall(
        tool_call_id="call_process_redaction",
        tool_name="process.run",
        arguments={
            "argv": [sys.executable, "-c", f"print({secret!r})"],
            "timeout_seconds": 2.0,
        },
    )
    context = ToolInvocationContext(
        run_id="run_tool_redaction",
        session_id="session_tool_redaction",
        trace=trace,
    )

    view = await registry.invoke(call, context=context)
    artifact_path = trace.paths.runtime_root / (
        view.output_capture.stdout.artifact.path
    )

    assert secret not in view.model_text
    assert secret not in view.tui_text
    assert secret not in view.result.model_dump_json()
    assert secret not in artifact_path.read_text(encoding="utf-8")
    assert artifact_path.is_file()
    await trace.close()
    await scope.close()


@pytest.mark.asyncio
async def test_process_tool_can_write_transaction_but_not_main_root(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repository"
    transaction = tmp_path / "transaction"
    repository.mkdir()
    transaction.mkdir()
    scope = ResourceScope("tools.process.transaction")
    trace = await _start_trace(tmp_path, repository)
    registry = build_workspace_tool_registry(
        WorkspaceBoundary(repository, transaction), scope=scope
    )
    call = ToolCall(
        tool_call_id="call_process_write",
        tool_name="process.run",
        arguments={
            "argv": [
                sys.executable,
                "-c",
                "from pathlib import Path; Path('created.txt').write_text('ok')",
            ],
            "timeout_seconds": 2.0,
        },
    )
    context = ToolInvocationContext(
        run_id="run_process_write",
        session_id="session_process_write",
        trace=trace,
    )

    view = await registry.invoke(call, context=context)

    assert view.result.success
    assert (transaction / "created.txt").read_text(encoding="utf-8") == "ok"
    assert not (repository / "created.txt").exists()
    await trace.close()
    await scope.close()


@pytest.mark.asyncio
async def test_registry_adapts_to_agent_loop_tool_contract(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    transaction = tmp_path / "transaction"
    repository.mkdir()
    transaction.mkdir()
    (repository / "sample.txt").write_text("content", encoding="utf-8")
    scope = ResourceScope("tools.agent.adapter")
    trace = await _start_trace(tmp_path, repository)
    registry = build_workspace_tool_registry(
        WorkspaceBoundary(repository, transaction), scope=scope
    )
    context = ToolInvocationContext(
        run_id="run_tool_adapter",
        session_id="session_tool_adapter",
        trace=trace,
    )
    agent_tool = next(
        tool
        for tool in registry.agent_tools(context=context)
        if tool.definition.name == "file.read_text"
    )

    observation = await agent_tool.execute(
        ToolCall(
            tool_call_id="call_read_adapter",
            tool_name="file.read_text",
            arguments={"path": "sample.txt"},
        )
    )

    assert "content" in observation
    await trace.close()
    await scope.close()


@pytest.mark.asyncio
async def test_registry_rejects_unknown_tool_and_extra_arguments(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repository"
    transaction = tmp_path / "transaction"
    repository.mkdir()
    transaction.mkdir()
    scope = ResourceScope("tools.validation")
    trace = await _start_trace(tmp_path, repository)
    registry = build_workspace_tool_registry(
        WorkspaceBoundary(repository, transaction), scope=scope
    )
    context = ToolInvocationContext(
        run_id="run_tool_validation",
        session_id="session_tool_validation",
        trace=trace,
    )

    unknown = await registry.invoke(
        ToolCall(
            tool_call_id="call_unknown_tool",
            tool_name="missing.tool",
            arguments={},
        ),
        context=context,
    )
    extra = await registry.invoke(
        ToolCall(
            tool_call_id="call_extra_argument",
            tool_name="workspace.info",
            arguments={"unexpected": True},
        ),
        context=context,
    )
    event_types = [
        record.event.event_type for record in trace.replay("run_tool_validation").events
    ]

    assert not unknown.result.success
    assert unknown.result.error is not None
    assert unknown.result.error.code == "tool.unknown"
    assert not extra.result.success
    assert extra.result.error is not None
    assert extra.result.error.code == "tool.validation_failed"
    assert event_types == [
        "tool.started",
        "tool.failed",
        "tool.started",
        "tool.failed",
    ]
    await trace.close()
    await scope.close()
