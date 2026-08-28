"""验证 stdout/stderr 预览与完整脱敏 artifact 分离。"""

from __future__ import annotations

import errno
from pathlib import Path
from unittest.mock import patch

import pytest

from rivet.trace.artifacts import TraceArtifactStore
from rivet.trace.errors import TraceWriteError
from rivet.trace.paths import RuntimePaths
from rivet.trace.redaction import REDACTED_TEXT, SecretRedactor


def test_capture_separates_preview_and_redacted_full_log(tmp_path: Path) -> None:
    token = "sk-" + ("d" * 32)
    paths = RuntimePaths.for_repository(
        tmp_path,
        environment={"XDG_CACHE_HOME": str(tmp_path / "cache")},
    )
    paths.prepare()
    artifacts = TraceArtifactStore(
        paths,
        SecretRedactor(environment={"DEEPSEEK_API_KEY": token}),
        max_preview_chars=16,
        max_artifact_bytes=1_024,
    )

    capture = artifacts.capture(
        run_id="run_trace_test",
        event_id="event_trace_1",
        stdout=("prefix " + token + " suffix") * 10,
        stderr="error " + token,
    )

    stdout_path = paths.runtime_root / capture.stdout.artifact.path
    stderr_path = paths.runtime_root / capture.stderr.artifact.path
    persisted = stdout_path.read_text(encoding="utf-8")
    assert token not in persisted
    assert REDACTED_TEXT in persisted
    assert len(capture.stdout.preview) <= 16
    assert capture.stdout.preview_truncated
    assert capture.stdout.artifact.sha256.startswith("sha256:")
    assert stderr_path.is_file()


def test_disk_full_is_classified_and_removes_temporary_file(tmp_path: Path) -> None:
    paths = RuntimePaths.for_repository(
        tmp_path,
        environment={"XDG_CACHE_HOME": str(tmp_path / "cache")},
    )
    artifacts = TraceArtifactStore(paths, SecretRedactor(environment={}))

    with (
        patch.object(
            Path,
            "write_bytes",
            side_effect=OSError(errno.ENOSPC, "fixture disk full"),
        ),
        pytest.raises(TraceWriteError, match="artifact"),
    ):
        artifacts.capture(
            run_id="run_artifact_full",
            event_id="event_artifact_full",
            stdout="content",
            stderr="",
        )

    assert not tuple(paths.runtime_root.rglob("*.tmp"))


def test_disk_full_while_creating_directory_is_classified(tmp_path: Path) -> None:
    paths = RuntimePaths.for_repository(
        tmp_path,
        environment={"XDG_CACHE_HOME": str(tmp_path / "cache")},
    )
    artifacts = TraceArtifactStore(paths, SecretRedactor(environment={}))

    with (
        patch.object(
            Path,
            "mkdir",
            side_effect=OSError(errno.ENOSPC, "fixture disk full"),
        ),
        pytest.raises(TraceWriteError, match="artifact"),
    ):
        artifacts.capture(
            run_id="run_artifact_directory_full",
            event_id="event_artifact_directory_full",
            stdout="content",
            stderr="",
        )
