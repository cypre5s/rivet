"""将 Evidence、Trace 和 Session 导出为真实、带哈希的 JSON 文件。"""

from __future__ import annotations

import base64
import os
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, cast

from pydantic import JsonValue

from rivet.storage.sessions import SessionStore
from rivet.trace.paths import RuntimePaths
from rivet.trace.redaction import SecretRedactor
from rivet.trace.replay import TraceReplayer, scan_trace_file
from rivet.transaction.hashing import canonical_json_bytes, sha256_digest
from rivet.verify.evidence import EvidenceBundleWriter

ExportKind = Literal["evidence", "trace", "session"]


class ExportError(ValueError):
    """携带稳定错误码的导出失败。"""

    def __init__(self, code: str, summary: str) -> None:
        super().__init__(summary)
        self.code = code
        self.summary = summary


@dataclass(frozen=True, slots=True)
class ExportResult:
    """描述真实导出文件、来源和完整性哈希。"""

    kind: ExportKind
    path: Path
    sha256: str
    source_id: str


class ExportService:
    """只向仓库内受控目录原子导出，不覆盖已有文件。"""

    def __init__(
        self,
        repository: Path,
        *,
        environment: Mapping[str, str],
    ) -> None:
        self._repository = repository.resolve(strict=True)
        self._redactor = SecretRedactor(environment)

    def export(self, kind: str, destination: Path | None = None) -> ExportResult:
        """导出指定种类的最新有效事实并返回真实路径与 SHA-256。"""
        if kind not in {"evidence", "trace", "session"}:
            raise ExportError("export.kind_invalid", "导出种类无效")
        selected_kind = cast(ExportKind, kind)
        payload, source_id = self._payload(selected_kind)
        redacted = self._redactor.redact_payload(payload)
        content = canonical_json_bytes(redacted) + b"\n"
        target = self._destination(selected_kind, source_id, destination)
        self._atomic_write(target, content)
        return ExportResult(
            kind=selected_kind,
            path=target,
            sha256=sha256_digest(content),
            source_id=source_id,
        )

    def _payload(self, kind: ExportKind) -> tuple[dict[str, JsonValue], str]:
        if kind == "session":
            store = SessionStore(self._repository)
            identifiers = store.list_recent_ids(limit=1)
            if not identifiers:
                raise ExportError("export.source_missing", "没有可导出的 Session")
            source_id = identifiers[0]
            checkpoint = store.load(source_id)
            return (
                {
                    "schema_version": 1,
                    "kind": kind,
                    "source_id": source_id,
                    "session": checkpoint.model_dump(mode="json"),
                },
                source_id,
            )
        if kind == "trace":
            events_path = RuntimePaths.for_repository(self._repository).events_path
            scan = scan_trace_file(events_path)
            run_ids = sorted(
                {located.record.event.run_id for located in scan.located_events}
            )
            if not run_ids:
                raise ExportError("export.source_missing", "没有可导出的 Trace")
            source_id = run_ids[-1]
            replay = TraceReplayer(events_path).replay(source_id)
            return (
                {
                    "schema_version": 1,
                    "kind": kind,
                    "source_id": source_id,
                    "trace": replay.model_dump(mode="json"),
                },
                source_id,
            )
        return self._evidence_payload()

    def _evidence_payload(self) -> tuple[dict[str, JsonValue], str]:
        manifests = tuple(
            path
            for path in (self._repository / ".rivet" / "transactions").glob(
                "*/evidence/attempt_*/manifest.json"
            )
            if path.is_file() and not path.is_symlink()
        )
        if not manifests:
            raise ExportError("export.source_missing", "没有可导出的 Evidence")
        manifest_path = max(manifests, key=lambda path: (path.stat().st_mtime_ns, path))
        attempt = manifest_path.parent
        manifest = EvidenceBundleWriter(attempt.parent).verify(attempt)
        files: dict[str, JsonValue] = {}
        for evidence_file in manifest.files:
            content = (attempt / evidence_file.path).read_bytes()
            try:
                files[evidence_file.path] = self._redactor.redact_text(
                    content.decode("utf-8", errors="strict")
                )
            except UnicodeDecodeError:
                files[evidence_file.path] = {
                    "base64": base64.b64encode(content).decode("ascii"),
                    "sha256": evidence_file.sha256,
                }
        source_id = manifest.evidence_id
        return (
            {
                "schema_version": 1,
                "kind": "evidence",
                "source_id": source_id,
                "manifest": manifest.model_dump(mode="json"),
                "files": files,
            },
            source_id,
        )

    def _destination(
        self,
        kind: ExportKind,
        source_id: str,
        requested: Path | None,
    ) -> Path:
        relative = requested or Path(".rivet") / "exports" / f"{kind}-{source_id}.json"
        if relative.is_absolute() or ".." in relative.parts or not relative.parts:
            raise ExportError(
                "export.destination_invalid", "导出路径必须位于受控运行目录"
            )
        if relative.parts[:2] != (".rivet", "exports"):
            raise ExportError(
                "export.destination_invalid",
                "导出路径必须位于 .rivet/exports",
            )
        candidate = self._repository / relative
        cursor = self._repository
        for part in relative.parts[:-1]:
            cursor /= part
            if cursor.is_symlink():
                raise ExportError("export.destination_invalid", "导出路径包含符号链接")
        resolved = candidate.resolve(strict=False)
        try:
            resolved.relative_to(self._repository)
        except ValueError as error:
            raise ExportError(
                "export.destination_invalid", "导出路径越过仓库边界"
            ) from error
        if candidate.exists() or candidate.is_symlink():
            raise ExportError("export.destination_exists", "导出目标已存在")
        return candidate

    @staticmethod
    def _atomic_write(target: Path, content: bytes) -> None:
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{target.name}.",
            suffix=".tmp",
            dir=target.parent,
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                descriptor = -1
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            temporary.chmod(0o600)
            os.replace(temporary, target)
            directory = os.open(target.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            temporary.unlink(missing_ok=True)
