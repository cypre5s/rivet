"""定义所有 Rivet 契约共用的版本、标识、路径和错误字段。"""

from __future__ import annotations

from pathlib import PurePosixPath
from typing import Annotated, Literal, Self

from pydantic import (
    AfterValidator,
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    model_validator,
)

SCHEMA_VERSION = 1
IDENTIFIER_SUFFIX_PATTERN = r"[a-z0-9][a-z0-9_-]{0,62}"
DOTTED_IDENTIFIER_PATTERN = r"^[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)+$"

RunId = Annotated[
    str, StringConstraints(pattern=rf"^run_{IDENTIFIER_SUFFIX_PATTERN}$", max_length=80)
]
SessionId = Annotated[
    str,
    StringConstraints(pattern=rf"^session_{IDENTIFIER_SUFFIX_PATTERN}$", max_length=84),
]
TransactionId = Annotated[
    str, StringConstraints(pattern=rf"^tx_{IDENTIFIER_SUFFIX_PATTERN}$", max_length=79)
]
EventId = Annotated[
    str,
    StringConstraints(pattern=rf"^event_{IDENTIFIER_SUFFIX_PATTERN}$", max_length=82),
]
EvidenceId = Annotated[
    str,
    StringConstraints(
        pattern=rf"^evidence_{IDENTIFIER_SUFFIX_PATTERN}$", max_length=85
    ),
]
ContextItemId = Annotated[
    str,
    StringConstraints(pattern=rf"^context_{IDENTIFIER_SUFFIX_PATTERN}$", max_length=84),
]
AcceptanceId = Annotated[
    str,
    StringConstraints(
        pattern=rf"^acceptance_{IDENTIFIER_SUFFIX_PATTERN}$", max_length=87
    ),
]
PatchId = Annotated[
    str,
    StringConstraints(pattern=rf"^patch_{IDENTIFIER_SUFFIX_PATTERN}$", max_length=82),
]
ToolCallId = Annotated[
    str,
    StringConstraints(pattern=rf"^call_{IDENTIFIER_SUFFIX_PATTERN}$", max_length=81),
]
RequestId = Annotated[
    str,
    StringConstraints(pattern=rf"^request_{IDENTIFIER_SUFFIX_PATTERN}$", max_length=84),
]
ResourceId = Annotated[
    str,
    StringConstraints(
        pattern=rf"^resource_{IDENTIFIER_SUFFIX_PATTERN}$", max_length=85
    ),
]
LeaseId = Annotated[
    str,
    StringConstraints(pattern=rf"^lease_{IDENTIFIER_SUFFIX_PATTERN}$", max_length=82),
]
VerificationStepId = Annotated[
    str,
    StringConstraints(
        pattern=rf"^verification_{IDENTIFIER_SUFFIX_PATTERN}$", max_length=89
    ),
]
ModuleId = Annotated[
    str, StringConstraints(pattern=DOTTED_IDENTIFIER_PATTERN, max_length=128)
]
CapabilityId = Annotated[
    str, StringConstraints(pattern=DOTTED_IDENTIFIER_PATTERN, max_length=160)
]
ToolName = Annotated[
    str, StringConstraints(pattern=DOTTED_IDENTIFIER_PATTERN, max_length=160)
]
EventType = Annotated[
    str, StringConstraints(pattern=DOTTED_IDENTIFIER_PATTERN, max_length=160)
]
ErrorCode = Annotated[
    str, StringConstraints(pattern=DOTTED_IDENTIFIER_PATTERN, max_length=160)
]
SemVer = Annotated[
    str,
    StringConstraints(
        pattern=r"^(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)"
        r"(?:-[0-9A-Za-z.-]+)?$",
        max_length=64,
    ),
]
Sha256Digest = Annotated[
    str, StringConstraints(pattern=r"^sha256:[0-9a-f]{64}$", max_length=71)
]
GitCommit = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{40}$", max_length=40)]
NonEmptyText = Annotated[str, StringConstraints(min_length=1, max_length=65_536)]
SummaryText = Annotated[str, StringConstraints(min_length=1, max_length=4_096)]
MediaType = Annotated[
    str,
    StringConstraints(
        pattern=r"^[a-z0-9][a-z0-9!#$&^_.+-]*/[a-z0-9][a-z0-9!#$&^_.+-]*$",
        max_length=160,
    ),
]


def _validate_repository_path(value: str) -> str:
    """拒绝绝对路径、反斜杠、跳转段和非规范 POSIX 形式。"""
    if not value or "\\" in value or value.startswith("/"):
        raise ValueError("序列化路径必须是相对 POSIX 路径")
    path_parts = value.split("/")
    if any(part in {"", ".", ".."} for part in path_parts):
        raise ValueError("序列化路径不得包含空段或跳转段")
    if PurePosixPath(value).as_posix() != value:
        raise ValueError("序列化路径必须使用规范 POSIX 形式")
    return value


RepositoryPath = Annotated[
    str,
    StringConstraints(min_length=1, max_length=4_096),
    AfterValidator(_validate_repository_path),
]


class ContractModel(BaseModel):
    """为持久化对象统一启用严格、冻结和隐藏输入的校验。"""

    model_config = ConfigDict(
        extra="forbid",
        strict=True,
        frozen=True,
        validate_default=True,
        hide_input_in_errors=True,
    )

    schema_version: Literal[1] = SCHEMA_VERSION


class SourceSpan(ContractModel):
    """标识仓库相对文件内的闭区间来源位置。"""

    repository_path: RepositoryPath
    start_line: int = Field(ge=1)
    start_column: int = Field(default=1, ge=1)
    end_line: int = Field(ge=1)
    end_column: int = Field(default=1, ge=1)

    @model_validator(mode="after")
    def _validate_order(self) -> Self:
        """确保结束位置不早于开始位置。"""
        start = (self.start_line, self.start_column)
        end = (self.end_line, self.end_column)
        if end < start:
            raise ValueError("来源区间结束位置不得早于开始位置")
        return self


class ArtifactReference(ContractModel):
    """引用受控运行目录中带哈希和大小的产物。"""

    path: RepositoryPath
    sha256: Sha256Digest
    size_bytes: int = Field(ge=0)
    media_type: MediaType


class ErrorDetail(ContractModel):
    """提供跨层稳定、可操作且已脱敏的错误字段。"""

    code: ErrorCode
    summary: SummaryText
    next_action: SummaryText
    retryable: bool
    run_id: RunId | None = None
    session_id: SessionId | None = None
    transaction_id: TransactionId | None = None
    trace_event_id: EventId | None = None
    cause_redacted: SummaryText | None = None


Timestamp = AwareDatetime
