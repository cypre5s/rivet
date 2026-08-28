"""运行短生命周期解析 worker 并验证其有界 JSON 输出。"""

from __future__ import annotations

import json
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from typing import cast

from pydantic import JsonValue

from rivet.tools.paths import WorkspaceBoundary
from rivet.tools.process import ProcessRunner

from .base import ReaderContext, ReaderError


@dataclass(frozen=True, slots=True)
class WorkerOutput:
    """保存已验证的解析 worker 成功载荷。"""

    content: str
    metadata: dict[str, JsonValue]
    warnings: tuple[str, ...]
    truncated: bool


async def run_reader_worker(
    context: ReaderContext,
    *,
    mode: str,
    arguments: Sequence[str] = (),
) -> dict[str, JsonValue]:
    """以无 shell argv、环境白名单和 ResourceScope 执行一次解析。"""
    runner = ProcessRunner(
        WorkspaceBoundary(context.repository_root),
        scope=context.scope,
        max_capture_bytes=min(
            context.request.max_output_chars * 4 + 1_000_000, 17_000_000
        ),
        root_kind="repository_read_only",
    )
    completed = await runner.run(
        (
            sys.executable,
            "-m",
            "rivet.readers.worker",
            "--mode",
            mode,
            "--source",
            context.inspection.source_path,
            *arguments,
        ),
        cwd=".",
        timeout_seconds=float(context.request.timeout_seconds),
    )
    if completed.timed_out:
        raise ReaderError("reader.worker.timeout", "解析 worker 超时")
    if completed.stdout_truncated:
        raise ReaderError("reader.worker.output_exceeded", "解析 worker 输出超限")
    try:
        raw_payload: object = json.loads(
            completed.stdout.decode("utf-8", errors="strict")
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ReaderError(
            "reader.worker.protocol_invalid", "解析 worker 协议无效"
        ) from error
    if not isinstance(raw_payload, dict):
        raise ReaderError(
            "reader.worker.protocol_invalid", "解析 worker 响应必须是对象"
        )
    payload = cast(dict[str, JsonValue], raw_payload)
    if completed.returncode != 0 or payload.get("ok") is not True:
        error_code = payload.get("error_code")
        code = error_code if isinstance(error_code, str) else "reader.worker.failed"
        raise ReaderError(code, "解析 worker 失败")
    return payload


def parse_worker_output(payload: dict[str, JsonValue]) -> WorkerOutput:
    """严格收窄 worker 的内容、元数据、警告和截断字段。"""
    content = payload.get("content")
    metadata = payload.get("metadata")
    warnings = payload.get("warnings")
    truncated = payload.get("truncated")
    if (
        not isinstance(content, str)
        or not isinstance(metadata, dict)
        or not isinstance(warnings, list)
        or not all(isinstance(item, str) for item in warnings)
        or not isinstance(truncated, bool)
    ):
        raise ReaderError("reader.worker.protocol_invalid", "解析 worker 字段无效")
    return WorkerOutput(
        content=content,
        metadata=cast(dict[str, JsonValue], metadata),
        warnings=tuple(cast(list[str], warnings)),
        truncated=truncated,
    )
