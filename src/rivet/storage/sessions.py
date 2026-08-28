"""原子保存会话 checkpoint，并保守恢复中断工具调用。"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import tempfile
from decimal import Decimal
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
    Timestamp,
    ToolCallId,
    TransactionId,
)
from rivet.contracts.messages import Message
from rivet.contracts.tools import SideEffectClass, ToolExecutionStatus

MAX_CHECKPOINT_BYTES = 4 * 1024 * 1024
MAX_SESSION_LIST_ENTRIES = 10_000
MAX_SESSION_LIST_LIMIT = 100
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
    ANSWERED = "ANSWERED"
    PLANNED = "PLANNED"
    READY_FOR_VERIFICATION = "READY_FOR_VERIFICATION"
    VERIFIED = "VERIFIED"
    REJECTED = "REJECTED"
    INCONCLUSIVE = "INCONCLUSIVE"
    BLOCKED = "BLOCKED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    INTERRUPTED = "INTERRUPTED"


class SessionStage(StrEnum):
    """区分可续跑 Agent、待验证事务和只读终态元数据。"""

    AGENT_LOOP = "AGENT_LOOP"
    PATCH_FINALIZATION = "PATCH_FINALIZATION"
    VERIFICATION = "VERIFICATION"
    TERMINAL = "TERMINAL"


ToolRecoveryStatus = ToolExecutionStatus


class PendingToolCall(ContractModel):
    """保存可能在进程退出时失去最终回执的工具调用。"""

    tool_call_id: ToolCallId
    run_id: RunId | None = None
    session_id: SessionId | None = None
    transaction_id: TransactionId | None = None
    tool_name: ToolName
    arguments_hash: str | None = Field(
        default=None,
        pattern=r"^sha256:[0-9a-f]{64}$",
        max_length=71,
    )
    side_effect_class: SideEffectClass = SideEffectClass.EXTERNAL_SIDE_EFFECT
    status: ToolExecutionStatus
    started_at: Timestamp | None = None
    completed_at: Timestamp | None = None
    result_hash: str | None = Field(
        default=None,
        pattern=r"^sha256:[0-9a-f]{64}$",
        max_length=71,
    )
    result_text: str | None = Field(default=None, max_length=65_536)
    error_code: str | None = Field(default=None, max_length=160)
    retry_policy: Literal[
        "AUTO_REPLAY_READ_ONLY",
        "VERIFY_THEN_RETRY",
        "VERIFY_TRANSACTION_EFFECT",
        "NEVER_AUTOMATIC",
    ] = "NEVER_AUTOMATIC"
    next_action: Literal["RETRY", "SKIP", "ABORT"] | None = None

    @model_validator(mode="after")
    def _validate_recovery_action(self) -> Self:
        """要求 UNKNOWN 明确动作，其他状态不得伪装恢复决策。"""
        if self.status is ToolExecutionStatus.UNKNOWN:
            if self.next_action is None:
                raise ValueError("UNKNOWN 工具调用必须记录下一步")
        elif self.next_action is not None:
            raise ValueError("只有 UNKNOWN 工具调用允许恢复动作")
        if self.status is ToolExecutionStatus.COMPLETED and (
            self.completed_at is None or self.result_hash is None
        ):
            raise ValueError("COMPLETED 工具调用必须记录完成时间和结果哈希")
        return self


class SessionCheckpoint(ContractModel):
    """持久化 Session、Run、事务和厂商 opaque 状态的最小集合。"""

    session_id: SessionId
    run_id: RunId
    transaction_id: TransactionId | None = None
    command: CommandName
    query: NonEmptyText
    status: SessionStatus
    stage: SessionStage = SessionStage.AGENT_LOOP
    model: str | None = Field(default=None, min_length=1, max_length=256)
    messages: tuple[Message, ...] = Field(default=(), max_length=1_024)
    termination_reason: str | None = Field(default=None, max_length=128)
    round_count: int = Field(default=0, ge=0)
    tool_call_count: int = Field(default=0, ge=0)
    prompt_tokens: int = Field(default=0, ge=0)
    completion_tokens: int = Field(default=0, ge=0)
    reasoning_tokens: int = Field(default=0, ge=0)
    cost_usd: Decimal = Field(default=Decimal(0), ge=0)
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
            tool.model_copy(
                update={
                    "status": ToolExecutionStatus.UNKNOWN,
                    "next_action": (
                        "RETRY"
                        if tool.side_effect_class is SideEffectClass.READ_ONLY
                        else "ABORT"
                    ),
                    "completed_at": None,
                    "result_hash": None,
                    "result_text": None,
                    "error_code": None,
                }
            )
            if tool.status is ToolExecutionStatus.EXECUTING
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
            stage=checkpoint.stage,
            model=checkpoint.model,
            messages=checkpoint.messages,
            termination_reason=checkpoint.termination_reason,
            round_count=checkpoint.round_count,
            tool_call_count=checkpoint.tool_call_count,
            prompt_tokens=checkpoint.prompt_tokens,
            completion_tokens=checkpoint.completion_tokens,
            reasoning_tokens=checkpoint.reasoning_tokens,
            cost_usd=checkpoint.cost_usd,
            provider_state=checkpoint.provider_state,
            pending_tools=pending_tools,
        )
        self.save(recovered)
        return recovered

    def list_recent_ids(self, *, limit: int = 20) -> tuple[str, ...]:
        """按修改时间列出经过完整校验的近期会话标识。"""
        if isinstance(limit, bool) or not 1 <= limit <= MAX_SESSION_LIST_LIMIT:
            raise ValueError("checkpoint 列表上限无效")
        self._validate_state_directories()
        if not self._sessions.exists():
            return ()
        if not self._sessions.is_dir():
            raise ValueError("checkpoint 状态目录无效")
        candidates: list[tuple[int, str]] = []
        try:
            for index, path in enumerate(self._sessions.iterdir()):
                if index >= MAX_SESSION_LIST_ENTRIES:
                    raise ValueError("checkpoint 文件数量超过上限")
                if path.is_symlink() or not path.is_file() or path.suffix != ".json":
                    continue
                session_id = path.stem
                if not SESSION_ID_PATTERN.fullmatch(session_id):
                    continue
                try:
                    checkpoint = self.load(session_id)
                    modified_ns = path.stat().st_mtime_ns
                except (KeyError, OSError, ValueError):
                    continue
                candidates.append((modified_ns, checkpoint.session_id))
        except OSError as error:
            raise ValueError("checkpoint 状态目录无法读取") from error
        candidates.sort(key=lambda item: (-item[0], item[1]))
        return tuple(session_id for _, session_id in candidates[:limit])

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
