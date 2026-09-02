"""验证最小 IPC 网关只暴露任务、事务与 Evidence 闭环。"""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import cast

import pytest
from pydantic import JsonValue

from rivet.contracts.ipc import IpcRequest
from rivet.contracts.transactions import AcceptanceSpec
from rivet.ipc.command_application import (
    TUI_METHODS,
    CommandExecution,
    CommandWorkerApplication,
)
from rivet.ipc.worker import EmitEvent, WorkerMethodError
from rivet.transaction.hashing import acceptance_sha256

Event = tuple[str, dict[str, JsonValue]]
Runner = Callable[[tuple[str, ...], EmitEvent], Awaitable[CommandExecution]]


def _request(
    method: str,
    params: dict[str, JsonValue] | None = None,
    *,
    request_id: str = "request_test",
) -> IpcRequest:
    return IpcRequest(
        request_id=request_id,
        method=method,
        params=params or {},
    )


def _event_sink(events: list[Event]) -> EmitEvent:
    async def emit(event_type: str, payload: dict[str, JsonValue]) -> None:
        events.append((event_type, payload))

    return emit


def _json_execution(
    payload: dict[str, object],
    *,
    return_code: int = 0,
) -> CommandExecution:
    return CommandExecution(
        return_code,
        json.dumps(payload, ensure_ascii=False).encode(),
        b"",
    )


def _fix_proposal(
    goal: str,
    scope: list[str],
    *,
    read_scope: list[str] | None = None,
    allowed_new_paths: list[str] | None = None,
    investigation: str = "parse_port 的边界检查缺少负数分支。",
) -> dict[str, object]:
    specification = AcceptanceSpec(
        acceptance_id="acceptance_proposal_one",
        user_goal=goal,
        baseline_reproduction=(("pytest", "tests/test_parser.py", "-q"),),
        read_scope=tuple(read_scope if read_scope is not None else scope),
        allowed_paths=tuple(scope),
        write_scope=tuple(scope),
        allowed_new_paths=tuple(allowed_new_paths or ()),
        forbidden_paths=(".rivet/project.toml", "tests/test_parser.py"),
        scope_reason="用户显式确认的最小读、写与新建范围",
        scope_source="explicit",
        expected_behaviors=(goal,),
        preserved_behaviors=("既有回归行为保持不变",),
        verification_commands=(("pytest", "-q"),),
        behavior_verification_commands=(("pytest", "tests/test_parser.py", "-q"),),
        max_wall_seconds=900,
        max_tokens=8_192,
        max_tool_calls=64,
    )
    return {
        "acceptance": specification.model_dump(mode="json"),
        "acceptance_sha256": acceptance_sha256(specification),
        "base_commit": "b" * 40,
        "confirmed": False,
        "investigation": investigation,
        "next_action": "审查提案后使用相同参数追加 --yes",
        "run_id": "run_proposal_one",
        "transaction_created": False,
    }


def test_ready_payload_is_read_only_and_exposes_only_minimal_startup_state(
    tmp_path: Path,
) -> None:
    (tmp_path / ".rivet").mkdir()
    (tmp_path / ".rivet" / "project.toml").write_text(
        """schema_version = 1
[rivet]
model = "deepseek-v4-flash"
[verification]
acceptance = [["python", "-m", "pytest", "-q"]]
regression = []
static = []
""",
        encoding="utf-8",
    )
    application = CommandWorkerApplication(
        tmp_path,
        environment={"DEEPSEEK_API_KEY": "private-value"},
    )

    payload = application.ready_payload()

    assert payload["acceptance_ready"] is True
    assert payload["credential_configured"] is True
    assert payload["model"] == "deepseek-v4-flash"
    assert "private-value" not in json.dumps(payload)
    assert set(payload) == {
        "acceptance_action",
        "acceptance_ready",
        "acceptance_reason",
        "branch",
        "credential_configured",
        "model",
        "models",
        "repository",
    }


