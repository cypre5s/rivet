"""原子保存会话 checkpoint，并保守恢复中断工具调用。"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import tempfile
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Literal, Self, cast

from pydantic import (
    Field,
    JsonValue,
    StringConstraints,
    ValidationError,
    model_validator,
)

from rivet.contracts.common import (
    ContractModel,
    NonEmptyText,
    RunId,
    SessionId,
    ToolCallId,
    TransactionId,
)

MAX_CHECKPOINT_BYTES = 4 * 1024 * 1024
SESSION_ID_PATTERN = re.compile(r"^session_[a-z0-9][a-z0-9_-]{0,62}$")
CommandName = Annotated[
    str,
    StringConstraints(pattern=r"^[a-z][a-z0-9-]{0,31}$", max_length=32),
]
ToolName = Annotated[
    str,
    StringConstraints(
        pattern=r"^[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)+$",
        max_length=160,
    ),
]


class SessionStatus(StrEnum):
    """区分可继续、终态和崩溃后待确认的会话。"""

    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    INTERRUPTED = "INTERRUPTED"


class ToolRecoveryStatus(StrEnum):
    """记录工具事实，UNKNOWN 不得被推断为已成功。"""

    RUNNING = "RUNNING"
    UNKNOWN = "UNKNOWN"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class PendingToolCall(ContractModel):
    """保存可能在进程退出时失去最终回执的工具调用。"""

    tool_call_id: ToolCallId
    tool_name: ToolName
    status: ToolRecoveryStatus
    next_action: Literal["RETRY", "SKIP", "ABORT"] | None = None

    @model_validator(mode="after")
    def _validate_recovery_action(self) -> Self:
        """要求 UNKNOWN 明确动作，其他状态不得伪装恢复决策。"""
        if self.status is ToolRecoveryStatus.UNKNOWN:
            if self.next_action is None:
                raise ValueError("UNKNOWN 工具调用必须记录下一步")
        elif self.next_action is not None:
            raise ValueError("只有 UNKNOWN 工具调用允许恢复动作")
        return self


class SessionCheckpoint(ContractModel):
    """持久化 Session、Run、事务和厂商 opaque 状态的最小集合。"""

    session_id: SessionId
    run_id: RunId
    transaction_id: TransactionId | None = None
    command: CommandName
    query: NonEmptyText
    status: SessionStatus
    provider_state: JsonValue | None = None
    pending_tools: tuple[PendingToolCall, ...] = Field(default=(), max_length=256)

    @model_validator(mode="after")
    def _validate_unique_tool_calls(self) -> Self:
        """拒绝同一 checkpoint 中重复的工具调用标识。"""
        identifiers = tuple(tool.tool_call_id for tool in self.pending_tools)
        if len(identifiers) != len(set(identifiers)):
            raise ValueError("工具调用标识不得重复")
        return self


class SessionStore:
    """在仓库私有目录中保存带内容哈希的 checkpoint。"""

    def __init__(self, repository: Path) -> None:
        self._repository = repository.resolve(strict=False)
        self._sessions = self._repository / ".rivet" / "sessions"

    def save(self, checkpoint: SessionCheckpoint) -> Path:
        """以 0600 原子替换 checkpoint，并写入可复核内容哈希。"""
        self._prepare()
        path = self._checkpoint_path(checkpoint.session_id)
        payload = checkpoint.model_dump(mode="json")
        canonical = _canonical_json(payload)
        envelope = {
            "checkpoint": payload,
            "checkpoint_sha256": f"sha256:{hashlib.sha256(canonical).hexdigest()}",
            "schema_version": 1,
        }
        content = _canonical_json(envelope) + b"\n"
        temporary_descriptor, temporary_name = tempfile.mkstemp(
            dir=self._sessions,
            prefix=f".{checkpoint.session_id}.",
            suffix=".tmp",
        )
        temporary_path = Path(temporary_name)
        try:
            with os.fdopen(temporary_descriptor, "wb") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            temporary_path.chmod(0o600)
            os.replace(temporary_path, path)
        finally:
            if temporary_path.exists():
                temporary_path.unlink()
        return path

    def load(self, session_id: str) -> SessionCheckpoint:
        """严格验证 ID、普通文件、大小、协议、哈希和契约。"""
        self._validate_state_directories()
        path = self._checkpoint_path(session_id)
        if not path.exists():
            raise KeyError(session_id)
        if path.is_symlink() or not path.is_file():
            raise ValueError("checkpoint 路径无效")
        try:
            size = path.stat().st_size
            if size <= 0 or size > MAX_CHECKPOINT_BYTES:
                raise ValueError("checkpoint 大小无效")
            raw_document = cast(object, json.loads(path.read_bytes()))
            if not isinstance(raw_document, dict):
                raise ValueError("checkpoint envelope 无效")
            document = cast(dict[str, object], raw_document)
            if (
                set(document)
                != {
                    "checkpoint",
                    "checkpoint_sha256",
                    "schema_version",
                }
                or document.get("schema_version") != 1
            ):
                raise ValueError("checkpoint envelope 无效")
            raw_payload = document.get("checkpoint")
            digest = document.get("checkpoint_sha256")
            if not isinstance(raw_payload, dict) or not isinstance(digest, str):
                raise ValueError("checkpoint envelope 无效")
            payload = cast(dict[str, object], raw_payload)
            expected = f"sha256:{hashlib.sha256(_canonical_json(payload)).hexdigest()}"
            if not hmac.compare_digest(digest, expected):
                raise ValueError("checkpoint 内容哈希不匹配")
            checkpoint = SessionCheckpoint.model_validate_json(_canonical_json(payload))
        except (OSError, UnicodeError, json.JSONDecodeError, ValidationError) as error:
            raise ValueError("checkpoint 无法验证") from error
        if checkpoint.session_id != session_id:
            raise ValueError("checkpoint 会话标识不匹配")
        return checkpoint

    def resume(self, session_id: str) -> SessionCheckpoint:
        """把崩溃时 RUNNING 工具转成 UNKNOWN/RETRY 后再返回。"""
        checkpoint = self.load(session_id)
        if checkpoint.status is not SessionStatus.RUNNING:
            return checkpoint
        pending_tools = tuple(
            PendingToolCall(
                tool_call_id=tool.tool_call_id,
                tool_name=tool.tool_name,
                status=ToolRecoveryStatus.UNKNOWN,
                next_action="RETRY",
            )
            if tool.status is ToolRecoveryStatus.RUNNING
            else tool
            for tool in checkpoint.pending_tools
        )
        recovered = SessionCheckpoint(
            session_id=checkpoint.session_id,
            run_id=checkpoint.run_id,
            transaction_id=checkpoint.transaction_id,
            command=checkpoint.command,
            query=checkpoint.query,
            status=SessionStatus.INTERRUPTED,
            provider_state=checkpoint.provider_state,
            pending_tools=pending_tools,
        )
        self.save(recovered)
        return recovered

    def _prepare(self) -> None:
        """只在显式保存时创建目录并拒绝任一级符号链接。"""
        self._validate_state_directories()
        self._sessions.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._sessions.chmod(0o700)

    def _validate_state_directories(self) -> None:
        """拒绝运行根或会话目录通过符号链接跳出仓库。"""
        runtime_root = self._repository / ".rivet"
        if runtime_root.is_symlink() or self._sessions.is_symlink():
            raise ValueError("checkpoint 状态目录不得是符号链接")

    def _checkpoint_path(self, session_id: str) -> Path:
        """在路径拼接前验证公共 Session ID 格式。"""
        if not SESSION_ID_PATTERN.fullmatch(session_id):
            raise ValueError("checkpoint session_id 无效")
        return self._sessions / f"{session_id}.json"


def _canonical_json(value: object) -> bytes:
    """以稳定 UTF-8 JSON 编码供原子存储和哈希复核。"""
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
