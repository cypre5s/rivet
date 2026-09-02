"""编排事务基线、冻结验收、补丁历史、显式应用与恢复。"""

from __future__ import annotations

import re
import shutil
import uuid
from collections.abc import Callable
from contextlib import suppress
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Literal

from rivet.contracts.common import RepositoryPath
from rivet.contracts.transactions import (
    AcceptanceSpec,
    Command,
    PatchSet,
    TransactionRecord,
    TransactionState,
)
from rivet.contracts.verification import Verdict, VerificationStatus
from rivet.kernel.resources import ResourceScope
from rivet.tools.paths import WorkspaceBoundary
from rivet.trace.paths import RuntimePaths

from .errors import TransactionError
from .git_backend import GitBackend
from .hashing import acceptance_sha256, sha256_digest
from .models import (
    ApplyIntent,
    OrphanWorktree,
    RecoverableWorktree,
    RepositorySnapshot,
    TransactionVerificationContext,
    WorktreeRecoveryReport,
)
from .state_machine import validate_transition
from .store import TransactionStore

Clock = Callable[[], datetime]
TERMINAL_STATES = frozenset({TransactionState.APPLIED, TransactionState.ABORTED})
VERIFICATION_ATTEMPT_PATTERN = re.compile(r"^attempt_[0-9]{4}$")
PROJECT_CONFIG_PATH = ".rivet/project.toml"


def _new_identifier(prefix: str) -> str:
    """生成符合公共 ID 约束的随机局部标识。"""
    return f"{prefix}_{uuid.uuid4().hex}"


def _is_relative_to(path: Path, root: Path) -> bool:
    """判断规范路径是否位于给定根内。"""
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