@pytest.mark.asyncio
async def test_method_surface_is_exact_and_removed_methods_fail_closed(
    tmp_path: Path,
) -> None:
    async def runner(_argv: tuple[str, ...], _emit: EmitEvent) -> CommandExecution:
        raise AssertionError("removed method must not reach the CLI")

    application = CommandWorkerApplication(tmp_path, environment={}, runner=runner)
    assert {
        "command.abort",
        "command.apply",
        "command.ask",
        "command.diff",
        "command.fix",
        "command.verify",
        "evidence.get",
        "evidence.log",
        "permission.resolve",
        "transactions.list",
        "workspace.files",
    } == TUI_METHODS
    removed = (
        "command.benchmark",
        "command.clean",
        "command.config",
        "command.doctor",
        "command.export",
        "command.modules",
        "command.plan",
        "command.read",
        "command.resume",
        "command.trace",
        "config.get",
        "context.search",
        "module.list",
        "sessions.list",
        "trace.query",
        "worker.snapshot",
    )
    for index, method in enumerate(removed):
        with pytest.raises(WorkerMethodError) as captured:
            await application.handle(
                _request(method, request_id=f"request_removed_{index}"),
                emit=_event_sink([]),
                cancel_event=asyncio.Event(),
            )
        assert captured.value.code == "ipc.method_unknown"


@pytest.mark.asyncio
async def test_ask_routes_context_to_formal_cli_and_redacts_result(
    tmp_path: Path,
) -> None:
    source = tmp_path / "src" / "app.py"
    source.parent.mkdir()
    source.write_text("print('ok')\n", encoding="utf-8")
    observed_argv: list[tuple[str, ...]] = []

    async def runner(argv: tuple[str, ...], _emit: EmitEvent) -> CommandExecution:
        observed_argv.append(argv)
        return _json_execution(
            {
                "answer": "answer contains private-value",
                "run_id": "run_example",
                "session_id": "session_removed",
                "status": "ANSWERED",
            }
        )

    events: list[Event] = []
    application = CommandWorkerApplication(
        tmp_path,
        environment={"DEEPSEEK_API_KEY": "private-value"},
        runner=runner,
    )
    result = cast(
        dict[str, JsonValue],
        await application.handle(
            _request(
                "command.ask",
                {
                    "context_paths": ["src/app.py"],
                    "model": "deepseek-v4-flash",
                    "query": "解释入口",
                },
            ),
            emit=_event_sink(events),
            cancel_event=asyncio.Event(),
        ),
    )

    argv = observed_argv[0]
    assert argv[:3] == (sys.executable, "-m", "rivet")
    assert "--model" in argv
    assert argv[-2] == "ask"
    assert "@src/app.py" in argv[-1]
    assert result["answer"] == "answer contains [REDACTED]"
    assert "session_id" not in result
    assert [event_type for event_type, _ in events] == [
        "command.started",
        "context.selected",
        "agent.answered",
        "command.completed",
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method", "params", "expected_tail"),
    [
        ("command.diff", {}, ("diff",)),
        ("command.diff", {"transaction_id": "tx_one"}, ("diff", "tx_one")),
        ("command.verify", {}, ("verify",)),
        (
            "command.verify",
            {"transaction_id": "tx_one"},
            ("verify", "tx_one"),
        ),
        ("command.apply", {"transaction_id": "tx_one"}, ("apply", "tx_one")),
        ("command.abort", {"transaction_id": "tx_one"}, ("abort", "tx_one")),
    ],
)
async def test_transaction_commands_do_not_require_provider_credentials(
    tmp_path: Path,
    method: str,
    params: dict[str, JsonValue],
    expected_tail: tuple[str, ...],
) -> None:
    observed: list[tuple[str, ...]] = []

    async def runner(argv: tuple[str, ...], _emit: EmitEvent) -> CommandExecution:
        observed.append(argv)
        return _json_execution({"status": "ok"})

    application = CommandWorkerApplication(tmp_path, environment={}, runner=runner)
    await application.handle(
        _request(method, params),
        emit=_event_sink([]),
        cancel_event=asyncio.Event(),
    )

    assert observed[0][-len(expected_tail) :] == expected_tail
    assert "DEEPSEEK_API_KEY" not in application._environment  # pyright: ignore[reportPrivateUsage]


