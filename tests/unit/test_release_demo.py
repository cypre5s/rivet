"""验证固定发布演示的任务、仓库和录制响应均可复核。"""

from __future__ import annotations

import json
import subprocess
import tomllib
from pathlib import Path

import pytest

import scripts.run_release_demo as release_demo
from scripts.run_release_demo import (
    DEMO_TASK,
    RECORDED_TOOL_NAMES,
    materialize_demo_repository,
)


def test_demo_repository_has_clean_failing_baseline(tmp_path: Path) -> None:
    repository = materialize_demo_repository(tmp_path / "repository")

    baseline = subprocess.run(
        ("/usr/bin/python3", "test_calculator.py"),
        cwd=repository,
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    status = subprocess.run(
        ("git", "status", "--porcelain"),
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert baseline.returncode == 1
    assert "FAIL" in baseline.stdout
    assert status.stdout == ""
    assert "calculator.py" in DEMO_TASK


def test_demo_repository_uses_flash_without_a_cost_ceiling() -> None:
    configuration = tomllib.loads(
        (release_demo.FIXTURE_ROOT / ".rivet" / "project.toml").read_text(
            encoding="utf-8"
        )
    )

    assert configuration["rivet"]["model"] == "deepseek-v4-flash"
    assert configuration["rivet"]["max_total_tokens"] >= 128_000
    assert "max_cost_usd" not in configuration["rivet"]


def test_recorded_demo_turns_cover_context_patch_verify_and_diff() -> None:
    assert RECORDED_TOOL_NAMES == (
        "workspace.info",
        "search.files",
        "file.read_text",
        "file.read_text",
        "process.run",
        "file.replace_transaction",
        "process.run",
        "git.diff",
    )


def test_cli_failure_preserves_stable_error_code(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = {
        "error": {
            "code": "verification.fixture_failed",
            "summary": "固定失败摘要",
        }
    }
    completed = subprocess.CompletedProcess(
        args=("rivet",),
        returncode=4,
        stdout=json.dumps(payload),
        stderr="",
    )

    def failed_command(
        arguments: tuple[str, ...],
        *,
        cwd: Path,
        environment: dict[str, str],
        timeout: int = 60,
    ) -> subprocess.CompletedProcess[str]:
        del arguments, cwd, environment, timeout
        return completed

    monkeypatch.setattr(release_demo, "_run_command", failed_command)

    with pytest.raises(release_demo.DemoError) as raised:
        release_demo._run_cli(  # pyright: ignore[reportPrivateUsage]
            tmp_path, {}, "fix"
        )

    assert "verification.fixture_failed" in str(raised.value)


def test_main_reports_demo_error_without_traceback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def fail_demo(*, bwrap_path: Path) -> dict[str, object]:
        del bwrap_path
        raise release_demo.DemoError("稳定演示失败")

    monkeypatch.setattr(release_demo, "run_release_demo", fail_demo)

    exit_code = release_demo.main(
        (
            "--bwrap-path",
            "/usr/bin/bwrap",
            "--result",
            str(tmp_path / "result.json"),
        )
    )
    captured = capsys.readouterr()

    assert exit_code == 1
    assert captured.out == ""
    assert captured.err == "稳定演示失败\n"
    assert "Traceback" not in captured.err
