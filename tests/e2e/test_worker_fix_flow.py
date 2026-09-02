"""用无网络 Fake Provider 跑通 Worker 到 Apply 的真实两阶段闭环。"""

from __future__ import annotations

import asyncio
import io
import json
import os
import shutil
import subprocess
import sys
from collections.abc import Awaitable, Callable, Sequence
from contextlib import redirect_stderr, redirect_stdout
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import cast

import pytest
from pydantic import JsonValue

from rivet.cli.application import run_cli
from rivet.contracts.ipc import IpcRequest
from rivet.contracts.messages import AssistantMessage
from rivet.contracts.provider import (
    ModelFinishReason,
    ModelRequest,
    ModelResponse,
    TokenUsage,
)
from rivet.contracts.tools import ToolCall
from rivet.ipc.command_application import (
    CommandExecution,
    CommandWorkerApplication,
)
from rivet.ipc.worker import EmitEvent
from rivet.kernel.module_api import ModuleActivationContext
from rivet.kernel.resources import ResourceScope
from rivet.trace.paths import RuntimePaths

NOW = datetime(2026, 9, 2, tzinfo=UTC)
Event = tuple[str, dict[str, JsonValue]]


class _FakeProvider:
    """按固定程序化响应工作，不打开 socket。"""

    def __init__(self, responses: Sequence[ModelResponse]) -> None:
        self._responses = list(responses)
        self.requests: list[ModelRequest] = []

    async def complete(
        self,
        request: ModelRequest,
        *,
        on_text_delta: Callable[[str], Awaitable[None]] | None = None,
    ) -> ModelResponse:
        self.requests.append(request)
        response = self._responses.pop(0)
        if on_text_delta is not None and response.message.content:
            await on_text_delta(response.message.content)
        return response


def _response(
    *,
    content: str = "",
    tool_calls: tuple[ToolCall, ...] = (),
    finish_reason: ModelFinishReason = ModelFinishReason.STOP,
) -> ModelResponse:
    return ModelResponse(
        provider_id="fake",
        model="deepseek-v4-flash",
        message=AssistantMessage(
            content=content,
            tool_calls=tool_calls,
            created_at=NOW,
        ),
        finish_reason=finish_reason,
        usage=TokenUsage(
            prompt_tokens=1,
            completion_tokens=1,
            total_tokens=2,
            cost_usd=Decimal("0"),
        ),
    )


def _git(repository: Path, *arguments: str) -> None:
    subprocess.run(
        ("git", *arguments),
        cwd=repository,
        check=True,
        capture_output=True,
        env={
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        },
    )