@pytest.mark.asyncio
async def test_fix_runs_read_only_proposal_then_replays_identical_confirmed_argv(
    tmp_path: Path,
) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("pass\n", encoding="utf-8")
    (tmp_path / "src" / "context.py").write_text("pass\n", encoding="utf-8")
    new_path = tmp_path / "src" / "generated.py"
    proposal_payload = _fix_proposal(
        "修复解析器",
        ["src/app.py", "src/generated.py"],
        read_scope=["src/app.py", "src/context.py"],
        allowed_new_paths=["src/generated.py"],
    )
    proposal_hash = cast(str, proposal_payload["acceptance_sha256"])
    events: list[Event] = []
    observed: list[tuple[str, ...]] = []

    async def runner(argv: tuple[str, ...], _emit: EmitEvent) -> CommandExecution:
        observed.append(argv)
        if len(observed) == 1:
            return _json_execution(proposal_payload)
        return _json_execution(
            {
                "acceptance_sha256": proposal_hash,
                "answer": "补丁已验证",
                "base_commit": "b" * 40,
                "changed_files": ["src/app.py"],
                "changed_symbols": ["legacy_symbol"],
                "evidence_id": "evidence_one",
                "manifest_sha256": "sha256:" + "m" * 64,
                "model_status": "READY_FOR_VERIFICATION",
                "patch_sha256": "sha256:" + "p" * 64,
                "session_id": "session_removed",
                "status": "PASSED",
                "transaction_id": "tx_one",
                "verification_results": [{"kind": "BEHAVIOR", "status": "PASSED"}],
            }
        )

    application = CommandWorkerApplication(tmp_path, environment={}, runner=runner)
    task = asyncio.create_task(
        application.handle(
            _request(
                "command.fix",
                {
                    "allowed_new_paths": ["src/generated.py"],
                    "context_paths": ["src/context.py"],
                    "model": "deepseek-v4-flash",
                    "query": "修复解析器",
                    "write_scope": ["src/app.py"],
                },
            ),
            emit=_event_sink(events),
            cancel_event=asyncio.Event(),
        )
    )
    for _ in range(100):
        permission = next(
            (
                payload
                for event_type, payload in events
                if event_type == "permission.requested"
            ),
            None,
        )
        if permission is not None:
            break
        await asyncio.sleep(0)
    else:
        raise AssertionError("permission request was not emitted")
    assert len(observed) == 1
    assert observed[0][-8:] == (
        "fix",
        "修复解析器",
        "--allow-read",
        "src/context.py",
        "--allow-write",
        "src/app.py",
        "--allow-new",
        "src/generated.py",
    )
    assert "--yes" not in observed[0]
    proposed = next(
        payload for event_type, payload in events if event_type == "acceptance.proposed"
    )
    assert proposed["confirmed"] is False
    assert proposed["transaction_created"] is False
    assert proposed["acceptance_sha256"] == proposal_hash
    assert proposed["base_commit"] == "b" * 40
    assert proposed["investigation"] == "parse_port 的边界检查缺少负数分支。"
    assert permission["goal"] == "修复解析器"
    assert permission["read_scope"] == ["src/app.py", "src/context.py"]
    assert permission["write_scope"] == ["src/app.py", "src/generated.py"]
    assert permission["allowed_new_paths"] == ["src/generated.py"]
    assert permission["forbidden_paths"] == [
        ".rivet/project.toml",
        "tests/test_parser.py",
    ]
    assert permission["expected_behaviors"] == ["修复解析器"]
    assert permission["preserved_behaviors"] == ["既有回归行为保持不变"]
    assert permission["budgets"] == {
        "max_cost_usd": None,
        "max_tokens": 8192,
        "max_tool_calls": 64,
        "max_wall_seconds": 900,
    }
    assert permission["acceptance_commands"] == [
        ["pytest", "tests/test_parser.py", "-q"]
    ]
    assert permission["regression_commands"] == [["pytest", "-q"]]
    assert permission["investigation"] == "parse_port 的边界检查缺少负数分支。"
    assert permission["acceptance_sha256"] == proposal_hash
    assert permission["base_commit"] == "b" * 40
    assert "src/app.py" in cast(str, permission["argv"])
    assert "--allow-read src/context.py" in cast(str, permission["argv"])
    assert "--allow-new src/generated.py" in cast(str, permission["argv"])
    assert not new_path.exists()
    assert "candidate" not in json.dumps(permission).casefold()
    permission_id = cast(str, permission["request_id"])
    await application.handle(
        _request(
            "permission.resolve",
            {"approved": True, "request_id": permission_id},
            request_id="request_permission_resolution",
        ),
        emit=_event_sink(events),
        cancel_event=asyncio.Event(),
    )
    result = cast(dict[str, JsonValue], await task)

    assert len(observed) == 2
    assert observed[1][:-5] == observed[0]
    assert observed[1][-5:] == (
        "--yes",
        "--acceptance-sha256",
        proposal_hash,
        "--base-commit",
        "b" * 40,
    )
    assert "candidate_only" not in result
    assert "changed_symbols" not in result
    assert "session_id" not in result
    evidence = next(
        payload for event_type, payload in events if event_type == "evidence.published"
    )
    assert evidence["base_commit"] == "b" * 40
    assert "changed_symbols" not in evidence


