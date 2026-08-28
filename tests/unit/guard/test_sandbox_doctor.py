"""验证沙箱 Doctor 区分可用、缺失与不可执行状态。"""

from __future__ import annotations

from pathlib import Path

from rivet.guard.doctor import SandboxDoctor


def test_sandbox_doctor_reports_missing_binary(tmp_path: Path) -> None:
    report = SandboxDoctor(executable=tmp_path / "missing").inspect()

    assert not report.ready
    assert report.status == "MISSING"
    assert report.executable is None
    assert "安装 bubblewrap" in report.next_action


def test_sandbox_doctor_reports_executable_candidate(tmp_path: Path) -> None:
    executable = tmp_path / "bwrap"
    executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    executable.chmod(0o700)

    report = SandboxDoctor(executable=executable).inspect()

    assert report.ready
    assert report.status == "AVAILABLE"
    assert report.executable == str(executable)
    assert "AVAILABLE" in report.to_json()


def test_sandbox_doctor_reports_unusable_candidate(tmp_path: Path) -> None:
    executable = tmp_path / "bwrap"
    executable.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    executable.chmod(0o700)

    report = SandboxDoctor(executable=executable).inspect()

    assert not report.ready
    assert report.status == "UNUSABLE"
    assert "内核" in report.next_action