class TransactionManager:
    """确保代码修改只在 XDG Worktree 内发生并由显式 apply 交付。"""

    def __init__(
        self,
        repository: Path,
        *,
        scope: ResourceScope,
        cache_root: Path | None = None,
        state_root: Path | None = None,
        evidence_root: Path | None = None,
        clock: Clock | None = None,
    ) -> None:
        self._candidate = repository.resolve(strict=False)
        self._scope = scope
        self._configured_cache_root = (
            cache_root.resolve(strict=False) if cache_root is not None else None
        )
        self._configured_state_root = (
            state_root.resolve(strict=False) if state_root is not None else None
        )
        self._configured_evidence_root = (
            evidence_root.resolve(strict=False) if evidence_root is not None else None
        )
        self._clock = clock or (lambda: datetime.now(UTC))
        self._backend: GitBackend | None = None
        self._store: TransactionStore | None = None
        self._cache_root: Path | None = None
        self._registered_worktrees: set[Path] = set()
        self._verification_worktrees: set[Path] = set()

    async def inspect_repository(self) -> RepositorySnapshot:
        """只读发现 Git 根并返回创建事务所需的完整事实。"""
        backend = await self._ensure_backend()
        return await backend.inspect()

    def draft_acceptance(
        self,
        *,
        user_goal: str,
        baseline_reproduction: tuple[Command, ...],
        allowed_paths: tuple[RepositoryPath, ...],
        read_scope: tuple[RepositoryPath, ...] = (),
        allowed_new_paths: tuple[RepositoryPath, ...] = (),
        expected_behaviors: tuple[str, ...],
        preserved_behaviors: tuple[str, ...],
        verification_commands: tuple[Command, ...],
        behavior_verification_commands: tuple[Command, ...],
        max_wall_seconds: int,
        max_tokens: int,
        max_tool_calls: int,
        forbidden_paths: tuple[RepositoryPath, ...] = (),
        scope_reason: str = "完成用户任务所需的最小文件集合",
        scope_source: Literal["explicit", "task", "project"] = "project",
        max_cost_usd: Decimal | None = None,
        acceptable_risks: tuple[str, ...] = (),
        non_goals: tuple[str, ...] = (),
        acceptance_id: str | None = None,
    ) -> AcceptanceSpec:
        """从用户与 headless 参数构造尚未持久化的验收草案。"""
        return AcceptanceSpec(
            acceptance_id=acceptance_id or _new_identifier("acceptance"),
            user_goal=user_goal,
            baseline_reproduction=baseline_reproduction,
            read_scope=read_scope,
            allowed_paths=allowed_paths,
            write_scope=allowed_paths,
            allowed_new_paths=allowed_new_paths,
            forbidden_paths=forbidden_paths,
            scope_reason=scope_reason,
            scope_source=scope_source,
            expected_behaviors=expected_behaviors,
            preserved_behaviors=preserved_behaviors,
            verification_commands=verification_commands,
            behavior_verification_commands=behavior_verification_commands,
            max_wall_seconds=max_wall_seconds,
            max_tokens=max_tokens,
            max_tool_calls=max_tool_calls,
            max_cost_usd=max_cost_usd,
            acceptable_risks=acceptable_risks,
            non_goals=non_goals,
        )

    async def create(
        self,
        specification: AcceptanceSpec,
        *,
        confirmed: bool,
        transaction_id: str | None = None,
        expected_base_commit: str | None = None,
    ) -> TransactionRecord:
        """把首个持久事务记录直接写成 ACCEPTANCE_FROZEN。"""
        if not confirmed:
            raise TransactionError(
                "transaction.acceptance_confirmation_required",
                "创建事务前必须显式确认 AcceptanceSpec",
            )
        backend = await self._ensure_backend()
        store = self._require_store()
        snapshot = await backend.inspect()
        if snapshot.dirty:
            raise TransactionError(
                "transaction.dirty_repository_rejected",
                "检测到脏工作区，请先 commit 或 stash 当前修改",
            )
        project_config = backend.repository_root / PROJECT_CONFIG_PATH
        if (
            project_config.exists() or project_config.is_symlink()
        ) and not await backend.is_tracked_path(PROJECT_CONFIG_PATH):
            raise TransactionError(
                "transaction.project_config_untracked",
                "项目验证配置必须由 Git 跟踪后才能冻结事务",
            )
        if (
            expected_base_commit is not None
            and snapshot.head_commit != expected_base_commit
        ):
            raise TransactionError(
                "transaction.proposal_base_drift",
                "Git 基线已不同于用户确认的只读提案",
            )
        store.prepare()
        selected_id = transaction_id or _new_identifier("tx")
        acceptance_hash = acceptance_sha256(specification)
        created_at = self._now()
        record = TransactionRecord(
            transaction_id=selected_id,
            state=TransactionState.ACCEPTANCE_FROZEN,
            repository_identity=snapshot.repository_identity,
            repository_fingerprint=snapshot.repository_fingerprint,
            head_commit=snapshot.head_commit,
            base_commit=snapshot.head_commit,
            branch=snapshot.branch,
            detached_head=snapshot.detached_head,
            has_submodules=snapshot.has_submodules,
            submodule_status_sha256=snapshot.submodule_status_sha256,
            git_config_summary=snapshot.git_config_summary,
            acceptance_sha256=acceptance_hash,
            created_at=created_at,
            updated_at=created_at,
        )
        store.publish_frozen_transaction(record, specification)
        await self._ensure_transaction_worktree(record)
        return record

    async def load_acceptance_spec(self, transaction_id: str) -> AcceptanceSpec:
        """加载并复验事务绑定的冻结 AcceptanceSpec。"""
        await self._ensure_backend()
        record = self._require_store().load_record(transaction_id)
        acceptance_hash = self._verify_acceptance(record)
        return self._require_store().load_acceptance(
            transaction_id,
            expected_sha256=acceptance_hash,
        )

    def transaction_boundary(self, transaction_id: str) -> WorkspaceBoundary:
        """只向写工具暴露主仓库只读根和独立事务写根。"""
        backend = self._require_backend()
        record = self._require_store().load_record(transaction_id)
        if record.state in TERMINAL_STATES:
            raise TransactionError(
                "transaction.worktree_terminal",
                "终态事务不再提供写入 Worktree",
            )
        worktree = self._worktree_path_from(
            record.repository_identity, record.transaction_id
        )
        if not worktree.is_dir():
            raise TransactionError(
                "transaction.worktree_missing",
                "事务 Worktree 不存在",
            )
        return WorkspaceBoundary(
            backend.repository_root,
            worktree,
            transaction_id=record.transaction_id,
            mode="FIX",
        )

    def worktree_path(self, transaction_id: str) -> Path:
        """返回记录绑定的 XDG cache Worktree 路径。"""
        record = self._require_store().load_record(transaction_id)
        return self._worktree_path_from(
            record.repository_identity, record.transaction_id
        )

    def patch_path(self, transaction_id: str, patch_id: str) -> Path:
        """返回本地持久化 binary diff 路径。"""
        return self._require_store().patch_path(transaction_id, patch_id)

    def evidence_root(self, transaction_id: str) -> Path:
        """返回独立 XDG Evidence 根中当前事务的私有目录。"""
        store = self._require_store()
        store.load_record(transaction_id)
        return store.evidence_directory(transaction_id)

    def store(self) -> TransactionStore:
        """返回当前管理器绑定的 XDG 事务事实源。"""
        return self._require_store()

    async def candidate_diff(
        self,
        transaction_id: str,
        *,
        path: str | None = None,
        max_bytes: int = 65_536,
    ) -> str:
        """读取 Worktree 相对冻结基线的有界候选补丁。"""
        if max_bytes <= 0:
            raise ValueError("候选补丁输出上限必须大于零")
        backend = await self._ensure_backend()
        store = self._require_store()
        record = store.load_record(transaction_id)
        self._verify_acceptance(record)
        worktree = self._active_worktree(record)
        paths: tuple[str, ...] = ()
        if path is not None:
            boundary = self.transaction_boundary(transaction_id)
            resolved = boundary.resolve_repository(path, require_exists=False)
            paths = (boundary.repository_relative(resolved),)
        content = await backend.binary_diff(
            worktree,
            record.base_commit,
            paths=paths,
        )
        truncated = len(content) > max_bytes
        visible = content[:max_bytes].decode("utf-8", errors="replace")
        return visible + ("\n[TRUNCATED]\n" if truncated else "")

    async def verification_context(
        self,
        transaction_id: str,
    ) -> TransactionVerificationContext:
        """复核验收、补丁、主仓库和 Worktree 后返回验证输入。"""
        backend = await self._ensure_backend()
        store = self._require_store()
        record = store.load_record(transaction_id)
        if record.state is not TransactionState.VERIFYING:
            raise TransactionError(
                "transaction.verification_context_state_invalid",
                "只有 VERIFYING 事务可以读取验证上下文",
            )
        acceptance_hash = self._verify_acceptance(record)
        acceptance = store.load_acceptance(
            transaction_id,
            expected_sha256=acceptance_hash,
        )
        patch, stored_content = self._load_current_patch(record)
        snapshot = await backend.inspect()
        if not await self._verification_repository_matches(
            backend,
            snapshot,
            record,
        ):
            raise TransactionError(
                "transaction.verification_repository_drift",
                "主仓库在验证前发生漂移",
            )
        worktree = self._active_worktree(record)
        current_content = await backend.binary_diff(worktree, record.base_commit)
        if (
            current_content != stored_content
            or sha256_digest(current_content) != patch.patch_sha256
        ):
            raise TransactionError(
                "transaction.verification_patch_drift",
                "事务补丁在验证前发生漂移",
            )
        return TransactionVerificationContext(
            record=record,
            acceptance=acceptance,
            patch=patch,
            repository_root=backend.repository_root,
            worktree=worktree,
            patch_path=store.patch_path(transaction_id, patch.patch_id),
        )

    async def create_verification_baseline(
        self,
        transaction_id: str,
        attempt_name: str,
    ) -> Path:
        """从事务 base commit 创建只供 V1 使用的临时 Worktree。"""
        return await self._create_verification_worktree(
            transaction_id,
            attempt_name,
            variant="baseline",
            apply_patch=False,
        )

    async def create_verification_candidate(
        self,
        transaction_id: str,
        attempt_name: str,
    ) -> Path:
        """从同一 base commit 创建并应用已冻结补丁的验证副本。"""
        return await self._create_verification_worktree(
            transaction_id,
            attempt_name,
            variant="candidate",
            apply_patch=True,
        )

    async def _create_verification_worktree(
        self,
        transaction_id: str,
        attempt_name: str,
        *,
        variant: str,
        apply_patch: bool,
    ) -> Path:
        """建立由 attempt 和固定变体名派生的临时 Worktree。"""
        if not VERIFICATION_ATTEMPT_PATTERN.fullmatch(attempt_name) or variant not in {
            "baseline",
            "candidate",
        }:
            raise TransactionError(
                "transaction.verification_attempt_invalid",
                "验证 attempt 或 Worktree 变体名称无效",
            )
        context = await self.verification_context(transaction_id)
        identity = context.record.repository_identity.removeprefix("sha256:")
        path = (
            self._require_cache_root()
            / "verification"
            / identity
            / transaction_id
            / attempt_name
            / variant
        ).resolve(strict=False)
        verification_root = (self._require_cache_root() / "verification").resolve(
            strict=False
        )
        if not _is_relative_to(path, verification_root) or path.exists():
            raise TransactionError(
                "transaction.verification_worktree_exists",
                "验证基线 Worktree 路径无效或已存在",
            )
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            await self._require_backend().add_worktree(path, context.record.base_commit)
            if apply_patch:
                await self._require_backend().apply_patch_to_worktree(
                    path,
                    context.patch_path,
                    check_only=True,
                )
                await self._require_backend().apply_patch_to_worktree(
                    path,
                    context.patch_path,
                    check_only=False,
                )
        except BaseException:
            if path.exists():
                shutil.rmtree(path)
            with suppress(TransactionError):
                await self._require_backend().remove_worktree(path)
            raise
        resolved = path.resolve(strict=True)
        self._scope.register_worktree(
            resolved,
            cleanup=self._cleanup_verification_worktree_on_scope_close,
            description="验证基线 Worktree",
        )
        self._verification_worktrees.add(resolved)
        return resolved

    async def cleanup_verification_worktree(self, worktree: Path) -> None:
        """只清理本管理器创建的验证 Worktree 并释放资源登记。"""
        resolved = worktree.resolve(strict=False)
        if resolved not in self._verification_worktrees:
            raise TransactionError(
                "transaction.verification_worktree_unknown",
                "验证基线 Worktree 不属于当前管理器",
            )
        await self._require_backend().remove_worktree(resolved)
        self._scope.release_worktree(resolved)
        self._verification_worktrees.remove(resolved)
        self._prune_verification_directories(resolved.parent)

    async def record_patch_set(
        self,
        transaction_id: str,
        *,
        patch_id: str | None = None,
    ) -> PatchSet:
        """生成完整 binary diff、保存 PatchSet 并进入 PATCHING。"""
        backend = await self._ensure_backend()
        store = self._require_store()
        record = store.load_record(transaction_id)
        if record.state not in {
            TransactionState.ACCEPTANCE_FROZEN,
            TransactionState.PATCHING,
            TransactionState.REJECTED,
            TransactionState.INCONCLUSIVE,
            TransactionState.BLOCKED,
            TransactionState.CANCELLED,
        }:
            raise TransactionError(
                "transaction.patch_state_invalid",
                "当前事务状态不能记录 PatchSet",
            )
        acceptance_hash = self._verify_acceptance(record)
        worktree = self._active_worktree(record)
        content = await backend.binary_diff(worktree, record.base_commit)
        if not content:
            raise TransactionError("transaction.patch_empty", "事务没有可记录的修改")
        changed_files = await backend.changed_paths(worktree)
        created_files = await backend.added_paths(worktree, record.base_commit)
        selected_patch_id = patch_id or _new_identifier("patch")
        patch = PatchSet(
            patch_id=selected_patch_id,
            transaction_id=record.transaction_id,
            base_commit=record.base_commit,
            acceptance_sha256=acceptance_hash,
            patch_sha256=sha256_digest(content),
            changed_files=changed_files,
            created_files=created_files,
            contains_binary_diff=(
                b"GIT binary patch" in content or b"Binary files " in content
            ),
            created_at=self._now(),
        )
        store.write_patch(patch, content)
        patching = self._transition(
            record,
            TransactionState.PATCHING,
            current_patch_id=patch.patch_id,
        )
        store.save_record(patching)
        return patch

    async def begin_verification(self, transaction_id: str) -> TransactionRecord:
        """锁定当前 PatchSet 并进入等待确定性 Verdict 的状态。"""
        await self._ensure_backend()
        store = self._require_store()
        record = store.load_record(transaction_id)
        if record.state is not TransactionState.PATCHING:
            raise TransactionError(
                "transaction.verification_state_invalid",
                "只有 PATCHING 事务可以开始验证",
            )
        self._verify_acceptance(record)
        self._load_current_patch(record)
        verifying = self._transition(record, TransactionState.VERIFYING)
        store.save_record(verifying)
        return verifying

    async def record_verdict(self, verdict: Verdict) -> TransactionRecord:
        """只接受与事务和验收哈希一致的程序化 Verdict。"""
        await self._ensure_backend()
        store = self._require_store()
        record = store.load_record(verdict.transaction_id)
        if record.state is not TransactionState.VERIFYING:
            raise TransactionError(
                "transaction.verdict_state_invalid",
                "只有 VERIFYING 事务可以接收 Verdict",
            )
        acceptance_hash = self._verify_acceptance(record)
        patch, _ = self._load_current_patch(record)
        if (
            verdict.base_commit != record.base_commit
            or verdict.acceptance_sha256 != acceptance_hash
            or verdict.patch_sha256 != patch.patch_sha256
        ):
            raise TransactionError(
                "transaction.verdict_binding_mismatch",
                "Verdict 未绑定当前基线、AcceptanceSpec 与 PatchSet",
            )
        manifest_digest = store.verify_verdict_evidence(
            verdict,
            expected_base_commit=record.base_commit,
            expected_patch_sha256=patch.patch_sha256,
        )
        target = {
            VerificationStatus.PASSED: TransactionState.VERIFIED,
            VerificationStatus.FAILED: TransactionState.REJECTED,
            VerificationStatus.INCONCLUSIVE: TransactionState.INCONCLUSIVE,
            VerificationStatus.BLOCKED: TransactionState.BLOCKED,
            VerificationStatus.CANCELLED: TransactionState.CANCELLED,
        }[verdict.status]
        decided = self._transition(
            record,
            target,
            evidence_id=verdict.evidence_id,
            evidence_manifest_path=verdict.evidence_manifest_path,
            evidence_manifest_sha256=manifest_digest,
        )
        store.save_record(decided)
        return decided

    async def apply(self, transaction_id: str) -> TransactionRecord:
        """复核三重哈希后检查并应用 patch，再清理 Worktree。"""
        backend = await self._ensure_backend()
        store = self._require_store()
        record = store.load_record(transaction_id)
        if record.state is TransactionState.APPLIED:
            self._verify_acceptance(record)
            patch, _ = self._load_current_patch(record)
            store.verify_record_evidence(
                record,
                expected_patch_sha256=patch.patch_sha256,
            )
            worktree = self._worktree_path_from(
                record.repository_identity, record.transaction_id
            )
            if worktree.exists():
                await self._cleanup_worktree(worktree)
            return record
        if record.state is not TransactionState.VERIFIED:
            raise TransactionError(
                "transaction.apply_not_verified",
                "只有 VERIFIED 事务允许 apply",
            )
        acceptance_hash = self._verify_acceptance(record)
        patch, stored_content = self._load_current_patch(record)
        store.verify_record_evidence(
            record,
            expected_patch_sha256=patch.patch_sha256,
        )
        evidence_manifest_sha256 = record.evidence_manifest_sha256
        if evidence_manifest_sha256 is None:
            raise TransactionError(
                "transaction.evidence_attestation_missing",
                "VERIFIED 事务缺少 Evidence manifest 哈希",
            )
        current_snapshot = await backend.inspect()
        if current_snapshot.repository_identity != record.repository_identity:
            raise TransactionError(
                "transaction.repository_identity_changed",
                "主仓库身份发生漂移",
            )
        patch_path = store.patch_path(record.transaction_id, patch.patch_id)
        intent: ApplyIntent | None = None
        if store.has_apply_intent(record.transaction_id):
            intent = store.load_apply_intent(record.transaction_id)
            self._validate_apply_intent(record, patch, intent)
        if current_snapshot.repository_fingerprint != record.repository_fingerprint:
            if intent is not None and await self._is_patch_applied(backend, patch_path):
                return await self._finalize_applied(record)
            raise TransactionError(
                "transaction.repository_drift",
                "主仓库在 apply 前发生漂移",
            )
        worktree = self._active_worktree(record)
        current_content = await backend.binary_diff(worktree, record.base_commit)
        if sha256_digest(current_content) != patch.patch_sha256:
            raise TransactionError(
                "transaction.patch_drift",
                "验证后发生补丁漂移",
            )
        if current_content != stored_content:
            raise TransactionError(
                "transaction.patch_bytes_changed",
                "持久化补丁与 Worktree 不一致",
            )
        if intent is None:
            intent = ApplyIntent(
                transaction_id=record.transaction_id,
                patch_id=patch.patch_id,
                base_commit=record.base_commit,
                acceptance_sha256=acceptance_hash,
                patch_sha256=patch.patch_sha256,
                evidence_manifest_sha256=evidence_manifest_sha256,
                repository_fingerprint=record.repository_fingerprint,
                created_at=self._now(),
            )
            store.write_apply_intent(intent)
            marker_snapshot = await backend.inspect()
            if marker_snapshot.repository_fingerprint != record.repository_fingerprint:
                raise TransactionError(
                    "transaction.repository_drift",
                    "主仓库在 apply 临界区发生漂移",
                )
        await backend.apply_patch(patch_path, check_only=True)
        await backend.apply_patch(patch_path, check_only=False)
        return await self._finalize_applied(record)

    async def abort(self, transaction_id: str) -> TransactionRecord:
        """幂等终止未进入 apply 临界区的事务并丢弃隔离 Worktree。"""
        await self._ensure_backend()
        store = self._require_store()
        record = store.load_record(transaction_id)
        if record.state in TERMINAL_STATES:
            return record
        if store.has_apply_intent(transaction_id):
            raise TransactionError(
                "transaction.apply_recovery_required",
                "事务已进入 apply 临界区，必须先确定性恢复 apply",
            )
        aborted = self._transition(record, TransactionState.ABORTED)
        store.save_record(aborted)
        worktree = self._worktree_path_from(
            record.repository_identity, record.transaction_id
        )
        await self._cleanup_worktree(worktree)
        return aborted

    async def scan_recovery(self) -> WorktreeRecoveryReport:
        """扫描 cache 下已登记 Worktree，区分恢复、孤儿和隔离项。"""
        backend = await self._ensure_backend()
        store = self._require_store()
        snapshot = await backend.inspect()
        container = self._worktree_container(snapshot.repository_identity)
        record_directories = {path.name: path for path in store.record_directories()}
        recoverable: list[RecoverableWorktree] = []
        orphans: list[OrphanWorktree] = []
        quarantined: list[OrphanWorktree] = []
        for path in await backend.list_worktrees():
            if not _is_relative_to(path, container):
                continue
            transaction_id = path.name
            state_directory = record_directories.get(transaction_id)
            if state_directory is None:
                orphans.append(OrphanWorktree(path))
                continue
            try:
                record = store.load_record(transaction_id)
                expected_path = self._worktree_path_from(
                    record.repository_identity, record.transaction_id
                )
            except TransactionError:
                quarantined.append(OrphanWorktree(path))
                continue
            if expected_path != path:
                quarantined.append(OrphanWorktree(path))
            elif record.state in TERMINAL_STATES:
                orphans.append(OrphanWorktree(path))
            else:
                recoverable.append(RecoverableWorktree(transaction_id, path))
        return WorktreeRecoveryReport(
            recoverable=tuple(
                sorted(recoverable, key=lambda item: item.transaction_id)
            ),
            orphans=tuple(sorted(orphans, key=lambda item: str(item.path))),
            quarantined=tuple(sorted(quarantined, key=lambda item: str(item.path))),
        )

    async def recover(self, transaction_id: str) -> TransactionRecord:
        """重新登记崩溃前的非终态 Worktree 并校验基线 HEAD。"""
        backend = await self._ensure_backend()
        record = self._require_store().load_record(transaction_id)
        if record.state in TERMINAL_STATES:
            raise TransactionError(
                "transaction.recovery_terminal",
                "终态事务不能恢复 Worktree",
            )
        worktree = await self._ensure_transaction_worktree(record)
        listed = await backend.list_worktrees()
        if (
            worktree not in listed
            or await backend.worktree_head(worktree) != record.base_commit
        ):
            raise TransactionError(
                "transaction.recovery_mismatch",
                "Worktree 登记或基线与事务记录不一致",
            )
        self._verify_acceptance(record)
        self._register_worktree(worktree)
        return record

    def suspend(self, transaction_id: str) -> TransactionRecord:
        """验证持久化记录后把活动 Worktree 移交给下次恢复。"""
        store = self._require_store()
        record = store.load_record(transaction_id)
        if record.state in TERMINAL_STATES:
            raise TransactionError(
                "transaction.suspend_terminal",
                "终态事务不需要挂起恢复",
            )
        if self._verification_worktrees:
            raise TransactionError(
                "transaction.suspend_verification_active",
                "验证临时 Worktree 尚未清理",
            )
        worktree = self._active_worktree(record)
        resolved = worktree.resolve(strict=False)
        if resolved not in self._registered_worktrees:
            raise TransactionError(
                "transaction.suspend_unregistered",
                "事务 Worktree 未登记到当前运行",
            )
        self._scope.transfer_persisted_worktree(resolved)
        self._registered_worktrees.remove(resolved)
        return record

    async def cleanup_orphans(self) -> tuple[Path, ...]:
        """只清理扫描中确认缺少活动记录的 cache Worktree。"""
        report = await self.scan_recovery()
        cleaned: list[Path] = []
        for orphan in report.orphans:
            await self._cleanup_worktree(orphan.path)
            cleaned.append(orphan.path)
        return tuple(cleaned)

    async def _ensure_backend(self) -> GitBackend:
        """延迟发现仓库并解析 XDG cache 与状态根。"""
        if self._backend is not None:
            return self._backend
        backend = await GitBackend.discover(self._candidate, scope=self._scope)
        runtime_paths = RuntimePaths.for_repository(backend.repository_root)
        cache_root = self._configured_cache_root or runtime_paths.cache_root
        state_root = self._configured_state_root or (
            runtime_paths.runtime_root / "transactions"
        )
        evidence_root = self._configured_evidence_root or runtime_paths.evidence_root
        cache_root = cache_root.resolve(strict=False)
        state_root = state_root.resolve(strict=False)
        evidence_root = evidence_root.resolve(strict=False)
        if _is_relative_to(cache_root, backend.repository_root) or _is_relative_to(
            backend.repository_root, cache_root
        ):
            raise TransactionError(
                "transaction.cache_not_isolated",
                "Worktree cache 必须位于主仓库之外",
            )
        if _is_relative_to(evidence_root, backend.repository_root) or _is_relative_to(
            backend.repository_root, evidence_root
        ):
            raise TransactionError(
                "transaction.evidence_not_isolated",
                "Evidence 根必须位于主仓库之外",
            )
        if _is_relative_to(evidence_root, state_root) or _is_relative_to(
            state_root, evidence_root
        ):
            raise TransactionError(
                "transaction.state_roots_overlap",
                "Transaction 与 Evidence 必须使用独立状态根",
            )
        self._backend = backend
        self._cache_root = cache_root
        self._store = TransactionStore(state_root, evidence_root=evidence_root)
        return backend

    @staticmethod
    async def _verification_repository_matches(
        backend: GitBackend,
        snapshot: RepositorySnapshot,
        record: TransactionRecord,
    ) -> bool:
        """Verify 仅容忍同一 HEAD 上唯一已跟踪项目配置的内容漂移。"""
        if snapshot.repository_identity != record.repository_identity:
            return False
        if snapshot.repository_fingerprint == record.repository_fingerprint:
            return True
        if (
            snapshot.head_commit != record.head_commit
            or snapshot.branch != record.branch
            or snapshot.detached_head != record.detached_head
            or snapshot.has_submodules != record.has_submodules
            or snapshot.submodule_status_sha256 != record.submodule_status_sha256
            or snapshot.git_config_summary != record.git_config_summary
            or snapshot.status_paths != (PROJECT_CONFIG_PATH,)
            or snapshot.untracked_paths
        ):
            return False
        return await backend.is_tracked_path(PROJECT_CONFIG_PATH)

    def _verify_acceptance(self, record: TransactionRecord) -> str:
        """验证记录与只读 AcceptanceSpec 文件保持同一哈希。"""
        acceptance_hash = record.acceptance_sha256
        self._require_store().load_acceptance(
            record.transaction_id,
            expected_sha256=acceptance_hash,
        )
        return acceptance_hash

    def _load_current_patch(self, record: TransactionRecord) -> tuple[PatchSet, bytes]:
        """加载当前 PatchSet 并核对事务、基线和验收绑定。"""
        patch_id = record.current_patch_id
        if patch_id is None:
            raise TransactionError(
                "transaction.current_patch_missing",
                "事务没有当前 PatchSet",
            )
        patch, content = self._require_store().load_patch(
            record.transaction_id, patch_id
        )
        if (
            patch.transaction_id != record.transaction_id
            or patch.base_commit != record.base_commit
            or patch.acceptance_sha256 != record.acceptance_sha256
        ):
            raise TransactionError(
                "transaction.patch_binding_mismatch",
                "PatchSet 未绑定当前事务基线与验收",
            )
        return patch, content

    @staticmethod
    def _validate_apply_intent(
        record: TransactionRecord,
        patch: PatchSet,
        intent: ApplyIntent,
    ) -> None:
        """拒绝与当前事务、补丁或创建指纹不一致的恢复意图。"""
        if (
            intent.transaction_id != record.transaction_id
            or intent.patch_id != patch.patch_id
            or intent.base_commit != record.base_commit
            or intent.acceptance_sha256 != record.acceptance_sha256
            or intent.patch_sha256 != patch.patch_sha256
            or intent.evidence_manifest_sha256 != record.evidence_manifest_sha256
            or intent.repository_fingerprint != record.repository_fingerprint
        ):
            raise TransactionError(
                "transaction.apply_intent_mismatch",
                "apply 恢复意图未绑定当前事务事实",
            )

    @staticmethod
    async def _is_patch_applied(backend: GitBackend, patch_path: Path) -> bool:
        """用 reverse check 判断崩溃前是否已完整应用补丁。"""
        try:
            await backend.apply_patch(
                patch_path,
                check_only=True,
                reverse=True,
            )
        except TransactionError as error:
            if error.code == "transaction.git_command_failed":
                return False
            raise
        return True

    async def _finalize_applied(self, record: TransactionRecord) -> TransactionRecord:
        """原子记录 APPLIED，并幂等回收可能仍存在的 Worktree。"""
        applied = self._transition(record, TransactionState.APPLIED)
        self._require_store().save_record(applied)
        worktree = self._worktree_path_from(
            record.repository_identity, record.transaction_id
        )
        await self._cleanup_worktree(worktree)
        return applied

    async def _ensure_transaction_worktree(self, record: TransactionRecord) -> Path:
        """幂等恢复事务 Worktree，并从持久 Patch 重建候选内容。"""
        backend = self._require_backend()
        worktree = self._worktree_path_from(
            record.repository_identity,
            record.transaction_id,
        )
        if worktree.exists():
            listed = await backend.list_worktrees()
            if (
                not worktree.is_dir()
                or worktree not in listed
                or await backend.worktree_head(worktree) != record.base_commit
            ):
                raise TransactionError(
                    "transaction.worktree_mismatch",
                    "现有事务 Worktree 与冻结基线不一致",
                )
            self._register_worktree(worktree)
            return worktree
        worktree.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        worktree.parent.chmod(0o700)
        try:
            # cache 目录可能在进程外被删除；先 prune Git 残留登记，避免同路径
            # worktree add 被一个已经不存在的旧 Worktree 阻塞。
            await backend.remove_worktree(worktree)
            await backend.add_worktree(worktree, record.base_commit)
            if record.current_patch_id is not None:
                patch, stored_content = self._load_current_patch(record)
                patch_path = self._require_store().patch_path(
                    record.transaction_id,
                    patch.patch_id,
                )
                await backend.apply_patch_to_worktree(
                    worktree,
                    patch_path,
                    check_only=True,
                )
                await backend.apply_patch_to_worktree(
                    worktree,
                    patch_path,
                    check_only=False,
                )
                rebuilt_content = await backend.binary_diff(
                    worktree,
                    record.base_commit,
                )
                if (
                    rebuilt_content != stored_content
                    or sha256_digest(rebuilt_content) != patch.patch_sha256
                ):
                    raise TransactionError(
                        "transaction.worktree_rebuild_patch_mismatch",
                        "重建 Worktree 的补丁与持久化 PatchSet 不一致",
                    )
        except BaseException:
            if worktree.exists():
                shutil.rmtree(worktree)
            with suppress(TransactionError):
                await backend.remove_worktree(worktree)
            raise
        self._register_worktree(worktree)
        return worktree

    def _active_worktree(self, record: TransactionRecord) -> Path:
        """返回存在且位于派生 cache 路径的活动 Worktree。"""
        worktree = self._worktree_path_from(
            record.repository_identity, record.transaction_id
        )
        if not worktree.is_dir():
            raise TransactionError(
                "transaction.worktree_missing",
                "事务 Worktree 不存在",
            )
        return worktree

    async def _cleanup_worktree(self, worktree: Path) -> None:
        """经 Git 清理 Worktree 后同步释放 ResourceScope 登记。"""
        await self._require_backend().remove_worktree(worktree)
        self._release_registered_worktree(worktree)

    def _register_worktree(self, worktree: Path) -> None:
        """为当前管理器生命周期登记唯一 Worktree 资源。"""
        resolved = worktree.resolve(strict=False)
        if resolved in self._registered_worktrees:
            return
        self._scope.register_worktree(
            resolved,
            cleanup=self._cleanup_worktree_on_scope_close,
            description="Git 事务 Worktree",
        )
        self._registered_worktrees.add(resolved)

    async def _cleanup_worktree_on_scope_close(self, worktree: Path) -> None:
        """在正常退出时清理 Worktree，并将非终态记录落为 ABORTED。"""
        await self._require_backend().cleanup_worktree_unscoped(worktree)
        try:
            record = self._require_store().load_record(worktree.name)
        except TransactionError:
            return
        if record.state in TERMINAL_STATES:
            return
        aborted = self._transition(record, TransactionState.ABORTED)
        self._require_store().save_record(aborted)

    async def _cleanup_verification_worktree_on_scope_close(
        self,
        worktree: Path,
    ) -> None:
        """退出时回收验证基线，不改变事务状态。"""
        await self._require_backend().cleanup_worktree_unscoped(worktree)
        self._verification_worktrees.discard(worktree.resolve(strict=False))
        self._prune_verification_directories(worktree.parent)

    def _prune_verification_directories(self, start: Path) -> None:
        """从已清理叶目录向上移除空验证容器。"""
        stop = (self._require_cache_root() / "verification").resolve(strict=False)
        cursor = start.resolve(strict=False)
        while cursor != stop and _is_relative_to(cursor, stop):
            try:
                cursor.rmdir()
            except OSError:
                return
            cursor = cursor.parent

    def _release_registered_worktree(self, worktree: Path) -> None:
        """释放本管理器曾登记且现已消失的 Worktree。"""
        resolved = worktree.resolve(strict=False)
        if resolved not in self._registered_worktrees:
            return
        self._scope.release_worktree(resolved)
        self._registered_worktrees.remove(resolved)

    def _worktree_container(self, repository_identity: str) -> Path:
        """按仓库身份隔离不同仓库的事务路径。"""
        digest = repository_identity.removeprefix("sha256:")
        if len(digest) != 64 or any(
            character not in "0123456789abcdef" for character in digest
        ):
            raise TransactionError(
                "transaction.repository_identity_invalid",
                "仓库身份哈希无效",
            )
        return self._require_cache_root() / "worktrees" / digest

    def _worktree_path_from(
        self,
        repository_identity: str,
        transaction_id: str,
    ) -> Path:
        """从已校验记录派生唯一 Worktree 路径。"""
        store = self._require_store()
        store.transaction_directory(transaction_id)
        path = self._worktree_container(repository_identity) / transaction_id
        resolved = path.resolve(strict=False)
        if not _is_relative_to(resolved, self._require_cache_root() / "worktrees"):
            raise TransactionError(
                "transaction.worktree_path_escape",
                "事务 Worktree 路径越过 cache 根",
            )
        return resolved

    def _transition(
        self,
        record: TransactionRecord,
        target: TransactionState,
        **updates: object,
    ) -> TransactionRecord:
        """验证状态边后重新执行严格 TransactionRecord 校验。"""
        validate_transition(record.state, target)
        payload = record.model_dump(mode="python")
        payload.update(updates)
        payload["state"] = target
        payload["updated_at"] = self._now()
        return TransactionRecord.model_validate(payload)

    def _now(self) -> datetime:
        """获取可注入且必须带时区的事务时间。"""
        value = self._clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise TransactionError(
                "transaction.clock_naive",
                "事务时钟必须返回带时区时间",
            )
        return value

    def _require_backend(self) -> GitBackend:
        """返回已完成显式发现的 Git backend。"""
        if self._backend is None:
            raise TransactionError(
                "transaction.not_initialized",
                "事务管理器尚未发现仓库",
            )
        return self._backend

    def _require_store(self) -> TransactionStore:
        """返回已解析的事务状态存储。"""
        if self._store is None:
            raise TransactionError(
                "transaction.not_initialized",
                "事务管理器尚未解析状态根",
            )
        return self._store

    def _require_cache_root(self) -> Path:
        """返回已解析的 XDG cache 根。"""
        if self._cache_root is None:
            raise TransactionError(
                "transaction.not_initialized",
                "事务管理器尚未解析 cache 根",
            )
        return self._cache_root