@pytest.mark.asyncio
async def test_fix_binding_mismatch_after_permission_propagates_without_transaction(
    tmp_path: Path,
) -> None:
    scope = tmp_path / "src" / "app.py"
    scope.parent.mkdir()
    scope.write_text("before proposal\n", encoding="utf-8")
    observed: list[tuple[str, ...]] = []

    async def runner(argv: tuple[str, ...], _emit: EmitEvent) -> CommandExecution:
        observed.append(argv)
        if len(observed) == 1:
            return _json_execution(_fix_proposal("修复", ["src/app.py"]))
        assert scope.read_text(encoding="utf-8") == "changed while waiting\n"
        return CommandExecution(
            5,
            b"",
            json.dumps(
                {
                    "error": {
                        "code": "acceptance.binding_mismatch",
                        "summary": "提案后仓库基线已改变",
                        "next_action": "重新调查并确认新的 AcceptanceSpec",
                    }
                },
                ensure_ascii=False,
            ).encode(),
        )

    events: list[Event] = []
    application = CommandWorkerApplication(tmp_path, environment={}, runner=runner)
    task = asyncio.create_task(
        application.handle(
            _request(
                "command.fix",
                {
                    "context_paths": ["src/app.py"],
                    "query": "修复",
                    "write_scope": ["src/app.py"],
                },
            ),
            emit=_event_sink(events),
            cancel_event=asyncio.Event(),
        )
    )
    while not any(event_type == "permission.requested" for event_type, _ in events):
        await asyncio.sleep(0)
    permission = next(
        payload
        for event_type, payload in events
        if event_type == "permission.requested"
    )
    scope.write_text("changed while waiting\n", encoding="utf-8")
    await application.handle(
        _request(
            "permission.resolve",
            {
                "approved": True,
                "request_id": cast(str, permission["request_id"]),
            },
            request_id="request_permission_stale_binding",
        ),
        emit=_event_sink(events),
        cancel_event=asyncio.Event(),
    )

    with pytest.raises(WorkerMethodError) as mismatch:
        await task
    assert mismatch.value.code == "acceptance.binding_mismatch"
    assert len(observed) == 2
    assert observed[1][:-5] == observed[0]
    assert observed[1][-5:] == (
        "--yes",
        "--acceptance-sha256",
        cast(str, _fix_proposal("修复", ["src/app.py"])["acceptance_sha256"]),
        "--base-commit",
        "b" * 40,
    )
    assert not any(
        event_type
        in {"transaction.started", "verification.completed", "evidence.published"}
        for event_type, _ in events
    )


