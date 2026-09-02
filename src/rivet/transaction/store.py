"""持久化事务记录、冻结验收与完整 binary patch 历史。"""

from __future__ import annotations

import fcntl
import os
import re
import shutil
import tempfile
from pathlib import Path
from typing import TypeVar

from pydantic import ValidationError

from rivet.contracts.transactions import (
    AcceptanceSpec,
    PatchSet,
    TransactionRecord,
    TransactionState,
)
from rivet.contracts.verification import EvidenceManifest, Verdict

from .errors import TransactionError
from .hashing import acceptance_sha256, canonical_json_bytes, sha256_digest
from .models import ApplyIntent

MAX_RECORD_BYTES = 1024 * 1024
MAX_ACCEPTANCE_BYTES = 4 * 1024 * 1024
MAX_PATCH_BYTES = 64 * 1024 * 1024
MAX_TRANSACTION_LIST_ENTRIES = 10_000
MAX_TRANSACTION_LIST_LIMIT = 100
ModelT = TypeVar(
    "ModelT",
    AcceptanceSpec,
    ApplyIntent,
    PatchSet,
    TransactionRecord,
)
TRANSACTION_ID_PATTERN = re.compile(r"^tx_[a-z0-9][a-z0-9_-]{0,62}$")
PATCH_ID_PATTERN = re.compile(r"^patch_[a-z0-9][a-z0-9_-]{0,62}$")
EVIDENCE_ATTEMPT_PATTERN = re.compile(r"^attempt_[0-9]{4}$")
FROZEN_PUBLISH_STAGING_PATTERN = re.compile(
    r"^\.tx_[a-z0-9][a-z0-9_-]{0,62}\.publish-[a-z0-9_]+$"
)
REQUIRED_ATTESTATION_FILES = frozenset(
    {
        "acceptance_spec.json",
        "patch.diff",
        "baseline.log",
        "behavior.log",
        "regression.log",
        "scope_check.json",
        "secret_scan.json",
        "binding_check.json",
        "resource_check.json",
        "matrix.json",
        "verdict.json",
        "summary.md",
    }
)
MAX_EVIDENCE_TOTAL_BYTES = 128 * 1024 * 1024


def _validated_transaction_id(transaction_id: str) -> str:
    """在拼接本地状态路径前严格验证事务 ID。"""
    if not TRANSACTION_ID_PATTERN.fullmatch(transaction_id):
        raise TransactionError(
            "transaction.id_invalid",
            "事务 ID 格式无效",
        )
    return transaction_id


def _validated_patch_id(patch_id: str) -> str:
    """在拼接补丁路径前严格验证 Patch ID。"""
    if not PATCH_ID_PATTERN.fullmatch(patch_id):
        raise TransactionError(
            "transaction.patch_id_invalid",
            "Patch ID 格式无效",
        )
    return patch_id


