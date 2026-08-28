"""持久化脱敏且受大小限制的 stdout/stderr 完整日志。"""

from __future__ import annotations

import hashlib
import os
from contextlib import suppress
from pathlib import Path

from rivet.contracts.common import ArtifactReference, EventId, RunId
from rivet.trace.errors import TraceWriteError
from rivet.trace.models import CapturedStream, OutputCapture
from rivet.trace.paths import RuntimePaths
from rivet.trace.redaction import SecretRedactor

TRUNCATION_MARKER = "\n[TRUNCATED]\n"


class TraceArtifactStore:
    """把事件预览与完整输出分开，且不保存原始秘密。"""

    def __init__(
        self,
        paths: RuntimePaths,
        redactor: SecretRedactor,
        *,
        max_preview_chars: int = 4_096,
        max_artifact_bytes: int = 5 * 1024 * 1024,
    ) -> None:
        if max_preview_chars <= 0 or max_artifact_bytes <= 0:
            raise ValueError("输出预览与 artifact 上限必须大于 0")
        self._paths = paths
        self._redactor = redactor
        self._max_preview_chars = max_preview_chars
        self._max_artifact_bytes = max_artifact_bytes

    def capture(
        self,
        *,
        run_id: RunId,
        event_id: EventId,
        stdout: str,
        stderr: str,
    ) -> OutputCapture:
        """同步写入同一事件的两个脱敏日志 artifact。"""
        return OutputCapture(
            stdout=self._capture_stream(run_id, event_id, "stdout", stdout),
            stderr=self._capture_stream(run_id, event_id, "stderr", stderr),
        )

    def _capture_stream(
        self,
        run_id: str,
        event_id: str,
        stream_name: str,
        content: str,
    ) -> CapturedStream:
        """限制单个流的预览字符与完整日志字节。"""
        redacted_content = self._redactor.redact_text(content)
        preview_truncated = len(redacted_content) > self._max_preview_chars
        preview = redacted_content[: self._max_preview_chars]
        encoded = redacted_content.encode("utf-8")
        artifact_truncated = len(encoded) > self._max_artifact_bytes
        if artifact_truncated:
            marker = TRUNCATION_MARKER.encode("utf-8")
            prefix_size = max(0, self._max_artifact_bytes - len(marker))
            encoded = (
                encoded[:prefix_size].decode("utf-8", errors="ignore").encode("utf-8")
                + marker
            )

        relative_path = Path("artifacts") / run_id / f"{event_id}.{stream_name}.log"
        target_path = self._paths.runtime_root / relative_path
        temporary_path = target_path.with_suffix(f"{target_path.suffix}.tmp")
        try:
            target_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            target_path.parent.chmod(0o700)
            temporary_path.write_bytes(encoded)
            temporary_path.chmod(0o600)
            os.replace(temporary_path, target_path)
        except OSError as error:
            with suppress(OSError):
                temporary_path.unlink()
            raise TraceWriteError("Trace artifact 无法原子写入") from error
        digest = hashlib.sha256(encoded).hexdigest()
        return CapturedStream(
            preview=preview,
            preview_truncated=preview_truncated,
            artifact_truncated=artifact_truncated,
            artifact=ArtifactReference(
                path=relative_path.as_posix(),
                sha256=f"sha256:{digest}",
                size_bytes=len(encoded),
                media_type="text/plain",
            ),
        )