@pytest.mark.asyncio
async def test_fix_denial_runs_only_proposal_and_never_confirms_transaction(
    tmp_path: Path,
) -> None:
    scope = tmp_path / "src" / "app.py"
    scope.parent.mkdir()
    scope.write_text("pass\n", encoding="utf-8")
    observed: list[tuple[str, ...]] = []

    async def runner(argv: tuple[str, ...], _emit: EmitEvent) -> CommandExecution:
        observed.append(argv)
        if len(observed) > 1:
            raise AssertionError("拒绝后不得运行确认阶段")
        return _json_execution(_fix_proposal("修复", ["src/app.py"]))

    application = CommandWorkerApplication(tmp_path, environment={}, runner=runner)
    events: list[Event] = []
    task = asyncio.create_task(
        application.handle(
            _request(
                "command.fix",
                {
                    "context_paths": ["src/app.py"],
                    "query": "修复",
                    "write_scope": ["src/app.py"],
                },
            ),
            emit=_event_sink(events),
            cancel_event=asyncio.Event(),
        )
    )
    while not any(event_type == "permission.requested" for event_type, _ in events):
        await asyncio.sleep(0)
    permission = next(
        payload
        for event_type, payload in events
        if event_type == "permission.requested"
    )
    await application.handle(
        _request(
            "permission.resolve",
            {
                "approved": False,
                "request_id": cast(str, permission["request_id"]),
            },
            request_id="request_permission_denied",
        ),
        emit=_event_sink(events),
        cancel_event=asyncio.Event(),
    )
    with pytest.raises(WorkerMethodError) as denied:
        await task
    assert denied.value.code == "guard.permission_denied"
    assert len(observed) == 1
    assert "--yes" not in observed[0]
    assert not any(event_type == "transaction.started" for event_type, _ in events)


@pytest.mark.asyncio
async def test_fix_can_authorize_only_a_nonexistent_new_path(tmp_path: Path) -> None:
    (tmp_path / "context.py").write_text("existing_context = True\n", encoding="utf-8")
    new_path = tmp_path / "generated.py"
    observed: list[tuple[str, ...]] = []

    async def runner(argv: tuple[str, ...], _emit: EmitEvent) -> CommandExecution:
        observed.append(argv)
        return _json_execution(
            _fix_proposal(
                "新增模块",
                ["generated.py"],
                read_scope=["context.py"],
                allowed_new_paths=["generated.py"],
            )
        )

    application = CommandWorkerApplication(tmp_path, environment={}, runner=runner)
    events: list[Event] = []
    task = asyncio.create_task(
        application.handle(
            _request(
                "command.fix",
                {
                    "allowed_new_paths": ["generated.py"],
                    "context_paths": ["context.py"],
                    "query": "新增模块",
                },
            ),
            emit=_event_sink(events),
            cancel_event=asyncio.Event(),
        )
    )
    while not any(event_type == "permission.requested" for event_type, _ in events):
        await asyncio.sleep(0)
    permission = next(
        payload
        for event_type, payload in events
        if event_type == "permission.requested"
    )

    assert observed[0][-6:] == (
        "fix",
        "新增模块",
        "--allow-read",
        "context.py",
        "--allow-new",
        "generated.py",
    )
    assert permission["read_scope"] == ["context.py"]
    assert permission["write_scope"] == ["generated.py"]
    assert permission["allowed_new_paths"] == ["generated.py"]
    assert not new_path.exists()
    await application.handle(
        _request(
            "permission.resolve",
            {
                "approved": False,
                "request_id": cast(str, permission["request_id"]),
            },
            request_id="request_new_only_denied",
        ),
        emit=_event_sink(events),
        cancel_event=asyncio.Event(),
    )
    with pytest.raises(WorkerMethodError, match="用户拒绝"):
        await task
    assert not new_path.exists()


@pytest.mark.asyncio
async def test_read_only_context_is_never_promoted_to_write_scope(
    tmp_path: Path,
) -> None:
    (tmp_path / "context.py").write_text("pass\n", encoding="utf-8")

    async def runner(_argv: tuple[str, ...], _emit: EmitEvent) -> CommandExecution:
        raise AssertionError("无 scope 不得开始只读调查")

    events: list[Event] = []
    application = CommandWorkerApplication(tmp_path, environment={}, runner=runner)
    with pytest.raises(WorkerMethodError) as captured:
        await application.handle(
            _request(
                "command.fix",
                {"context_paths": ["context.py"], "query": "修复"},
            ),
            emit=_event_sink(events),
            cancel_event=asyncio.Event(),
        )

    assert captured.value.code == "acceptance.write_scope_required"
    assert events == []


