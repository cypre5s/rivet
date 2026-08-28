"""持久化事务记录、冻结验收与完整 binary patch 历史。"""

from __future__ import annotations

import os
import re
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
REQUIRED_ATTESTATION_FILES = frozenset(
    {
        "acceptance_spec.json",
        "acceptance_spec.sha256",
        "patch.diff",
        "scope_check.json",
        "secret_scan.json",
        "resource_check.json",
        "verdict.json",
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

    def __init__(self, state_root: Path) -> None:
        self.state_root = state_root.resolve(strict=False)

    def prepare(self) -> None:
        """显式创建私有状态根并拒绝符号链接。"""
        if self.state_root.is_symlink():
            raise TransactionError(
                "transaction.state_root_symlink",
                "事务状态根不得是符号链接",
            )
        self.state_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.state_root.chmod(0o700)

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
        """原子覆盖当前事务记录并 fsync。"""
        directory = self.transaction_directory(record.transaction_id)
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        directory.chmod(0o700)
        self._atomic_write(
            self.record_path(record.transaction_id),
            canonical_json_bytes(record.model_dump(mode="json")) + b"\n",
            mode=0o600,
        )

    def load_record(self, transaction_id: str) -> TransactionRecord:
        """严格加载一个存在的事务记录。"""
        return self._load_model(
            self.record_path(transaction_id),
            TransactionRecord,
            max_bytes=MAX_RECORD_BYTES,
            missing_code="transaction.record_missing",
            invalid_code="transaction.record_invalid",
        )

    def write_acceptance(
        self,
        transaction_id: str,
        specification: AcceptanceSpec,
    ) -> str:
        """只写一次 AcceptanceSpec 与哈希，并将文件设为只读。"""
        content = canonical_json_bytes(specification.model_dump(mode="json")) + b"\n"
        digest = acceptance_sha256(specification)
        specification_path = self.acceptance_path(transaction_id)
        hash_path = self.acceptance_hash_path(transaction_id)
        if specification_path.exists() or hash_path.exists():
            existing = self.load_acceptance(transaction_id, expected_sha256=digest)
            if existing != specification:
                raise TransactionError(
                    "transaction.acceptance_frozen",
                    "AcceptanceSpec 已冻结且不可修改",
                )
            return digest
        specification_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._atomic_write(specification_path, content, mode=0o400)
        self._atomic_write(hash_path, f"{digest}\n".encode("ascii"), mode=0o400)
        return digest

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
        expected_patch_sha256: str | None = None,
    ) -> str:
        """复核 Verdict 所指清单和全部文件，并返回 manifest 哈希。"""
        manifest_digest, persisted_verdict = self._verify_evidence_bundle(
            transaction_id=verdict.transaction_id,
            acceptance_sha256=verdict.acceptance_sha256,
            evidence_id=verdict.evidence_id,
            manifest_relative_path=verdict.evidence_manifest_path,
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
            record.acceptance_sha256 is None
            or record.evidence_id is None
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
        expected_patch_sha256: str | None,
    ) -> tuple[str, Verdict]:
        """验证受限 attempt 路径、manifest 契约和全量文件哈希。"""
        relative = Path(manifest_relative_path)
        if (
            relative.is_absolute()
            or len(relative.parts) != 3
            or relative.parts[0] != "evidence"
            or not EVIDENCE_ATTEMPT_PATTERN.fullmatch(relative.parts[1])
            or relative.parts[2] != "manifest.json"
        ):
            raise TransactionError(
                "transaction.evidence_path_invalid",
                "Evidence manifest 路径无效",
            )
        transaction_directory = self.transaction_directory(transaction_id)
        cursor = transaction_directory
        for part in relative.parts:
            cursor /= part
            if cursor.is_symlink():
                raise TransactionError(
                    "transaction.evidence_path_symlink",
                    "Evidence 路径不得包含符号链接",
                )
        manifest_path = transaction_directory / relative
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
            or manifest.acceptance_sha256 != acceptance_sha256
            or manifest.evidence_id != evidence_id
        ):
            raise TransactionError(
                "transaction.evidence_manifest_mismatch",
                "Evidence manifest 未绑定当前事务与验收条件",
            )
        attempt_directory = manifest_path.parent
        expected_paths = {evidence_file.path for evidence_file in manifest.files}
        if not expected_paths >= REQUIRED_ATTESTATION_FILES:
            raise TransactionError(
                "transaction.evidence_files_missing",
                "Evidence manifest 缺少交付门禁文件",
            )
        if expected_patch_sha256 is not None:
            patch_file = next(
                evidence_file
                for evidence_file in manifest.files
                if evidence_file.path == "patch.diff"
            )
            if patch_file.sha256 != expected_patch_sha256:
                raise TransactionError(
                    "transaction.evidence_patch_mismatch",
                    "通过结论中的 Evidence patch 与冻结 PatchSet 不一致",
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
