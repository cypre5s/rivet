"""签发显式权限租约并对每次敏感动作执行失败关闭授权。"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from rivet.contracts.guard import (
    AuthorizationDecision,
    AuthorizationStatus,
    CapabilityLease,
    Permission,
    PermissionRequest,
    PermissionScope,
    TaintSource,
)

Clock = Callable[[], datetime]
LeaseIdFactory = Callable[[], str]
UNTRUSTED_SOURCES = frozenset(
    {
        TaintSource.REPOSITORY_DATA,
        TaintSource.EXTERNAL_CONTENT,
        TaintSource.TOOL_OUTPUT,
    }
)


def _utc_now() -> datetime:
    """返回租约使用的 UTC 当前时间。"""
    return datetime.now(UTC)


def _new_lease_id() -> str:
    """生成不携带用户数据的租约标识。"""
    return f"lease_{uuid.uuid4().hex}"


class PermissionLeaseError(ValueError):
    """表示租约签发请求缺少显式批准或有效期限。"""


@dataclass(slots=True)
class _LeaseState:
    """把冻结租约与仅驻留内存的剩余次数绑定。"""

    lease: CapabilityLease
    remaining_uses: int


class GuardPolicy:
    """依据安全读取、显式租约、交互模式和污点作授权决定。"""

    def __init__(
        self,
        *,
        headless: bool,
        clock: Clock = _utc_now,
        lease_id_factory: LeaseIdFactory = _new_lease_id,
    ) -> None:
        self._headless = headless
        self._clock = clock
        self._lease_id_factory = lease_id_factory
        self._leases: dict[str, _LeaseState] = {}

    def issue_lease(
        self,
        request: PermissionRequest,
        *,
        approved_by_user: bool,
        expires_at: datetime,
        max_uses: int,
    ) -> CapabilityLease:
        """只把真实 UI/CLI 用户确认转换成有界租约。"""
        if not approved_by_user:
            raise PermissionLeaseError("权限租约必须获得显式用户批准")
        issued_at = self._clock()
        if expires_at <= issued_at:
            raise PermissionLeaseError("权限租约必须具有未来过期时间")
        lease = CapabilityLease(
            lease_id=self._lease_id_factory(),
            permission=request.permission,
            scope=request.scope,
            reason=request.reason,
            run_id=request.run_id,
            transaction_id=request.transaction_id,
            paths=request.paths,
            domains=request.domains,
            issued_at=issued_at,
            expires_at=expires_at,
            max_uses=max_uses,
        )
        self._leases[lease.lease_id] = _LeaseState(lease, max_uses)
        return lease

    def authorize(self, request: PermissionRequest) -> AuthorizationDecision:
        """自动允许安全读取，其余动作必须命中租约或明确询问。"""
        if request.permission is Permission.READ:
            return AuthorizationDecision(
                status=AuthorizationStatus.ALLOWED,
                code="guard.read_auto_approved",
                summary="安全工作区读取已自动批准",
            )
        matching = self._find_matching_lease(request)
        if matching is not None:
            matching.remaining_uses -= 1
            return AuthorizationDecision(
                status=AuthorizationStatus.ALLOWED,
                code="guard.lease_authorized",
                summary="动作已由有效权限租约批准",
                lease_id=matching.lease.lease_id,
            )
        if set(request.taint_sources).intersection(UNTRUSTED_SOURCES):
            return AuthorizationDecision(
                status=AuthorizationStatus.DENIED,
                code="guard.tainted_permission_denied",
                summary="不可信内容不能创建隐式权限请求",
            )
        if self._headless:
            return AuthorizationDecision(
                status=AuthorizationStatus.DENIED,
                code="guard.permission_required",
                summary="headless 模式缺少有效权限租约",
            )
        return AuthorizationDecision(
            status=AuthorizationStatus.PROMPT,
            code="guard.user_confirmation_required",
            summary="敏感动作必须等待用户显式确认",
        )

    def _find_matching_lease(self, request: PermissionRequest) -> _LeaseState | None:
        """按稳定签发顺序查找未过期、未耗尽且范围覆盖的租约。"""
        now = self._clock()
        for state in self._leases.values():
            lease = state.lease
            if state.remaining_uses <= 0 or lease.expires_at <= now:
                continue
            if (
                lease.permission is not request.permission
                or lease.run_id != request.run_id
                or lease.transaction_id != request.transaction_id
                or lease.scope is not request.scope
            ):
                continue
            if request.scope is PermissionScope.SPECIFIC_PATHS and not all(
                any(_path_covers(allowed, requested) for allowed in lease.paths)
                for requested in request.paths
            ):
                continue
            if request.scope is PermissionScope.SPECIFIC_DOMAINS and not set(
                request.domains
            ).issubset(lease.domains):
                continue
            return state
        return None


def _path_covers(allowed: str, requested: str) -> bool:
    """把租约路径解释为精确文件或目录前缀。"""
    return requested == allowed or requested.startswith(f"{allowed}/")