@pytest.mark.asyncio
async def test_removed_fix_escape_hatch_fails_before_proposal(tmp_path: Path) -> None:
    scope = tmp_path / "app.py"
    scope.write_text("pass\n", encoding="utf-8")

    async def runner(_argv: tuple[str, ...], _emit: EmitEvent) -> CommandExecution:
        raise AssertionError("未知参数不得到达 CLI")

    application = CommandWorkerApplication(tmp_path, environment={}, runner=runner)
    with pytest.raises(WorkerMethodError) as captured:
        await application.handle(
            _request(
                "command.fix",
                {
                    "candidate_only": True,
                    "context_paths": ["app.py"],
                    "query": "修复",
                    "write_scope": ["app.py"],
                },
            ),
            emit=_event_sink([]),
            cancel_event=asyncio.Event(),
        )
    assert captured.value.code == "ipc.params_invalid"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "malformation",
    [
        "confirmed",
        "transaction_created",
        "missing_field",
        "extra_field",
        "goal",
        "scope",
        "acceptance_commands",
        "run_id",
        "acceptance_sha256",
        "base_commit",
    ],
)
async def test_malformed_fix_proposal_fails_closed_before_permission(
    tmp_path: Path,
    malformation: str,
) -> None:
    scope = tmp_path / "app.py"
    scope.write_text("pass\n", encoding="utf-8")

    async def runner(_argv: tuple[str, ...], _emit: EmitEvent) -> CommandExecution:
        proposal = _fix_proposal("修复", ["app.py"])
        acceptance = cast(dict[str, object], proposal["acceptance"])
        if malformation == "confirmed":
            proposal["confirmed"] = True
        elif malformation == "transaction_created":
            proposal["transaction_created"] = True
        elif malformation == "missing_field":
            proposal.pop("investigation")
        elif malformation == "extra_field":
            proposal["transaction_id"] = "tx_forbidden"
        elif malformation == "goal":
            acceptance["user_goal"] = "另一个任务"
        elif malformation == "scope":
            acceptance["write_scope"] = ["other.py"]
        elif malformation == "acceptance_commands":
            acceptance["behavior_verification_commands"] = []
        elif malformation == "run_id":
            proposal["run_id"] = "invalid"
        elif malformation == "acceptance_sha256":
            proposal["acceptance_sha256"] = "a" * 64
        elif malformation == "base_commit":
            proposal["base_commit"] = "not-a-full-git-commit"
        return _json_execution(proposal)

    events: list[Event] = []
    application = CommandWorkerApplication(tmp_path, environment={}, runner=runner)
    with pytest.raises(WorkerMethodError) as captured:
        await application.handle(
            _request(
                "command.fix",
                {
                    "context_paths": ["app.py"],
                    "query": "修复",
                    "write_scope": ["app.py"],
                },
            ),
            emit=_event_sink(events),
            cancel_event=asyncio.Event(),
        )

    assert captured.value.code == "ipc.fix_proposal_invalid"
    assert not any(
        event_type in {"acceptance.proposed", "permission.requested"}
        for event_type, _ in events
    )


@pytest.mark.asyncio
async def test_failed_proposal_cli_never_requests_confirmation(tmp_path: Path) -> None:
    scope = tmp_path / "app.py"
    scope.write_text("pass\n", encoding="utf-8")

    async def runner(_argv: tuple[str, ...], _emit: EmitEvent) -> CommandExecution:
        return CommandExecution(
            5,
            b"",
            json.dumps(
                {
                    "error": {
                        "code": "acceptance.proposal_failed",
                        "next_action": "缩小调查范围",
                        "summary": "只读调查失败",
                    }
                }
            ).encode(),
        )

    events: list[Event] = []
    application = CommandWorkerApplication(tmp_path, environment={}, runner=runner)
    with pytest.raises(WorkerMethodError) as captured:
        await application.handle(
            _request(
                "command.fix",
                {
                    "context_paths": ["app.py"],
                    "query": "修复",
                    "write_scope": ["app.py"],
                },
            ),
            emit=_event_sink(events),
            cancel_event=asyncio.Event(),
        )

    assert captured.value.code == "acceptance.proposal_failed"
    assert not any(event_type == "permission.requested" for event_type, _ in events)