class TransactionStore:
    """在私有状态根内原子保存可恢复事务事实。"""

    def __init__(self, state_root: Path, *, evidence_root: Path | None = None) -> None:
        self.state_root = state_root.resolve(strict=False)
        self.evidence_root = (
            evidence_root.resolve(strict=False)
            if evidence_root is not None
            else (self.state_root.parent / "evidence").resolve(strict=False)
        )

    def prepare(self) -> None:
        """显式创建私有状态根并拒绝符号链接。"""
        for root, label in (
            (self.state_root, "事务状态根"),
            (self.evidence_root, "Evidence 状态根"),
        ):
            if root.is_symlink():
                raise TransactionError(
                    "transaction.state_root_symlink",
                    f"{label}不得是符号链接",
                )
            root.mkdir(parents=True, exist_ok=True, mode=0o700)
            root.chmod(0o700)
        self._cleanup_stale_frozen_publications()

    def transaction_directory(self, transaction_id: str) -> Path:
        """返回经过 ID 校验且不能逃逸的事务状态目录。"""
        validated = _validated_transaction_id(transaction_id)
        path = self.state_root / validated
        if path.is_symlink():
            raise TransactionError(
                "transaction.state_path_symlink",
                "事务状态路径不得是符号链接",
            )
        return path

    def evidence_directory(self, transaction_id: str) -> Path:
        """返回独立 Evidence 根内经过事务 ID 校验的目录。"""
        validated = _validated_transaction_id(transaction_id)
        path = self.evidence_root / validated
        if path.is_symlink():
            raise TransactionError(
                "transaction.evidence_path_symlink",
                "Evidence 事务目录不得是符号链接",
            )
        return path

    def evidence_manifest_path(
        self,
        transaction_id: str,
        manifest_relative_path: str,
    ) -> Path:
        """解析 `<tx>/<attempt>/manifest.json`，拒绝跨事务路径。"""
        validated = _validated_transaction_id(transaction_id)
        relative = Path(manifest_relative_path)
        if (
            relative.is_absolute()
            or len(relative.parts) != 3
            or relative.parts[0] != validated
            or not EVIDENCE_ATTEMPT_PATTERN.fullmatch(relative.parts[1])
            or relative.parts[2] != "manifest.json"
        ):
            raise TransactionError(
                "transaction.evidence_path_invalid",
                "Evidence manifest 路径无效",
            )
        cursor = self.evidence_root
        for part in relative.parts:
            cursor /= part
            if cursor.is_symlink():
                raise TransactionError(
                    "transaction.evidence_path_symlink",
                    "Evidence 路径不得包含符号链接",
                )
        return self.evidence_root / relative

    def record_path(self, transaction_id: str) -> Path:
        """返回事务记录路径。"""
        return self.transaction_directory(transaction_id) / "record.json"

    def acceptance_path(self, transaction_id: str) -> Path:
        """返回冻结 AcceptanceSpec JSON 路径。"""
        return self.transaction_directory(transaction_id) / "acceptance_spec.json"

    def acceptance_hash_path(self, transaction_id: str) -> Path:
        """返回冻结 AcceptanceSpec 哈希路径。"""
        return self.transaction_directory(transaction_id) / "acceptance_spec.sha256"

    def patch_path(self, transaction_id: str, patch_id: str) -> Path:
        """返回经过双重 ID 校验的 binary patch 路径。"""
        validated_patch = _validated_patch_id(patch_id)
        return (
            self.transaction_directory(transaction_id)
            / "patches"
            / f"{validated_patch}.diff"
        )

    def patch_record_path(self, transaction_id: str, patch_id: str) -> Path:
        """返回 PatchSet JSON 路径。"""
        validated_patch = _validated_patch_id(patch_id)
        return (
            self.transaction_directory(transaction_id)
            / "patches"
            / f"{validated_patch}.json"
        )

    def apply_intent_path(self, transaction_id: str) -> Path:
        """返回 apply 崩溃恢复意图路径。"""
        return self.transaction_directory(transaction_id) / "apply_intent.json"

    def save_record(self, record: TransactionRecord) -> None:
        """只在已原子发布的事务目录中覆盖记录并 fsync。"""
        directory = self.transaction_directory(record.transaction_id)
        if not directory.is_dir():
            raise TransactionError(
                "transaction.record_parent_missing",
                "事务尚未原子发布，不能单独保存记录",
            )
        directory.chmod(0o700)
        self._atomic_write(
            self.record_path(record.transaction_id),
            canonical_json_bytes(record.model_dump(mode="json")) + b"\n",
            mode=0o600,
        )

    def publish_frozen_transaction(
        self,
        record: TransactionRecord,
        specification: AcceptanceSpec,
    ) -> None:
        """以一次目录 rename 原子发布首个记录、验收规范和规范哈希。"""
        if (
            record.state is not TransactionState.ACCEPTANCE_FROZEN
            or record.current_patch_id is not None
            or record.evidence_id is not None
            or record.evidence_manifest_path is not None
            or record.evidence_manifest_sha256 is not None
        ):
            raise TransactionError(
                "transaction.frozen_publish_invalid",
                "首个事务发布必须是无补丁、无证据的 ACCEPTANCE_FROZEN 记录",
            )
        digest = acceptance_sha256(specification)
        if record.acceptance_sha256 != digest:
            raise TransactionError(
                "transaction.acceptance_hash_mismatch",
                "首个事务记录未绑定给定 AcceptanceSpec",
            )
        final_directory = self.transaction_directory(record.transaction_id)
        root_descriptor = self._open_state_root()
        staging_directory: Path | None = None
        published = False
        try:
            fcntl.flock(root_descriptor, fcntl.LOCK_EX)
            if final_directory.exists():
                raise TransactionError(
                    "transaction.already_exists",
                    "事务 ID 已存在",
                )
            staging_directory = Path(
                tempfile.mkdtemp(
                    prefix=f".{record.transaction_id}.publish-",
                    dir=self.state_root,
                )
            )
            staging_directory.chmod(0o700)
            self._atomic_write(
                staging_directory / "acceptance_spec.json",
                canonical_json_bytes(specification.model_dump(mode="json")) + b"\n",
                mode=0o400,
            )
            self._atomic_write(
                staging_directory / "acceptance_spec.sha256",
                f"{digest}\n".encode("ascii"),
                mode=0o400,
            )
            self._atomic_write(
                staging_directory / "record.json",
                canonical_json_bytes(record.model_dump(mode="json")) + b"\n",
                mode=0o600,
            )
            self._fsync_directory(staging_directory)
            os.rename(staging_directory, final_directory)
            published = True
            os.fsync(root_descriptor)
        except TransactionError:
            raise
        except OSError as error:
            raise TransactionError(
                "transaction.frozen_publish_failed",
                "无法原子发布冻结事务事实",
            ) from error
        finally:
            if staging_directory is not None and not published:
                shutil.rmtree(staging_directory, ignore_errors=True)
            fcntl.flock(root_descriptor, fcntl.LOCK_UN)
            os.close(root_descriptor)

    def load_record(self, transaction_id: str) -> TransactionRecord:
        """严格加载一个存在的事务记录。"""
        return self._load_model(
            self.record_path(transaction_id),
            TransactionRecord,
            max_bytes=MAX_RECORD_BYTES,
            missing_code="transaction.record_missing",
            invalid_code="transaction.record_invalid",
        )

    def load_acceptance(
        self,
        transaction_id: str,
        *,
        expected_sha256: str,
    ) -> AcceptanceSpec:
        """同时验证 JSON 内容、旁路哈希和记录中的冻结哈希。"""
        specification_path = self.acceptance_path(transaction_id)
        hash_path = self.acceptance_hash_path(transaction_id)
        specification = self._load_model(
            specification_path,
            AcceptanceSpec,
            max_bytes=MAX_ACCEPTANCE_BYTES,
            missing_code="transaction.acceptance_missing",
            invalid_code="transaction.acceptance_invalid",
        )
        try:
            stored_hash = hash_path.read_text(encoding="ascii", errors="strict").strip()
        except (OSError, UnicodeError) as error:
            raise TransactionError(
                "transaction.acceptance_hash_unreadable",
                "AcceptanceSpec 哈希文件不可读",
            ) from error
        actual_hash = acceptance_sha256(specification)
        if stored_hash != expected_sha256 or actual_hash != expected_sha256:
            raise TransactionError(
                "transaction.acceptance_hash_mismatch",
                "AcceptanceSpec 冻结哈希不匹配",
            )
        return specification

    def write_patch(self, patch: PatchSet, content: bytes) -> None:
        """按 Patch ID 保存不可变 diff 和对应契约。"""
        if len(content) > MAX_PATCH_BYTES:
            raise TransactionError(
                "transaction.patch_size_exceeded",
                "binary patch 超过事务上限",
            )
        if sha256_digest(content) != patch.patch_sha256:
            raise TransactionError(
                "transaction.patch_hash_mismatch",
                "PatchSet 哈希与 diff 不一致",
            )
        patch_path = self.patch_path(patch.transaction_id, patch.patch_id)
        record_path = self.patch_record_path(patch.transaction_id, patch.patch_id)
        if patch_path.exists() or record_path.exists():
            raise TransactionError(
                "transaction.patch_exists",
                "Patch ID 已存在",
            )
        patch_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        patch_path.parent.chmod(0o700)
        self._atomic_write(patch_path, content, mode=0o600)
        self._atomic_write(
            record_path,
            canonical_json_bytes(patch.model_dump(mode="json")) + b"\n",
            mode=0o600,
        )

    def load_patch(self, transaction_id: str, patch_id: str) -> tuple[PatchSet, bytes]:
        """加载 PatchSet 并重新计算完整 diff 哈希。"""
        patch = self._load_model(
            self.patch_record_path(transaction_id, patch_id),
            PatchSet,
            max_bytes=MAX_RECORD_BYTES,
            missing_code="transaction.patch_record_missing",
            invalid_code="transaction.patch_record_invalid",
        )
        path = self.patch_path(transaction_id, patch_id)
        try:
            content = path.read_bytes()
        except OSError as error:
            raise TransactionError(
                "transaction.patch_missing",
                "事务 diff 不可读",
            ) from error
        if (
            len(content) > MAX_PATCH_BYTES
            or sha256_digest(content) != patch.patch_sha256
        ):
            raise TransactionError(
                "transaction.patch_hash_mismatch",
                "事务 diff 哈希不匹配",
            )
        return patch, content

    def write_apply_intent(self, intent: ApplyIntent) -> None:
        """在主工作区变更前只写一次 apply 意图。"""
        path = self.apply_intent_path(intent.transaction_id)
        if path.exists():
            existing = self.load_apply_intent(intent.transaction_id)
            if existing != intent:
                raise TransactionError(
                    "transaction.apply_intent_mismatch",
                    "现有 apply 意图与当前补丁不一致",
                )
            return
        self._atomic_write(
            path,
            canonical_json_bytes(intent.model_dump(mode="json")) + b"\n",
            mode=0o600,
        )

    def load_apply_intent(self, transaction_id: str) -> ApplyIntent:
        """严格读取一次已持久化 apply 意图。"""
        return self._load_model(
            self.apply_intent_path(transaction_id),
            ApplyIntent,
            max_bytes=MAX_RECORD_BYTES,
            missing_code="transaction.apply_intent_missing",
            invalid_code="transaction.apply_intent_invalid",
        )

    def has_apply_intent(self, transaction_id: str) -> bool:
        """指示事务是否已进入可恢复 apply 临界区。"""
        return self.apply_intent_path(transaction_id).is_file()

    def verify_verdict_evidence(
        self,
        verdict: Verdict,
        *,
        expected_base_commit: str,
        expected_patch_sha256: str,
    ) -> str:
        """复核 Verdict 所指清单和全部文件，并返回 manifest 哈希。"""
        manifest_digest, persisted_verdict = self._verify_evidence_bundle(
            transaction_id=verdict.transaction_id,
            acceptance_sha256=verdict.acceptance_sha256,
            evidence_id=verdict.evidence_id,
            manifest_relative_path=verdict.evidence_manifest_path,
            expected_base_commit=expected_base_commit,
            expected_patch_sha256=expected_patch_sha256,
        )
        if persisted_verdict != verdict:
            raise TransactionError(
                "transaction.evidence_verdict_mismatch",
                "Evidence 中的 Verdict 与待记录结论不一致",
            )
        return manifest_digest

    def verify_record_evidence(
        self,
        record: TransactionRecord,
        *,
        expected_patch_sha256: str,
    ) -> Verdict:
        """在 apply 前按事务记录重新验证 EvidenceBundle。"""
        if (
            record.evidence_id is None
            or record.evidence_manifest_path is None
            or record.evidence_manifest_sha256 is None
        ):
            raise TransactionError(
                "transaction.evidence_attestation_missing",
                "事务缺少 Evidence manifest 绑定",
            )
        manifest_digest, verdict = self._verify_evidence_bundle(
            transaction_id=record.transaction_id,
            acceptance_sha256=record.acceptance_sha256,
            evidence_id=record.evidence_id,
            manifest_relative_path=record.evidence_manifest_path,
            expected_base_commit=record.base_commit,
            expected_patch_sha256=expected_patch_sha256,
        )
        if manifest_digest != record.evidence_manifest_sha256:
            raise TransactionError(
                "transaction.evidence_manifest_hash_mismatch",
                "Evidence manifest 哈希与事务记录不一致",
            )
        if record.state in {TransactionState.VERIFIED, TransactionState.APPLIED}:
            expected_passed = True
        elif record.state in {
            TransactionState.REJECTED,
            TransactionState.INCONCLUSIVE,
            TransactionState.BLOCKED,
            TransactionState.CANCELLED,
        }:
            expected_passed = False
        else:
            raise TransactionError(
                "transaction.evidence_state_invalid",
                "当前事务状态不能复核交付证据",
            )
        if verdict.passed is not expected_passed:
            raise TransactionError(
                "transaction.evidence_state_mismatch",
                "Evidence Verdict 与事务状态不一致",
            )
        return verdict

    def _verify_evidence_bundle(
        self,
        *,
        transaction_id: str,
        acceptance_sha256: str,
        evidence_id: str,
        manifest_relative_path: str,
        expected_base_commit: str,
        expected_patch_sha256: str,
    ) -> tuple[str, Verdict]:
        """验证受限 attempt 路径、manifest 契约和全量文件哈希。"""
        manifest_path = self.evidence_manifest_path(
            transaction_id,
            manifest_relative_path,
        )
        try:
            if manifest_path.stat().st_size > MAX_RECORD_BYTES:
                raise TransactionError(
                    "transaction.evidence_manifest_invalid",
                    "Evidence manifest 超过大小上限",
                )
            manifest_content = manifest_path.read_bytes()
            manifest = EvidenceManifest.model_validate_json(manifest_content)
        except TransactionError:
            raise
        except (OSError, ValidationError) as error:
            raise TransactionError(
                "transaction.evidence_manifest_invalid",
                "Evidence manifest 无法读取或校验",
            ) from error
        if (
            manifest.transaction_id != transaction_id
            or manifest.base_commit != expected_base_commit
            or manifest.acceptance_sha256 != acceptance_sha256
            or manifest.patch_sha256 != expected_patch_sha256
            or manifest.evidence_id != evidence_id
        ):
            raise TransactionError(
                "transaction.evidence_manifest_mismatch",
                "Evidence manifest 未绑定当前事务、基线、验收条件与补丁",
            )
        attempt_directory = manifest_path.parent
        expected_paths = {evidence_file.path for evidence_file in manifest.files}
        if not expected_paths >= REQUIRED_ATTESTATION_FILES:
            raise TransactionError(
                "transaction.evidence_files_missing",
                "Evidence manifest 缺少交付门禁文件",
            )
        patch_file = next(
            evidence_file
            for evidence_file in manifest.files
            if evidence_file.path == "patch.diff"
        )
        if not manifest.patch_redacted and patch_file.sha256 != expected_patch_sha256:
            raise TransactionError(
                "transaction.evidence_patch_mismatch",
                "Evidence patch 与冻结 PatchSet 不一致",
            )
        entries = tuple(attempt_directory.iterdir())
        if any(
            entry.is_symlink()
            or not entry.is_file()
            or len(entry.relative_to(attempt_directory).parts) != 1
            for entry in entries
        ):
            raise TransactionError(
                "transaction.evidence_file_set_mismatch",
                "Evidence attempt 包含非普通文件",
            )
        actual_paths = {entry.name for entry in entries}
        if actual_paths != expected_paths | {"manifest.json"}:
            raise TransactionError(
                "transaction.evidence_file_set_mismatch",
                "Evidence 文件集合与 manifest 不一致",
            )
        total_bytes = 0
        for evidence_file in manifest.files:
            evidence_path = attempt_directory / evidence_file.path
            try:
                if evidence_path.stat().st_size > MAX_PATCH_BYTES:
                    raise TransactionError(
                        "transaction.evidence_file_too_large",
                        "Evidence 文件超过大小上限",
                    )
                content = evidence_path.read_bytes()
            except TransactionError:
                raise
            except OSError as error:
                raise TransactionError(
                    "transaction.evidence_file_unreadable",
                    "Evidence 文件不可读",
                ) from error
            total_bytes += len(content)
            if (
                total_bytes > MAX_EVIDENCE_TOTAL_BYTES
                or len(content) != evidence_file.size_bytes
                or sha256_digest(content) != evidence_file.sha256
            ):
                raise TransactionError(
                    "transaction.evidence_hash_mismatch",
                    "Evidence 文件大小或哈希不匹配",
                )
        try:
            persisted_verdict = Verdict.model_validate_json(
                (attempt_directory / "verdict.json").read_bytes()
            )
        except (OSError, ValidationError) as error:
            raise TransactionError(
                "transaction.evidence_verdict_invalid",
                "Evidence Verdict 无法读取或校验",
            ) from error
        if (
            persisted_verdict.transaction_id != transaction_id
            or persisted_verdict.base_commit != expected_base_commit
            or persisted_verdict.acceptance_sha256 != acceptance_sha256
            or persisted_verdict.patch_sha256 != expected_patch_sha256
            or persisted_verdict.evidence_id != evidence_id
        ):
            raise TransactionError(
                "transaction.evidence_verdict_binding_mismatch",
                "Evidence Verdict 未绑定当前事务事实",
            )
        if persisted_verdict.passed and manifest.patch_redacted:
            raise TransactionError(
                "transaction.evidence_patch_redacted",
                "通过结论不得使用已脱敏而不可复算的补丁证据",
            )
        return sha256_digest(manifest_content), persisted_verdict

    def record_directories(self) -> tuple[Path, ...]:
        """稳定列出潜在事务目录，不跟随非目录节点。"""
        if not self.state_root.is_dir():
            return ()
        return tuple(
            sorted(
                path
                for path in self.state_root.iterdir()
                if path.is_dir() and not path.is_symlink()
            )
        )

    def list_recent_records(self, *, limit: int = 20) -> tuple[TransactionRecord, ...]:
        """按记录修改时间列出经过契约校验的近期事务。"""
        if isinstance(limit, bool) or not 1 <= limit <= MAX_TRANSACTION_LIST_LIMIT:
            raise TransactionError(
                "transaction.list_limit_invalid",
                "事务列表上限无效",
            )
        if self.state_root.is_symlink():
            raise TransactionError(
                "transaction.state_root_symlink",
                "事务状态根不得是符号链接",
            )
        if not self.state_root.exists():
            return ()
        if not self.state_root.is_dir():
            raise TransactionError(
                "transaction.state_root_invalid",
                "事务状态根不是目录",
            )
        candidates: list[tuple[int, TransactionRecord]] = []
        try:
            for index, directory in enumerate(self.state_root.iterdir()):
                if index >= MAX_TRANSACTION_LIST_ENTRIES:
                    raise TransactionError(
                        "transaction.list_too_large",
                        "事务目录数量超过上限",
                    )
                if (
                    directory.is_symlink()
                    or not directory.is_dir()
                    or not TRANSACTION_ID_PATTERN.fullmatch(directory.name)
                ):
                    continue
                record_path = directory / "record.json"
                if record_path.is_symlink() or not record_path.is_file():
                    continue
                try:
                    record = self.load_record(directory.name)
                    modified_ns = record_path.stat().st_mtime_ns
                except (OSError, TransactionError):
                    continue
                candidates.append((modified_ns, record))
        except OSError as error:
            raise TransactionError(
                "transaction.list_unreadable",
                "事务状态目录无法读取",
            ) from error
        candidates.sort(
            key=lambda item: (-item[0], item[1].transaction_id),
        )
        return tuple(record for _, record in candidates[:limit])

    @staticmethod
    def _load_model(
        path: Path,
        model_type: type[ModelT],
        *,
        max_bytes: int,
        missing_code: str,
        invalid_code: str,
    ) -> ModelT:
        """有界读取严格 Pydantic 模型并隐藏原始内容。"""
        try:
            if path.stat().st_size > max_bytes:
                raise TransactionError(invalid_code, "事务状态文件超过上限")
            content = path.read_bytes()
        except FileNotFoundError as error:
            raise TransactionError(missing_code, "事务状态文件不存在") from error
        except OSError as error:
            raise TransactionError(invalid_code, "事务状态文件不可读") from error
        try:
            return model_type.model_validate_json(content)
        except ValidationError as error:
            raise TransactionError(invalid_code, "事务状态契约无效") from error

    @staticmethod
    def _atomic_write(path: Path, content: bytes, *, mode: int) -> None:
        """在目标目录内写临时文件、replace 并 fsync 目录。"""
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        file_descriptor, temporary_name = tempfile.mkstemp(
            prefix=".rivet-transaction-",
            suffix=".tmp",
            dir=path.parent,
        )
        temporary_path = Path(temporary_name)
        try:
            os.fchmod(file_descriptor, mode)
            with os.fdopen(file_descriptor, "wb") as stream:
                file_descriptor = -1
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary_path, path)
            path.chmod(mode)
            directory_descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory_descriptor)
            finally:
                os.close(directory_descriptor)
        except BaseException:
            if file_descriptor >= 0:
                os.close(file_descriptor)
            temporary_path.unlink(missing_ok=True)
            raise

    def _open_state_root(self) -> int:
        """打开同文件系统状态根，供发布锁和目录 fsync 共同使用。"""
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            return os.open(self.state_root, flags)
        except OSError as error:
            raise TransactionError(
                "transaction.state_root_unreadable",
                "事务状态根无法安全打开",
            ) from error

    @staticmethod
    def _fsync_directory(directory: Path) -> None:
        """在 rename 前确保 staging 目录项已经持久化。"""
        descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def _cleanup_stale_frozen_publications(self) -> None:
        """持锁删除崩溃遗留且从未成为公共事务的 staging 目录。"""
        root_descriptor = self._open_state_root()
        try:
            fcntl.flock(root_descriptor, fcntl.LOCK_EX)
            for candidate in self.state_root.iterdir():
                if not FROZEN_PUBLISH_STAGING_PATTERN.fullmatch(candidate.name):
                    continue
                if candidate.is_symlink() or not candidate.is_dir():
                    raise TransactionError(
                        "transaction.frozen_staging_invalid",
                        "冻结事务 staging 路径类型无效",
                    )
                shutil.rmtree(candidate)
            os.fsync(root_descriptor)
        except TransactionError:
            raise
        except OSError as error:
            raise TransactionError(
                "transaction.frozen_staging_cleanup_failed",
                "无法清理未发布的冻结事务 staging",
            ) from error
        finally:
            fcntl.flock(root_descriptor, fcntl.LOCK_UN)
            os.close(root_descriptor)