def _repository(tmp_path: Path) -> Path:
    repository = tmp_path / "repository"
    repository.mkdir()
    _git(repository, "init", "-q", "-b", "main")
    _git(repository, "config", "user.name", "Fixture")
    _git(repository, "config", "user.email", "fixture@example.invalid")
    (repository / "calc.py").write_text(
        "def answer():\n    return 1\n",
        encoding="utf-8",
    )
    (repository / "acceptance.py").write_text(
        "from calc import answer\nraise SystemExit(0 if answer() == 2 else 1)\n",
        encoding="utf-8",
    )
    (repository / ".rivet").mkdir()
    acceptance_command = json.dumps(
        [[sys.executable, "acceptance.py"]],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    (repository / ".rivet" / "project.toml").write_text(
        "\n".join(
            (
                "schema_version = 1",
                "[rivet]",
                'model = "deepseek-v4-flash"',
                "[verification]",
                f"acceptance = {acceptance_command}",
                "regression = []",
                "static = []",
                "",
            )
        ),
        encoding="utf-8",
    )
    _git(repository, "add", "--", ".rivet/project.toml", "acceptance.py", "calc.py")
    _git(repository, "commit", "-qm", "baseline")
    return repository


def _request(
    method: str,
    params: dict[str, JsonValue],
    *,
    request_id: str,
) -> IpcRequest:
    return IpcRequest(request_id=request_id, method=method, params=params)


def _event_sink(events: list[Event]) -> EmitEvent:
    async def emit(event_type: str, payload: dict[str, JsonValue]) -> None:
        events.append((event_type, payload))

    return emit


def _in_process_runner(
    environment: dict[str, str],
) -> Callable[[tuple[str, ...], EmitEvent], Awaitable[CommandExecution]]:
    async def run(arguments: tuple[str, ...], _emit: EmitEvent) -> CommandExecution:
        def invoke() -> CommandExecution:
            stdout = io.StringIO()
            stderr = io.StringIO()
            with redirect_stdout(stdout), redirect_stderr(stderr):
                return_code = run_cli(arguments[3:], environment=environment)
            return CommandExecution(
                return_code=return_code,
                stdout=stdout.getvalue().encode("utf-8"),
                stderr=stderr.getvalue().encode("utf-8"),
            )

        return await asyncio.to_thread(invoke)

    return run


@pytest.mark.asyncio
async def test_worker_two_stage_fix_uses_real_cli_and_applies_verified_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = _repository(tmp_path)
    bwrap = shutil.which("bwrap")
    if bwrap is None:
        pytest.skip("integration environment has no bubblewrap")
    environment = {
        "DEEPSEEK_API_KEY": "fake-provider-key",
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "RIVET_BWRAP_PATH": bwrap,
        "XDG_CACHE_HOME": str(tmp_path / "cache"),
        "XDG_STATE_HOME": str(tmp_path / "state"),
    }
    monkeypatch.setenv("RIVET_BWRAP_PATH", bwrap)
    investigation = _FakeProvider(
        (
            _response(
                tool_calls=(
                    ToolCall(
                        tool_call_id="call_investigate",
                        tool_name="context_search",
                        arguments={"query": "answer return", "max_results": 4},
                    ),
                ),
                finish_reason=ModelFinishReason.TOOL_CALLS,
            ),
            _response(content="根因在 calc.py；最小写范围仅 calc.py。"),
        )
    )
    patching = _FakeProvider(
        (
            _response(
                tool_calls=(
                    ToolCall(
                        tool_call_id="call_patch",
                        tool_name="file_replace",
                        arguments={
                            "path": "calc.py",
                            "old_text": "return 1",
                            "new_text": "return 2",
                            "expected_count": 1,
                        },
                    ),
                ),
                finish_reason=ModelFinishReason.TOOL_CALLS,
            ),
            _response(content="候选补丁已生成，等待独立验证。"),
        )
    )
    providers = [investigation, patching]

    def create_provider(
        context: ModuleActivationContext,
        scope: ResourceScope,
    ) -> _FakeProvider:
        del context, scope
        return providers.pop(0)

    from rivet.modules import factories

    monkeypatch.setattr(factories, "_create_deepseek_provider", create_provider)
    events: list[Event] = []
    application = CommandWorkerApplication(
        repository,
        environment=environment,
        runner=_in_process_runner(environment),
    )
    fix_task = asyncio.create_task(
        application.handle(
            _request(
                "command.fix",
                {
                    "context_paths": ["calc.py"],
                    "model": "deepseek-v4-flash",
                    "query": "让 answer 返回 2",
                    "write_scope": ["calc.py"],
                },
                request_id="request_fix_real",
            ),
            emit=_event_sink(events),
            cancel_event=asyncio.Event(),
        )
    )
    for _ in range(1_000):
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
        await asyncio.sleep(0.001)
    else:
        raise AssertionError("real proposal did not request permission")

    paths = RuntimePaths.for_repository(repository, environment=environment)
    assert not paths.transactions_root.exists()
    assert not paths.worktrees_root.exists()
    assert (repository / "calc.py").read_text(encoding="utf-8").endswith("return 1\n")
    await application.handle(
        _request(
            "permission.resolve",
            {
                "approved": True,
                "request_id": cast(str, permission["request_id"]),
            },
            request_id="request_fix_permission_real",
        ),
        emit=_event_sink(events),
        cancel_event=asyncio.Event(),
    )
    fix_result = cast(dict[str, JsonValue], await fix_task)

    assert fix_result["state"] == "VERIFIED"
    assert fix_result["evidence_verified"] is True
    assert providers == []
    assert len(investigation.requests) == 2
    assert len(patching.requests) == 2
    assert (repository / "calc.py").read_text(encoding="utf-8").endswith("return 1\n")
    assert any(event_type == "acceptance.proposed" for event_type, _ in events)
    assert any(event_type == "verification.completed" for event_type, _ in events)
    assert any(event_type == "evidence.published" for event_type, _ in events)
    transaction_id = cast(str, fix_result["transaction_id"])
    apply_result = cast(
        dict[str, JsonValue],
        await application.handle(
            _request(
                "command.apply",
                {"transaction_id": transaction_id},
                request_id="request_apply_real",
            ),
            emit=_event_sink(events),
            cancel_event=asyncio.Event(),
        ),
    )

    assert apply_result["state"] == "APPLIED"
    assert (repository / "calc.py").read_text(encoding="utf-8").endswith("return 2\n")
    await application.close()