@pytest.mark.asyncio
async def test_unknown_cli_output_fields_are_not_forwarded(tmp_path: Path) -> None:
    async def runner(_argv: tuple[str, ...], _emit: EmitEvent) -> CommandExecution:
        return _json_execution(
            {"candidate_only": True, "private_extension": "x", "status": "PASSED"}
        )

    application = CommandWorkerApplication(tmp_path, environment={}, runner=runner)
    result = cast(
        dict[str, JsonValue],
        await application.handle(
            _request("command.ask", {"query": "hello"}),
            emit=_event_sink([]),
            cancel_event=asyncio.Event(),
        ),
    )

    assert result == {"status": "PASSED"}


@pytest.mark.asyncio
async def test_unknown_verification_kind_is_rejected_from_cli_output(
    tmp_path: Path,
) -> None:
    async def runner(_argv: tuple[str, ...], _emit: EmitEvent) -> CommandExecution:
        return _json_execution(
            {
                "status": "PASSED",
                "verification_results": [{"kind": "V10", "status": "PASSED"}],
            }
        )

    application = CommandWorkerApplication(tmp_path, environment={}, runner=runner)
    with pytest.raises(WorkerMethodError) as captured:
        await application.handle(
            _request("command.ask", {"query": "hello"}),
            emit=_event_sink([]),
            cancel_event=asyncio.Event(),
        )
    assert captured.value.code == "ipc.command_output_invalid"


@pytest.mark.asyncio
async def test_workspace_files_is_on_demand_git_aware_and_secret_safe(
    tmp_path: Path,
) -> None:
    subprocess.run(("git", "init", "-q", str(tmp_path)), check=True)
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("pass\n", encoding="utf-8")
    (tmp_path / "src" / "service.py").write_text("pass\n", encoding="utf-8")
    (tmp_path / ".env").write_text("SECRET=value\n", encoding="utf-8")
    (tmp_path / ".env.example").write_text("SECRET=\n", encoding="utf-8")
    (tmp_path / ".rivet").mkdir()
    (tmp_path / ".rivet" / "project.toml").write_text("", encoding="utf-8")
    (tmp_path / "link.py").symlink_to("src/app.py")
    events: list[Event] = []
    application = CommandWorkerApplication(tmp_path, environment={})

    result = cast(
        dict[str, JsonValue],
        await application.handle(
            _request("workspace.files", {"limit": 50, "query": ""}),
            emit=_event_sink(events),
            cancel_event=asyncio.Event(),
        ),
    )

    paths = cast(list[str], result["paths"])
    assert "src/app.py" in paths
    assert "src/service.py" in paths
    assert ".env.example" in paths
    assert ".env" not in paths
    assert "link.py" not in paths
    assert not any(path.startswith(".rivet/") for path in paths)
    assert [event_type for event_type, _ in events] == ["workspace.tree_updated"]


