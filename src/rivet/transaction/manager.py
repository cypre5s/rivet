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
    DirtyPolicy,
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
        expected_behaviors: tuple[str, ...],
        preserved_behaviors: tuple[str, ...],
        verification_commands: tuple[Command, ...],
        max_wall_seconds: int,
        max_tokens: int,
        max_tool_calls: int,
        forbidden_paths: tuple[RepositoryPath, ...] = (),
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
            allowed_paths=allowed_paths,
            forbidden_paths=forbidden_paths,
            expected_behaviors=expected_behaviors,
            preserved_behaviors=preserved_behaviors,
            verification_commands=verification_commands,
            max_wall_seconds=max_wall_seconds,
            max_tokens=max_tokens,
            max_tool_calls=max_tool_calls,
            max_cost_usd=max_cost_usd,
            acceptable_risks=acceptable_risks,
            non_goals=non_goals,
        )

    async def create(
        self,
        *,
        transaction_id: str | None = None,
        dirty_policy: DirtyPolicy = DirtyPolicy.REJECT,
    ) -> TransactionRecord:
        """创建独立 Worktree，脏仓库仅在显式策略下生成快照。"""
        backend = await self._ensure_backend()
        store = self._require_store()
        store.prepare()
        selected_id = transaction_id or _new_identifier("tx")
        record_path = store.record_path(selected_id)
        if record_path.exists():
            raise TransactionError(
                "transaction.already_exists",
                "事务 ID 已存在",
            )
        snapshot = await backend.inspect()
        if snapshot.dirty and dirty_policy is DirtyPolicy.REJECT:
            raise TransactionError(
                "transaction.dirty_repository_rejected",
                "检测到脏工作区，必须显式选择 snapshot 模式",
            )
        created_at = self._now()
        dirty_snapshot_hash: str | None = None
        base_commit = snapshot.head_commit
        if snapshot.dirty:
            temporary_index = (
                self._require_cache_root() / "snapshot-indexes" / f"{selected_id}.index"
            )
            dirty_snapshot_hash = await backend.create_dirty_snapshot(
                snapshot,
                transaction_id=selected_id,
                temporary_index=temporary_index,
                timestamp=created_at,
            )
            base_commit = dirty_snapshot_hash
        worktree = self._worktree_path_from(snapshot.repository_identity, selected_id)
        if worktree.exists():
            raise TransactionError(
                "transaction.worktree_exists",
                "事务 Worktree 路径已存在",
            )
        worktree.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        worktree.parent.chmod(0o700)
        try:
            await backend.add_worktree(worktree, base_commit)
        except BaseException:
            if worktree.exists():
                shutil.rmtree(worktree)
            with suppress(TransactionError):
                await backend.remove_worktree(worktree)
            raise
        try:
            self._register_worktree(worktree)
            record = TransactionRecord(
                transaction_id=selected_id,
                state=TransactionState.CREATED,
                repository_identity=snapshot.repository_identity,
                repository_fingerprint=snapshot.repository_fingerprint,
                head_commit=snapshot.head_commit,
                base_commit=base_commit,
                branch=snapshot.branch,
                detached_head=snapshot.detached_head,
                dirty=snapshot.dirty,
                dirty_snapshot_hash=dirty_snapshot_hash,
                has_submodules=snapshot.has_submodules,
                submodule_status_sha256=snapshot.submodule_status_sha256,
                git_config_summary=snapshot.git_config_summary,
                created_at=created_at,
                updated_at=created_at,
            )
            store.save_record(record)
            if snapshot.dirty:
                record = self._transition(record, TransactionState.SNAPSHOTTED)
                store.save_record(record)
            record = self._transition(record, TransactionState.BASELINED)
            store.save_record(record)
        except BaseException:
            await backend.remove_worktree(worktree)
            self._release_registered_worktree(worktree)
            raise
        return record

    async def freeze_acceptance(
        self,
        transaction_id: str,
        specification: AcceptanceSpec,
        *,
        confirmed: bool,
    ) -> str:
        """经显式确认只写一次 AcceptanceSpec，并记录规范哈希。"""
        if not confirmed:
            raise TransactionError(
                "transaction.acceptance_confirmation_required",
                "冻结 AcceptanceSpec 需要显式确认",
            )
        await self._ensure_backend()
        store = self._require_store()
        record = store.load_record(transaction_id)
        digest = acceptance_sha256(specification)
        if record.acceptance_sha256 is not None:
            existing = store.load_acceptance(
                transaction_id,
                expected_sha256=record.acceptance_sha256,
            )
            if existing != specification or digest != record.acceptance_sha256:
                raise TransactionError(
                    "transaction.acceptance_frozen",
                    "AcceptanceSpec 已冻结且不可修改",
                )
            return record.acceptance_sha256
        if record.state is not TransactionState.BASELINED:
            raise TransactionError(
                "transaction.acceptance_state_invalid",
                "只有 BASELINED 事务可以冻结 AcceptanceSpec",
            )
        stored_digest = store.write_acceptance(transaction_id, specification)
        planned = self._transition(
            record,
            TransactionState.PLANNED,
            acceptance_sha256=stored_digest,
        )
        store.save_record(planned)
        return stored_digest

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
        return WorkspaceBoundary(backend.repository_root, worktree)

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
        """返回事务私有状态内由验证模块管理的证据根。"""
        store = self._require_store()
        store.load_record(transaction_id)
        return store.transaction_directory(transaction_id) / "evidence"

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
        if (
            snapshot.repository_identity != record.repository_identity
            or snapshot.repository_fingerprint != record.repository_fingerprint
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
        changed_symbols: tuple[str, ...] = (),
    ) -> PatchSet:
        """生成完整 binary diff、保存 PatchSet 并进入 PATCHING。"""
        backend = await self._ensure_backend()
        store = self._require_store()
        record = store.load_record(transaction_id)
        if record.state not in {
            TransactionState.PLANNED,
            TransactionState.PATCHING,
            TransactionState.REJECTED,
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
        selected_patch_id = patch_id or _new_identifier("patch")
        patch = PatchSet(
            patch_id=selected_patch_id,
            transaction_id=record.transaction_id,
            base_commit=record.base_commit,
            acceptance_sha256=acceptance_hash,
            patch_sha256=sha256_digest(content),
            changed_files=changed_files,
            changed_symbols=changed_symbols,
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
        if verdict.acceptance_sha256 != acceptance_hash:
            raise TransactionError(
                "transaction.verdict_acceptance_mismatch",
                "Verdict 未绑定当前 AcceptanceSpec",
            )
        manifest_digest = store.verify_verdict_evidence(
            verdict,
            expected_patch_sha256=patch.patch_sha256 if verdict.passed else None,
        )
        target = (
            TransactionState.VERIFIED
            if verdict.status is VerificationStatus.PASSED and verdict.passed
            else TransactionState.REJECTED
        )
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
        self._verify_acceptance(record)
        patch, stored_content = self._load_current_patch(record)
        store.verify_record_evidence(
            record,
            expected_patch_sha256=patch.patch_sha256,
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
                patch_sha256=patch.patch_sha256,
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
        """幂等终止非应用事务并明确丢弃隔离 Worktree。"""
        await self._ensure_backend()
        store = self._require_store()
        record = store.load_record(transaction_id)
        if record.state in TERMINAL_STATES:
            return record
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
        worktree = self._active_worktree(record)
        listed = await backend.list_worktrees()
        if (
            worktree not in listed
            or await backend.worktree_head(worktree) != record.base_commit
        ):
            raise TransactionError(
                "transaction.recovery_mismatch",
                "Worktree 登记或基线与事务记录不一致",
            )
        if record.acceptance_sha256 is not None:
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
        cache_root = cache_root.resolve(strict=False)
        state_root = state_root.resolve(strict=False)
        if _is_relative_to(cache_root, backend.repository_root) or _is_relative_to(
            backend.repository_root, cache_root
        ):
            raise TransactionError(
                "transaction.cache_not_isolated",
                "Worktree cache 必须位于主仓库之外",
            )
        self._backend = backend
        self._cache_root = cache_root
        self._store = TransactionStore(state_root)
        return backend

    def _verify_acceptance(self, record: TransactionRecord) -> str:
        """验证记录与只读 AcceptanceSpec 文件保持同一哈希。"""
        acceptance_hash = record.acceptance_sha256
        if acceptance_hash is None:
            raise TransactionError(
                "transaction.acceptance_not_frozen",
                "事务尚未冻结 AcceptanceSpec",
            )
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
            or intent.patch_sha256 != patch.patch_sha256
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
