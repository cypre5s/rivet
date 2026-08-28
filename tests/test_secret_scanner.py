"""验证凭据扫描只报告脱敏命中。"""

import subprocess
from pathlib import Path

import pytest

from scripts.verify_secrets import main, scan_bytes


def _run_git(repository_root: Path, *arguments: str) -> None:
    """在临时仓库中执行不经 shell 的 Git 测试命令。"""
    subprocess.run(
        ["git", *arguments],
        cwd=repository_root,
        check=True,
        capture_output=True,
    )


def test_generated_provider_key_is_detected_without_echo() -> None:
    secret_value = "sk-" + ("a" * 32)

    findings = scan_bytes(secret_value.encode(), "fixture.txt")

    assert len(findings) == 1
    assert findings[0].rule_id == "provider_key"
    assert secret_value not in repr(findings)


def test_empty_environment_placeholder_is_allowed() -> None:
    findings = scan_bytes(b"DEEPSEEK_API_KEY=\n", ".env.example")

    assert findings == ()


def test_worktree_scan_detects_secret_without_echo(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _run_git(tmp_path, "init", "-b", "main")
    secret_value = "sk-" + ("b" * 32)
    (tmp_path / "unsafe.txt").write_text(secret_value, encoding="utf-8")

    exit_code = main(["--repository", str(tmp_path), "--worktree"])
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "provider_key" in captured.err
    assert secret_value not in captured.err


def test_staged_scan_accepts_placeholder(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _run_git(tmp_path, "init", "-b", "main")
    (tmp_path / ".env.example").write_text("DEEPSEEK_API_KEY=\n", encoding="utf-8")
    _run_git(tmp_path, "add", ".env.example")

    exit_code = main(["--repository", str(tmp_path), "--staged"])
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "未发现已知凭据模式" in captured.out


def test_default_scope_scans_worktree_and_staged(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _run_git(tmp_path, "init", "-b", "main")
    (tmp_path / "safe.txt").write_text("安全内容\n", encoding="utf-8")

    exit_code = main(["--repository", str(tmp_path)])
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "未发现已知凭据模式" in captured.out
