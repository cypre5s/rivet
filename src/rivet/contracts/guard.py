"""定义权限、租约、授权决定与不可信来源的稳定契约。"""

from __future__ import annotations

import re
from enum import StrEnum
from typing import Annotated, Self

from pydantic import AfterValidator, Field, StringConstraints, model_validator

from rivet.contracts.common import (
    ContractModel,
    ErrorCode,
    LeaseId,
    RunId,
    SummaryText,
    Timestamp,
    TransactionId,
)

DOMAIN_PATTERN = re.compile(
    r"^(?=.{1,253}\Z)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)*"
    r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$"
)


class Permission(StrEnum):
    """列出运行时可租用的四种副作用权限。"""

    READ = "READ"
    WRITE = "WRITE"
    EXECUTE = "EXECUTE"
    NETWORK = "NETWORK"


class PermissionScope(StrEnum):
    """限制权限可作用的工作区、事务、路径或域名。"""

    WORKSPACE = "workspace"
    TRANSACTION = "transaction"
    SPECIFIC_PATHS = "specific_paths"
    SPECIFIC_DOMAINS = "specific_domains"


class TaintSource(StrEnum):
    """标记可能影响一次权限请求的文本来源。"""

    USER_INSTRUCTION = "user_instruction"
    REPOSITORY_DATA = "repository_data"
    EXTERNAL_CONTENT = "external_content"
    TOOL_OUTPUT = "tool_output"


class AuthorizationStatus(StrEnum):
    """表示授权已允许、已拒绝或必须询问用户。"""

    ALLOWED = "ALLOWED"
    DENIED = "DENIED"
    PROMPT = "PROMPT"


def _validate_domain(value: str) -> str:
    """只接受规范小写主机名，不接受 URL、端口或通配符。"""
    if value != value.lower() or DOMAIN_PATTERN.fullmatch(value) is None:
        raise ValueError("域名必须是规范小写主机名")
    return value


DomainName = Annotated[
    str,
    StringConstraints(min_length=1, max_length=253),
    AfterValidator(_validate_domain),
]


def _validate_scoped_path(value: str) -> str:
    """拒绝绝对路径、反斜杠和父目录跳转。"""
    if not value or value.startswith("/") or "\\" in value or "\x00" in value:
        raise ValueError("权限路径必须是非空 POSIX 相对路径")
    parts = value.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise ValueError("权限路径不得包含空段或跳转段")
    return value


ScopedPath = Annotated[
    str,
    StringConstraints(min_length=1, max_length=4_096),
    AfterValidator(_validate_scoped_path),
]


class PermissionRequest(ContractModel):
    """描述一次待授权动作及其身份、范围和来源。"""

    permission: Permission
    scope: PermissionScope
    reason: SummaryText
    run_id: RunId
    transaction_id: TransactionId | None = None
    paths: tuple[ScopedPath, ...] = ()
    domains: tuple[DomainName, ...] = ()
    taint_sources: tuple[TaintSource, ...] = (TaintSource.USER_INSTRUCTION,)

    @model_validator(mode="after")
    def _validate_scope(self) -> Self:
        """确保范围载荷与权限类型一致且没有歧义。"""
        if len(set(self.paths)) != len(self.paths) or len(set(self.domains)) != len(
            self.domains
        ):
            raise ValueError("权限范围不得包含重复项")
        if not self.taint_sources or len(set(self.taint_sources)) != len(
            self.taint_sources
        ):
            raise ValueError("权限请求必须包含唯一来源")
        if self.scope is PermissionScope.SPECIFIC_PATHS:
            if not self.paths or self.domains:
                raise ValueError("具体路径范围必须只包含路径")
        elif self.scope is PermissionScope.SPECIFIC_DOMAINS:
            if not self.domains or self.paths:
                raise ValueError("具体域名范围必须只包含域名")
            if self.permission is not Permission.NETWORK:
                raise ValueError("具体域名范围只适用于网络权限")
        elif self.paths or self.domains:
            raise ValueError("工作区或事务范围不得附带路径和域名")
        if (
            self.scope
            in {
                PermissionScope.TRANSACTION,
                PermissionScope.SPECIFIC_PATHS,
            }
            and self.transaction_id is None
        ):
            raise ValueError("事务或具体路径权限必须绑定事务")
        if (
            self.permission is Permission.WRITE
            and self.scope is PermissionScope.WORKSPACE
        ):
            raise ValueError("写权限不得授予主工作区")
        return self


class CapabilityLease(ContractModel):
    """保存经用户显式批准且可过期、可计次的权限租约。"""

    lease_id: LeaseId
    permission: Permission
    scope: PermissionScope
    reason: SummaryText
    run_id: RunId
    transaction_id: TransactionId | None = None
    paths: tuple[ScopedPath, ...] = ()
    domains: tuple[DomainName, ...] = ()
    issued_at: Timestamp
    expires_at: Timestamp
    max_uses: int = Field(ge=1, le=10_000)

    @model_validator(mode="after")
    def _validate_window_and_scope(self) -> Self:
        """复用请求校验并要求租约具有正时间窗口。"""
        if self.expires_at <= self.issued_at:
            raise ValueError("租约过期时间必须晚于签发时间")
        PermissionRequest(
            permission=self.permission,
            scope=self.scope,
            reason=self.reason,
            run_id=self.run_id,
            transaction_id=self.transaction_id,
            paths=self.paths,
            domains=self.domains,
        )
        return self


class AuthorizationDecision(ContractModel):
    """给工具层返回稳定、可审计且不含敏感输入的决定。"""

    status: AuthorizationStatus
    code: ErrorCode
    summary: SummaryText
    lease_id: LeaseId | None = None
