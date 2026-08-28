"""定义事务内部仓库事实和启动恢复报告。"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from rivet.contracts.common import (
    ContractModel,
    PatchId,
    Sha256Digest,
    Timestamp,
    TransactionId,
)
from rivet.contracts.transactions import AcceptanceSpec, PatchSet, TransactionRecord


class DirtyPolicy(StrEnum):
    """区分拒绝脏仓库和显式创建非破坏性快照。"""

    REJECT = "reject"
    SNAPSHOT = "snapshot"


class ApplyIntent(ContractModel):
    """在修改主工作区前持久化可恢复的 apply 意图。"""

    transaction_id: TransactionId
    patch_id: PatchId
    patch_sha256: Sha256Digest
    repository_fingerprint: Sha256Digest
    created_at: Timestamp


@dataclass(frozen=True, slots=True)
class RepositorySnapshot:
    """保存创建事务所需且已哈希的 Git 仓库事实。"""

    repository_root: Path
    repository_identity: str
    repository_fingerprint: str
    head_commit: str
    branch: str | None
    detached_head: bool
    dirty: bool
    status_paths: tuple[str, ...]
    untracked_paths: tuple[str, ...]
    has_submodules: bool
    submodule_status_sha256: str
    git_config_summary: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class RecoverableWorktree:
    """标识具有非终态事务记录的可恢复 Worktree。"""

    transaction_id: str
    path: Path


@dataclass(frozen=True, slots=True)
class OrphanWorktree:
    """标识缺少活动事务记录但仍登记在 Git 的 Worktree。"""

    path: Path


@dataclass(frozen=True, slots=True)
class WorktreeRecoveryReport:
    """汇总启动扫描发现的可恢复和孤儿 Worktree。"""

    recoverable: tuple[RecoverableWorktree, ...]
    orphans: tuple[OrphanWorktree, ...]
    quarantined: tuple[OrphanWorktree, ...] = ()


@dataclass(frozen=True, slots=True)
class TransactionVerificationContext:
    """向验证模块暴露已复核且只读的事务事实。"""

    record: TransactionRecord
    acceptance: AcceptanceSpec
    patch: PatchSet
    repository_root: Path
    worktree: Path
    patch_path: Path