@pytest.mark.asyncio
async def test_transaction_queries_use_xdg_state_not_repository(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from rivet.transaction.store import TransactionStore

    state_home = tmp_path.parent / "state-home"
    cache_home = tmp_path.parent / "cache-home"
    observed_roots: list[Path] = []

    def list_recent_records(
        store: TransactionStore,
        *,
        limit: int = 20,
    ) -> tuple[()]:
        assert limit == 20
        observed_roots.append(store.state_root)
        return ()

    monkeypatch.setattr(TransactionStore, "list_recent_records", list_recent_records)
    application = CommandWorkerApplication(
        tmp_path,
        environment={
            "XDG_CACHE_HOME": str(cache_home),
            "XDG_STATE_HOME": str(state_home),
        },
    )
    result = await application.handle(
        _request("transactions.list"),
        emit=_event_sink([]),
        cancel_event=asyncio.Event(),
    )

    assert result == {"transactions": []}
    assert len(observed_roots) == 1
    assert observed_roots[0].is_relative_to(state_home / "rivet")
    assert observed_roots[0].name == "transactions"
    assert observed_roots[0] != tmp_path / ".rivet" / "transactions"


@pytest.mark.asyncio
async def test_evidence_queries_are_explicit_redacted_and_emit_events(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from rivet.verify.evidence_query import EvidenceQueryService

    def detail(
        _service: EvidenceQueryService,
        transaction_id: str,
    ) -> dict[str, JsonValue]:
        return {
            "summary": "private-value",
            "transaction_id": transaction_id,
        }

    def log(
        _service: EvidenceQueryService,
        transaction_id: str,
        *,
        step_id: str | None = None,
    ) -> dict[str, JsonValue]:
        return {
            "content": f"{step_id}: private-value",
            "transaction_id": transaction_id,
        }

    monkeypatch.setattr(EvidenceQueryService, "detail", detail)
    monkeypatch.setattr(EvidenceQueryService, "log", log)
    application = CommandWorkerApplication(
        tmp_path,
        environment={"DEEPSEEK_API_KEY": "private-value"},
    )
    events: list[Event] = []
    detail_result = await application.handle(
        _request("evidence.get", {"transaction_id": "tx_one"}),
        emit=_event_sink(events),
        cancel_event=asyncio.Event(),
    )
    log_result = await application.handle(
        _request(
            "evidence.log",
            {"step_id": "verification_behavior", "transaction_id": "tx_one"},
        ),
        emit=_event_sink(events),
        cancel_event=asyncio.Event(),
    )

    assert "private-value" not in json.dumps([detail_result, log_result])
    assert [event_type for event_type, _ in events] == [
        "evidence.snapshot",
        "evidence.log",
    ]


def test_trace_projection_preserves_demand_causality_and_redacts_again(
    tmp_path: Path,
) -> None:
    application = CommandWorkerApplication(
        tmp_path,
        environment={"DEEPSEEK_API_KEY": "private-value"},
    )
    raw = json.dumps(
        {
            "event": {
                "event_id": "event_one",
                "event_type": "module.activated",
                "input_summary": "private-value",
                "parent_event_id": "event_parent",
                "payload": {
                    "api_key": "private-value",
                    "demand_id": "demand_one",
                    "demand_source": "MODEL_TOOL_CALL",
                    "operation_id": "tool:one",
                    "parent_demand_id": "demand_root",
                    "stream_id": "stream_one",
                },
                "run_id": "run_one",
            }
        }
    ).encode()

    projected = application._project_trace_line(raw)  # pyright: ignore[reportPrivateUsage]

    assert projected is not None
    assert projected.stream_id == "stream_one"
    assert projected.payload["demand_id"] == "demand_one"
    assert projected.payload["parent_demand_id"] == "demand_root"
    assert projected.payload["parent_event_id"] == "event_parent"
    assert projected.payload["summary"] == "[REDACTED]"
    assert "api_key" not in projected.payload
    assert "stream_id" not in projected.payload


@pytest.mark.asyncio
async def test_cli_error_is_classified_without_forwarding_stderr(
    tmp_path: Path,
) -> None:
    async def runner(_argv: tuple[str, ...], _emit: EmitEvent) -> CommandExecution:
        return CommandExecution(
            5,
            b"",
            json.dumps(
                {
                    "error": {
                        "code": "guard.denied",
                        "next_action": "review scope",
                        "summary": "permission denied",
                    },
                    "private": "private-value",
                }
            ).encode(),
        )

    application = CommandWorkerApplication(tmp_path, environment={}, runner=runner)
    with pytest.raises(WorkerMethodError) as captured:
        await application.handle(
            _request("command.apply", {"transaction_id": "tx_one"}),
            emit=_event_sink([]),
            cancel_event=asyncio.Event(),
        )

    assert captured.value.code == "guard.denied"
    assert captured.value.summary == "permission denied"
    assert "private-value" not in str(captured.value)
