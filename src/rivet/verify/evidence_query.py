"""对已发布 Evidence 做完整性复核，并提供有界、只读的 UI 查询。"""

from __future__ import annotations

import unicodedata
from typing import cast

from pydantic import JsonValue

from rivet.contracts.transactions import TransactionState
from rivet.contracts.verification import (
    EvidenceManifest,
    VerificationResult,
    VerificationStatus,
)
from rivet.transaction.errors import TransactionError
from rivet.transaction.store import TransactionStore

MAX_EVIDENCE_LOG_VIEW_BYTES = 512 * 1024


class EvidenceQueryService:
    """只从哈希复核通过的事务事实构建公共 Evidence 视图。"""

    def __init__(self, store: TransactionStore) -> None:
        self._store = store

    def detail(self, transaction_id: str) -> dict[str, JsonValue]:
        """返回补丁、Verdict、矩阵和 manifest 文件索引。"""
        record = self._store.load_record(transaction_id)
        patch = None
        if record.current_patch_id is not None:
            patch, _ = self._store.load_patch(
                transaction_id,
                record.current_patch_id,
            )
        payload: dict[str, JsonValue] = {
            "acceptance_sha256": record.acceptance_sha256,
            "apply_eligible": record.state is TransactionState.VERIFIED,
            "base_commit": record.base_commit,
            "changed_files": list(patch.changed_files) if patch is not None else [],
            "evidence_id": record.evidence_id,
            "evidence_verified": False,
            "manifest_sha256": record.evidence_manifest_sha256,
            "next_action": self._next_action(record.state, transaction_id),
            "patch_id": record.current_patch_id,
            "patch_sha256": patch.patch_sha256 if patch is not None else None,
            "state": record.state.value,
            "status": record.state.value,
            "transaction_id": record.transaction_id,
            "updated_at": record.updated_at.isoformat(),
            "verification_results": [],
            "files": [],
        }
        if record.evidence_id is None:
            return payload
        if patch is None:
            raise TransactionError(
                "transaction.patch_missing",
                "已发布 Evidence 的事务缺少当前补丁",
            )
        verdict = self._store.verify_record_evidence(
            record,
            expected_patch_sha256=patch.patch_sha256,
        )
        manifest = self._manifest(
            record.transaction_id, cast(str, record.evidence_manifest_path)
        )
        payload.update(
            {
                "acceptance_sha256": verdict.acceptance_sha256,
                "base_commit": verdict.base_commit,
                "decided_at": verdict.decided_at.isoformat(),
                "evidence_verified": True,
                "files": [
                    {
                        "path": item.path,
                        "sha256": item.sha256,
                        "size_bytes": item.size_bytes,
                    }
                    for item in manifest.files
                ],
                "passed": verdict.passed,
                "status": verdict.status.value,
                "verdict_status": verdict.status.value,
                "verification_results": [
                    self._result_payload(result) for result in verdict.results
                ],
            }
        )
        return payload

    def log(
        self,
        transaction_id: str,
        *,
        step_id: str | None = None,
    ) -> dict[str, JsonValue]:
        """惰性读取一个已索引步骤日志，并再次绑定 manifest 与结果哈希。"""
        record = self._store.load_record(transaction_id)
        if record.current_patch_id is None or record.evidence_manifest_path is None:
            raise TransactionError(
                "transaction.evidence_attestation_missing",
                "当前事务没有可读取的 Evidence 日志",
            )
        patch, _ = self._store.load_patch(transaction_id, record.current_patch_id)
        verdict = self._store.verify_record_evidence(
            record,
            expected_patch_sha256=patch.patch_sha256,
        )
        manifest = self._manifest(transaction_id, record.evidence_manifest_path)
        candidates = tuple(
            result for result in verdict.results if result.log_path is not None
        )
        if step_id is None:
            result = next(
                (
                    item
                    for item in candidates
                    if item.status is not VerificationStatus.PASSED
                ),
                candidates[0] if candidates else None,
            )
        else:
            if len(step_id) > 256 or not step_id:
                result = None
            else:
                result = next(
                    (item for item in candidates if item.step.step_id == step_id),
                    None,
                )
        if result is None or result.log_path is None or result.log_sha256 is None:
            raise TransactionError(
                "transaction.evidence_log_missing",
                "所选验证步骤没有可读取日志",
            )
        indexed = next(
            (item for item in manifest.files if item.path == result.log_path),
            None,
        )
        if indexed is None or indexed.sha256 != result.log_sha256:
            raise TransactionError(
                "transaction.evidence_log_hash_mismatch",
                "验证日志未与 Evidence manifest 正确绑定",
            )
        attempt = self._store.evidence_manifest_path(
            transaction_id,
            record.evidence_manifest_path,
        ).parent
        path = attempt / result.log_path
        if path.is_symlink() or not path.is_file():
            raise TransactionError(
                "transaction.evidence_log_invalid",
                "验证日志路径不是受控普通文件",
            )
        try:
            with path.open("rb") as stream:
                content = stream.read(MAX_EVIDENCE_LOG_VIEW_BYTES + 1)
        except OSError as error:
            raise TransactionError(
                "transaction.evidence_log_unreadable",
                "验证日志无法读取",
            ) from error
        truncated = len(content) > MAX_EVIDENCE_LOG_VIEW_BYTES
        if truncated:
            content = content[:MAX_EVIDENCE_LOG_VIEW_BYTES]
        return {
            "content": _safe_terminal_text(content.decode("utf-8", errors="replace")),
            "evidence_id": verdict.evidence_id,
            "log_path": result.log_path,
            "log_sha256": result.log_sha256,
            "status": result.status.value,
            "step_id": result.step.step_id,
            "transaction_id": transaction_id,
            "truncated": truncated,
        }

    def _manifest(
        self,
        transaction_id: str,
        manifest_relative_path: str,
    ) -> EvidenceManifest:
        """在 Store 全量复核后读取同一受限 manifest 契约。"""
        path = self._store.evidence_manifest_path(
            transaction_id,
            manifest_relative_path,
        )
        return EvidenceManifest.model_validate_json(path.read_bytes())

    @staticmethod
    def _result_payload(result: VerificationResult) -> dict[str, JsonValue]:
        """投影单个验证事实，不回显未脱敏环境。"""
        return {
            "argv": list(result.step.command),
            "duration_ms": result.duration_ms,
            "exit_code": result.exit_code,
            "kind": result.step.kind.value,
            "log_path": result.log_path,
            "log_sha256": result.log_sha256,
            "name": result.step.name,
            "output_truncated": result.output_truncated,
            "required": result.step.required,
            "status": result.status.value,
            "stderr_summary": _safe_terminal_text(result.stderr_summary),
            "stdout_summary": _safe_terminal_text(result.stdout_summary),
            "step_id": result.step.step_id,
        }

    @staticmethod
    def _next_action(state: TransactionState, transaction_id: str) -> str:
        """根据后端权威状态给出不会越过 Apply 门禁的动作。"""
        if state is TransactionState.VERIFIED:
            return f"审查证据后可显式运行 rivet apply {transaction_id}"
        if state is TransactionState.PATCHING:
            return f"配置独立验收后运行 rivet verify {transaction_id}"
        if state is TransactionState.APPLIED:
            return "补丁已经显式应用到主工作区"
        return f"审查 Evidence，并按需运行 rivet diff {transaction_id}"


def _safe_terminal_text(value: str) -> str:
    """移除会改变终端状态的控制字符，保留换行与制表。"""
    return "".join(
        character
        for character in value
        if character in {"\n", "\t"}
        or unicodedata.category(character) not in {"Cc", "Cf"}
    )
