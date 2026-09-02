"""原子写入不可覆盖 EvidenceBundle，并复核完整文件清单。"""

from __future__ import annotations

import os
import re
import shutil
import tempfile
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from pydantic import ValidationError

from rivet.contracts.transactions import AcceptanceSpec
from rivet.contracts.verification import EvidenceFile, EvidenceManifest
from rivet.trace.redaction import SecretRedactor
from rivet.transaction.hashing import acceptance_sha256 as compute_acceptance_sha256
from rivet.transaction.hashing import canonical_json_bytes, sha256_digest

from .errors import VerificationError
from .security import contains_secret

MANDATORY_EVIDENCE_FILES = frozenset(
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
ATTEMPT_PATTERN = re.compile(r"^attempt_[0-9]{4}$")
MAX_EVIDENCE_FILE_BYTES = 64 * 1024 * 1024
MAX_EVIDENCE_TOTAL_BYTES = 128 * 1024 * 1024
Clock = Callable[[], datetime]


@dataclass(frozen=True, slots=True)
class EvidenceBundle:
    """返回已原子发布的尝试目录及清单事实。"""

    directory: Path
    manifest: EvidenceManifest
    manifest_sha256: str


class EvidenceBundleWriter:
    """在私有 evidence 根中以 attempt 目录保存不可变证据。"""

    def __init__(
        self,
        evidence_root: Path,
        *,
        clock: Clock | None = None,
    ) -> None:
        self.evidence_root = evidence_root.resolve(strict=False)
        self._clock = clock or (lambda: datetime.now(UTC))
        self._redactor = SecretRedactor(environment={})

    def write(
        self,
        *,
        transaction_id: str,
        base_commit: str,
        acceptance_sha256: str,
        patch_sha256: str,
        files: Mapping[str, bytes],
        evidence_id: str | None = None,
        attempt_name: str | None = None,
    ) -> EvidenceBundle:
        """完整写临时目录后一次 rename，绝不覆盖旧 attempt。"""
        missing = MANDATORY_EVIDENCE_FILES - set(files)
        if missing:
            raise VerificationError(
                "verification.evidence_incomplete",
                "EvidenceBundle 缺少必需文件",
            )
        if "manifest.json" in files:
            raise VerificationError(
                "verification.evidence_manifest_reserved",
                "manifest.json 只能由证据写入器生成",
            )
        try:
            specification = AcceptanceSpec.model_validate_json(
                files["acceptance_spec.json"]
            )
        except ValidationError as error:
            raise VerificationError(
                "verification.evidence_acceptance_invalid",
                "Evidence 中的 AcceptanceSpec 无法校验",
            ) from error
        if compute_acceptance_sha256(specification) != acceptance_sha256:
            raise VerificationError(
                "verification.evidence_acceptance_mismatch",
                "Evidence 未绑定冻结 AcceptanceSpec",
            )
        if sha256_digest(files["patch.diff"]) != patch_sha256:
            raise VerificationError(
                "verification.evidence_patch_mismatch",
                "Evidence 未绑定冻结 PatchSet",
            )
        self._prepare_root()
        selected_attempt = attempt_name or self._next_attempt_name()
        if not ATTEMPT_PATTERN.fullmatch(selected_attempt):
            raise VerificationError(
                "verification.evidence_attempt_invalid",
                "Evidence attempt 名称无效",
            )
        final_directory = self.evidence_root / selected_attempt
        if final_directory.exists():
            raise VerificationError(
                "verification.evidence_attempt_conflict",
                "Evidence attempt 目录已存在",
            )
        temporary_directory = Path(
            tempfile.mkdtemp(prefix=f".{selected_attempt}.", dir=self.evidence_root)
        )
        temporary_directory.chmod(0o700)
        try:
            evidence_files: list[EvidenceFile] = []
            total_bytes = 0
            patch_redacted = False
            for relative_path in sorted(files):
                self._validate_flat_path(relative_path)
                content = self._sanitize(files[relative_path])
                if relative_path == "patch.diff":
                    patch_redacted = content != files[relative_path]
                total_bytes += len(content)
                if (
                    len(content) > MAX_EVIDENCE_FILE_BYTES
                    or total_bytes > MAX_EVIDENCE_TOTAL_BYTES
                ):
                    raise VerificationError(
                        "verification.evidence_size_exceeded",
                        "EvidenceBundle 超过大小上限",
                    )
                self._write_file(temporary_directory / relative_path, content)
                evidence_files.append(
                    EvidenceFile(
                        path=relative_path,
                        sha256=sha256_digest(content),
                        size_bytes=len(content),
                    )
                )
            created_at = self._now()
            manifest = EvidenceManifest(
                evidence_id=evidence_id or f"evidence_{uuid.uuid4().hex}",
                transaction_id=transaction_id,
                base_commit=base_commit,
                acceptance_sha256=acceptance_sha256,
                patch_sha256=patch_sha256,
                patch_redacted=patch_redacted,
                files=tuple(evidence_files),
                created_at=created_at,
            )
            manifest_content = (
                canonical_json_bytes(manifest.model_dump(mode="json")) + b"\n"
            )
            self._write_file(
                temporary_directory / "manifest.json",
                manifest_content,
            )
            self._fsync_directory(temporary_directory)
            try:
                os.rename(temporary_directory, final_directory)
            except FileExistsError as error:
                raise VerificationError(
                    "verification.evidence_attempt_conflict",
                    "Evidence attempt 目录发生并发冲突",
                ) from error
            except OSError as error:
                raise VerificationError(
                    "verification.evidence_publish_failed",
                    "Evidence attempt 无法原子发布",
                ) from error
            self._fsync_directory(self.evidence_root)
        except BaseException:
            if temporary_directory.exists():
                shutil.rmtree(temporary_directory)
            raise
        return EvidenceBundle(
            directory=final_directory,
            manifest=manifest,
            manifest_sha256=sha256_digest(manifest_content),
        )

    def next_attempt_name(self) -> str:
        """准备私有根并返回下一次验证的稳定目录名。"""
        self._prepare_root()
        return self._next_attempt_name()

    def verify(self, directory: Path) -> EvidenceManifest:
        """验证清单契约、文件集合、大小和每个 SHA-256。"""
        if directory.is_symlink():
            raise VerificationError(
                "verification.evidence_path_symlink",
                "Evidence attempt 不得是符号链接",
            )
        resolved = directory.resolve(strict=True)
        try:
            parent = resolved.parent.resolve(strict=True)
        except OSError as error:
            raise VerificationError(
                "verification.evidence_path_invalid",
                "Evidence 根不可解析",
            ) from error
        if parent != self.evidence_root or not ATTEMPT_PATTERN.fullmatch(resolved.name):
            raise VerificationError(
                "verification.evidence_path_invalid",
                "Evidence attempt 不属于配置根",
            )
        manifest_path = resolved / "manifest.json"
        try:
            if manifest_path.is_symlink() or manifest_path.stat().st_size > 1024 * 1024:
                raise VerificationError(
                    "verification.evidence_manifest_invalid",
                    "Evidence manifest 类型或大小无效",
                )
            manifest = EvidenceManifest.model_validate_json(manifest_path.read_bytes())
        except VerificationError:
            raise
        except (OSError, ValidationError) as error:
            raise VerificationError(
                "verification.evidence_manifest_invalid",
                "Evidence manifest 无法读取或校验",
            ) from error
        entries = tuple(resolved.iterdir())
        if any(path.is_symlink() or not path.is_file() for path in entries):
            raise VerificationError(
                "verification.evidence_file_set_mismatch",
                "Evidence 文件清单包含非普通文件",
            )
        actual_paths = {path.name for path in entries}
        expected_paths = {evidence_file.path for evidence_file in manifest.files}
        if actual_paths != expected_paths | {"manifest.json"}:
            raise VerificationError(
                "verification.evidence_file_set_mismatch",
                "Evidence 文件清单与目录不一致",
            )
        for evidence_file in manifest.files:
            path = resolved / evidence_file.path
            try:
                content = path.read_bytes()
            except OSError as error:
                raise VerificationError(
                    "verification.evidence_file_unreadable",
                    "Evidence 文件不可读",
                ) from error
            if (
                len(content) != evidence_file.size_bytes
                or sha256_digest(content) != evidence_file.sha256
            ):
                raise VerificationError(
                    "verification.evidence_hash_mismatch",
                    "Evidence 文件大小或哈希不匹配",
                )
        return manifest

    def _prepare_root(self) -> None:
        """显式建立私有 evidence 根并拒绝符号链接。"""
        if self.evidence_root.is_symlink():
            raise VerificationError(
                "verification.evidence_root_symlink",
                "Evidence 根不得是符号链接",
            )
        self.evidence_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.evidence_root.chmod(0o700)

    def _next_attempt_name(self) -> str:
        """选择首个未使用的四位 attempt 名称。"""
        for index in range(1, 10_000):
            name = f"attempt_{index:04d}"
            if not (self.evidence_root / name).exists():
                return name
        raise VerificationError(
            "verification.evidence_attempt_exhausted",
            "Evidence attempt 编号已耗尽",
        )

    @staticmethod
    def _validate_flat_path(relative_path: str) -> None:
        """当前 Evidence 契约只允许稳定的顶层文件名。"""
        path = Path(relative_path)
        if (
            not relative_path
            or path.is_absolute()
            or len(path.parts) != 1
            or path.name in {".", ".."}
            or "\x00" in relative_path
        ):
            raise VerificationError(
                "verification.evidence_path_invalid",
                "Evidence 文件名无效",
            )

    def _sanitize(self, content: bytes) -> bytes:
        """只有命中秘密时才把文本证据转换为脱敏 UTF-8。"""
        if not contains_secret(content):
            return content
        text = content.decode("utf-8", errors="replace")
        return self._redactor.redact_text(text).encode("utf-8")

    @staticmethod
    def _write_file(path: Path, content: bytes) -> None:
        """以 O_EXCL 写入、fsync 并固定私有权限。"""
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                descriptor = -1
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
        finally:
            if descriptor >= 0:
                os.close(descriptor)

    @staticmethod
    def _fsync_directory(path: Path) -> None:
        """同步目录项，确保 rename 与文件清单持久。"""
        descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def _now(self) -> datetime:
        """读取可注入且必须带时区的证据时间。"""
        value = self._clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise VerificationError(
                "verification.clock_naive",
                "证据时钟必须带时区",
            )
        return value
