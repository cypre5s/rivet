"""用本地录制 Provider 跑通固定、真实且不接触用户凭据的修复任务。"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from collections import deque
from collections.abc import Generator, Mapping, Sequence
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import cast

DEMO_TASK = (
    "修复 calculator.py：total_with_tax 应计算 subtotal * (1 + rate)，"
    "保留两位小数，并保持零金额和零税率行为"
)
RECORDED_TOOL_NAMES = (
    "workspace.info",
    "search.files",
    "file.read_text",
    "file.read_text",
    "process.run",
    "file.replace_transaction",
    "process.run",
    "git.diff",
)
ROOT = Path(__file__).parents[1]
FIXTURE_ROOT = ROOT / "demo" / "calculator-fix"
FIXED_GIT_ENVIRONMENT = {
    "GIT_AUTHOR_DATE": "2026-08-28T08:00:00+08:00",
    "GIT_COMMITTER_DATE": "2026-08-28T08:00:00+08:00",
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_TERMINAL_PROMPT": "0",
    "LANG": "C.UTF-8",
    "LC_ALL": "C.UTF-8",
}
SAFE_PARENT_ENVIRONMENT = frozenset({"LANG", "LC_ALL", "PATH", "TZ", "VIRTUAL_ENV"})
OLD_IMPLEMENTATION = '    return (subtotal + rate).quantize(Decimal("0.01"))'
NEW_IMPLEMENTATION = (
    '    return (subtotal * (Decimal("1") + rate)).quantize(Decimal("0.01"))'
)


class DemoError(RuntimeError):
    """表示固定演示没有满足真实闭环条件。"""


class _RecordedServer(ThreadingHTTPServer):
    """按固定顺序返回 SSE，并只记录非秘密计数。"""

    responses: deque[bytes]
    request_count: int

    def __init__(self) -> None:
        super().__init__(("127.0.0.1", 0), _RecordedHandler)
        self.responses = deque(_recorded_responses())
        self.request_count = 0

    @property
    def base_url(self) -> str:
        """返回只含回环地址和临时端口的根 URL。"""
        host = self.server_address[0]
        port = self.server_address[1]
        return f"http://{host}:{port}"


class _RecordedHandler(BaseHTTPRequestHandler):
    """只接受本机 Chat Completions 请求且不记录 Header。"""

    protocol_version = "HTTP/1.1"

    def do_POST(self) -> None:
        """验证最小请求形状并返回下一条固定 SSE。"""
        server = cast(_RecordedServer, self.server)
        try:
            length = int(self.headers.get("Content-Length", "0"))
            raw_body = self.rfile.read(length)
            raw_document = cast(object, json.loads(raw_body))
        except (ValueError, json.JSONDecodeError):
            self.send_error(400)
            return
        if (
            self.path != "/chat/completions"
            or not isinstance(raw_document, dict)
            or not self.headers.get("Authorization", "").startswith("Bearer ")
        ):
            self.send_error(400)
            return
        document = cast(dict[str, object], raw_document)
        if document.get("stream") is not True or not isinstance(
            document.get("messages"), list
        ):
            self.send_error(400)
            return
        if not server.responses:
            self.send_error(500)
            return
        response = server.responses.popleft()
        server.request_count += 1
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Content-Length", str(len(response)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(response)

    def log_message(self, format: str, *args: object) -> None:
        """禁止默认 HTTP 日志意外展示请求信息。"""
        del format, args


def materialize_demo_repository(destination: Path) -> Path:
    """复制固定模板并建立内容和提交时间均确定的 Git 基线。"""
    if destination.exists() or destination.is_symlink():
        raise DemoError("演示仓库目标必须尚不存在")
    shutil.copytree(FIXTURE_ROOT, destination)
    _run_git(destination, "init", "-q", "-b", "main")
    _run_git(
        destination,
        "add",
        "--",
        ".gitignore",
        ".rivet/project.toml",
        "calculator.py",
        "test_calculator.py",
    )
    _run_git(
        destination,
        "-c",
        "user.name=Rivet Demo",
        "-c",
        "user.email=demo@example.invalid",
        "commit",
        "-qm",
        "固定演示基线",
    )
    return destination


def run_release_demo(*, bwrap_path: Path) -> dict[str, object]:
    """执行基线、Agent 修复、证据审查、显式 apply 和最终验证。"""
    executable = bwrap_path.resolve(strict=True)
    if not executable.is_file() or not os.access(executable, os.X_OK):
        raise DemoError("bubblewrap 路径不是可执行普通文件")
    started_at = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="rivet-release-demo-") as raw_root:
        root = Path(raw_root)
        repository = materialize_demo_repository(root / "repository")
        environment = _demo_environment(root, executable)
        baseline = _run_command(
            ("/usr/bin/python3", "test_calculator.py"),
            cwd=repository,
            environment=environment,
        )
        if baseline.returncode != 1 or "FAIL" not in baseline.stdout:
            raise DemoError("固定演示基线没有稳定复现")
        modules = _run_cli(repository, environment, "modules")
        with _recorded_provider() as provider:
            environment["RIVET_BASE_URL"] = provider.base_url
            fix = _run_cli(
                repository,
                environment,
                "fix",
                DEMO_TASK,
                "--yes",
                timeout=180,
            )
        if provider.request_count != len(RECORDED_TOOL_NAMES) + 1:
            raise DemoError("录制 Provider 请求轮数不符合冻结脚本")
        transaction_id = _required_text(fix, "transaction_id")
        if fix.get("status") != "PASSED" or fix.get("apply_required") is not True:
            raise DemoError("固定演示补丁没有通过确定性验证")
        diff_result = _run_cli(repository, environment, "diff", transaction_id)
        patch = _required_text(diff_result, "diff")
        if OLD_IMPLEMENTATION not in patch or NEW_IMPLEMENTATION not in patch:
            raise DemoError("演示 Diff 不包含冻结修复")
        trace_index = _run_cli(repository, environment, "trace")
        raw_run_ids = trace_index.get("run_ids")
        if not isinstance(raw_run_ids, list):
            raise DemoError("演示 Trace 缺少唯一 run_id")
        run_ids = cast(list[object], raw_run_ids)
        if len(run_ids) != 1:
            raise DemoError("演示 Trace 缺少唯一 run_id")
        run_id = run_ids[0]
        if not isinstance(run_id, str):
            raise DemoError("演示 Trace run_id 无效")
        trace_replay = _run_cli(repository, environment, "trace", run_id)
        apply_result = _run_cli(repository, environment, "apply", transaction_id)
        final_test = _run_command(
            ("/usr/bin/python3", "test_calculator.py"),
            cwd=repository,
            environment=environment,
        )
        if final_test.returncode != 0 or "PASS" not in final_test.stdout:
            raise DemoError("apply 后的固定演示验证失败")
        final_source = (repository / "calculator.py").read_text(encoding="utf-8")
        if NEW_IMPLEMENTATION not in final_source:
            raise DemoError("apply 没有把冻结修复写回主工作区")
        status = _run_command(
            ("git", "status", "--porcelain"),
            cwd=repository,
            environment=environment,
        )
        return {
            "schema_version": 1,
            "mode": "offline_recorded_provider",
            "task": DEMO_TASK,
            "passed": True,
            "duration_seconds": round(time.monotonic() - started_at, 3),
            "baseline": {
                "exit_code": baseline.returncode,
                "summary": baseline.stdout.strip(),
            },
            "agent": {
                "answer": _required_text(fix, "answer"),
                "evidence_id": _required_text(fix, "evidence_id"),
                "provider_request_count": provider.request_count,
                "status": fix["status"],
                "tool_sequence": list(RECORDED_TOOL_NAMES),
                "transaction_id": transaction_id,
            },
            "modules": modules,
            "patch": patch,
            "trace": trace_replay,
            "apply": apply_result,
            "final_test": {
                "exit_code": final_test.returncode,
                "summary": final_test.stdout.strip(),
            },
            "main_worktree_status": status.stdout.splitlines(),
        }


@contextmanager
def _recorded_provider() -> Generator[_RecordedServer]:
    """在回环地址启动有界录制服务并保证线程退出。"""
    server = _RecordedServer()
    thread = threading.Thread(
        target=server.serve_forever,
        name="rivet-release-demo-provider",
        daemon=True,
    )
    thread.start()
    try:
        yield server
        if server.responses:
            raise DemoError("录制 Provider 仍有未消费响应")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        if thread.is_alive():
            raise DemoError("录制 Provider 线程未能退出")


def _recorded_responses() -> tuple[bytes, ...]:
    """返回上下文、基线、补丁、验证、Diff 和最终回答的固定轮次。"""
    calls: tuple[tuple[str, dict[str, object]], ...] = (
        ("workspace.info", {}),
        ("search.files", {"glob": "*.py", "max_results": 20}),
        ("file.read_text", {"path": "calculator.py"}),
        ("file.read_text", {"path": "test_calculator.py"}),
        (
            "process.run",
            {
                "argv": ["/usr/bin/python3", "test_calculator.py"],
                "cwd": ".",
                "timeout_seconds": 10.0,
            },
        ),
        (
            "file.replace_transaction",
            {
                "path": "calculator.py",
                "old_text": OLD_IMPLEMENTATION,
                "new_text": NEW_IMPLEMENTATION,
                "expected_count": 1,
            },
        ),
        (
            "process.run",
            {
                "argv": ["/usr/bin/python3", "test_calculator.py"],
                "cwd": ".",
                "timeout_seconds": 10.0,
            },
        ),
        ("git.diff", {}),
    )
    responses = tuple(
        _tool_sse(index, name, arguments)
        for index, (name, arguments) in enumerate(calls, start=1)
    )
    return (*responses, _final_sse(len(responses) + 1))


def _tool_sse(index: int, name: str, arguments: Mapping[str, object]) -> bytes:
    """编码一条完整 Tool Call SSE 响应。"""
    response_id = f"release-demo-{index}"
    delta: dict[str, object] = {
        "role": "assistant",
        "reasoning_content": f"recorded release step {index}",
        "tool_calls": [
            {
                "index": 0,
                "id": f"call_release_demo_{index}",
                "type": "function",
                "function": {
                    "name": name.replace(".", "_"),
                    "arguments": json.dumps(
                        arguments,
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ),
                },
            }
        ],
    }
    return _sse_bytes(response_id, delta=delta, finish_reason="tool_calls")


def _final_sse(index: int) -> bytes:
    """编码不夸大模型能力的最终回答。"""
    return _sse_bytes(
        f"release-demo-{index}",
        delta={
            "role": "assistant",
            "content": (
                "已在隔离 Worktree 修复含税计算并运行固定测试；"
                "请审查 Diff 与 Evidence 后再显式 apply。"
            ),
        },
        finish_reason="stop",
    )


def _sse_bytes(
    response_id: str,
    *,
    delta: Mapping[str, object],
    finish_reason: str,
) -> bytes:
    """按 DeepSeek 流协议生成增量、结束和 DONE 三个事件。"""
    prefix: dict[str, object] = {
        "id": response_id,
        "object": "chat.completion.chunk",
        "created": 1787850000,
        "model": "deepseek-v4-pro",
    }
    first: dict[str, object] = {
        **prefix,
        "choices": [{"index": 0, "delta": delta, "finish_reason": None}],
    }
    finish: dict[str, object] = {
        **prefix,
        "choices": [{"index": 0, "delta": {}, "finish_reason": finish_reason}],
        "usage": {
            "prompt_tokens": 10,
            "completion_tokens": 5,
            "total_tokens": 15,
            "completion_tokens_details": {"reasoning_tokens": 1},
        },
    }
    return (
        b"data: "
        + json.dumps(first, ensure_ascii=False, separators=(",", ":")).encode()
        + b"\n\ndata: "
        + json.dumps(finish, ensure_ascii=False, separators=(",", ":")).encode()
        + b"\n\ndata: [DONE]\n\n"
    )


def _demo_environment(root: Path, bwrap_path: Path) -> dict[str, str]:
    """构造不继承真实凭据且运行状态完全位于临时目录的环境。"""
    environment = {
        name: value
        for name, value in os.environ.items()
        if name in SAFE_PARENT_ENVIRONMENT
    }
    environment.update(
        {
            "DEEPSEEK_API_KEY": "offline-demo-placeholder",
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "PYTHONNOUSERSITE": "1",
            "RIVET_BWRAP_PATH": str(bwrap_path),
            "TZ": "UTC",
            "XDG_CACHE_HOME": str(root / "cache"),
            "XDG_CONFIG_HOME": str(root / "config"),
            "XDG_STATE_HOME": str(root / "state"),
        }
    )
    return environment


def _run_cli(
    repository: Path,
    environment: dict[str, str],
    *arguments: str,
    timeout: float = 60,
) -> dict[str, object]:
    """调用正式 JSON CLI 并拒绝任何未分类失败或非对象输出。"""
    completed = _run_command(
        (
            sys.executable,
            "-m",
            "rivet",
            "--repository",
            str(repository),
            "--json",
            *arguments,
        ),
        cwd=repository,
        environment=environment,
        timeout=timeout,
    )
    if completed.returncode != 0:
        error_code = "cli.unknown_failure"
        for error_stream in (completed.stdout, completed.stderr):
            try:
                error_payload = cast(object, json.loads(error_stream))
            except (json.JSONDecodeError, UnicodeError):
                continue
            if not isinstance(error_payload, dict):
                continue
            error_mapping = cast(dict[str, object], error_payload)
            raw_error = error_mapping.get("error")
            if not isinstance(raw_error, dict):
                continue
            raw_code = cast(dict[str, object], raw_error).get("code")
            if isinstance(raw_code, str) and raw_code:
                error_code = raw_code
                break
        raise DemoError(f"Rivet 演示命令失败：{arguments[0]}（{error_code}）")
    try:
        payload = cast(object, json.loads(completed.stdout))
    except (json.JSONDecodeError, UnicodeError) as error:
        raise DemoError(f"Rivet 演示命令输出无效：{arguments[0]}") from error
    if not isinstance(payload, dict):
        raise DemoError(f"Rivet 演示命令未返回对象：{arguments[0]}")
    return cast(dict[str, object], payload)


def _run_command(
    arguments: tuple[str, ...],
    *,
    cwd: Path,
    environment: dict[str, str],
    timeout: float = 30,
) -> subprocess.CompletedProcess[str]:
    """执行固定 argv，并以有界 UTF-8 文本保存演示事实。"""
    try:
        return subprocess.run(
            arguments,
            cwd=cwd,
            env=environment,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="strict",
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise DemoError("固定演示命令无法完成") from error


def _run_git(repository: Path, *arguments: str) -> None:
    """用固定身份和时间执行演示仓库 Git 命令。"""
    completed = subprocess.run(
        ("git", *arguments),
        cwd=repository,
        env={
            **FIXED_GIT_ENVIRONMENT,
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        },
        check=False,
        capture_output=True,
        timeout=20,
    )
    if completed.returncode != 0:
        raise DemoError("固定演示 Git 命令失败")


def _required_text(payload: dict[str, object], key: str) -> str:
    """读取演示 JSON 中不可为空的文本字段。"""
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise DemoError(f"演示结果字段 {key} 无效")
    return value


def _build_parser() -> argparse.ArgumentParser:
    """构造显式 bubblewrap 与可选结果文件参数。"""
    parser = argparse.ArgumentParser(description="运行 Rivet 固定发布演示")
    parser.add_argument(
        "--bwrap-path",
        type=Path,
        default=os.environ.get("RIVET_BWRAP_PATH") or shutil.which("bwrap"),
    )
    parser.add_argument(
        "--result",
        type=Path,
        default=ROOT / "artifacts" / "local" / "release-demo.json",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """运行演示并原子保存不含凭据和绝对临时路径的结果。"""
    try:
        arguments = _build_parser().parse_args(argv)
        bwrap_path = cast(Path | None, arguments.bwrap_path)
        result_path = cast(Path, arguments.result)
        if bwrap_path is None:
            raise DemoError("演示需要 RIVET_BWRAP_PATH 或系统 bubblewrap")
        if result_path.is_symlink():
            raise DemoError("演示结果路径不得是符号链接")
        result = run_release_demo(bwrap_path=bwrap_path)
        result_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = result_path.with_suffix(f"{result_path.suffix}.tmp")
        temporary.write_text(
            json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, result_path)
    except DemoError as error:
        print(str(error), file=sys.stderr)
        return 1
    print(f"固定演示通过，结果已写入 {result_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
