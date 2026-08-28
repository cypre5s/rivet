"""验证 TUI 启动检测、环境白名单和 headless 降级。"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

import rivet
from rivet.tui_launcher import TuiLaunchError, build_tui_environment, launch_tui


def test_tui_environment_excludes_unlisted_secret(tmp_path: Path) -> None:
    environment = build_tui_environment(
        tmp_path,
        source={
            "PATH": "/usr/bin",
            "DEEPSEEK_API_KEY": "allowed-provider-value",
            "UNRELATED_SECRET": "must-not-pass",
        },
    )

    assert environment["PATH"] == "/usr/bin"
    assert "DEEPSEEK_API_KEY" in environment
    assert "UNRELATED_SECRET" not in environment
    assert "allowed-provider-value" not in environment["RIVET_WORKER_COMMAND_JSON"]


def test_launch_tui_checks_bun_version_and_uses_argv(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tui_directory = tmp_path / "tui"
    (tui_directory / "src").mkdir(parents=True)
    (tui_directory / "src" / "main.tsx").write_text("", encoding="utf-8")
    (tui_directory / "bun.lock").write_text("", encoding="utf-8")
    calls: list[tuple[str, ...]] = []

    def fake_run(
        arguments: tuple[str, ...], **options: object
    ) -> subprocess.CompletedProcess[str]:
        """记录 argv，并模拟版本探针和前台 TUI。"""
        calls.append(arguments)
        if arguments[-1] == "--version":
            return subprocess.CompletedProcess(arguments, 0, "1.4.0\n", "")
        assert options.get("cwd") == tui_directory
        return subprocess.CompletedProcess(arguments, 0)

    def find_bun(_command: str, *, path: str | None = None) -> str:
        """返回固定测试运行时路径。"""
        return "/bin/bun"

    monkeypatch.setattr("rivet.tui_launcher.shutil.which", find_bun)
    monkeypatch.setattr("rivet.tui_launcher.subprocess.run", fake_run)

    exit_code = launch_tui(
        tmp_path,
        environment={"PATH": "/bin"},
        tui_directory=tui_directory,
    )

    assert exit_code == 0
    assert calls == [
        ("/bin/bun", "--version"),
        ("/bin/bun", "run", "src/main.tsx"),
    ]


def test_launch_tui_rejects_missing_or_wrong_bun(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def miss_bun(_command: str, *, path: str | None = None) -> None:
        """模拟 PATH 中没有 Bun。"""

    monkeypatch.setattr("rivet.tui_launcher.shutil.which", miss_bun)
    with pytest.raises(TuiLaunchError, match="Bun 1.4"):
        launch_tui(tmp_path, environment={"PATH": "/bin"}, tui_directory=tmp_path)


def test_headless_without_subcommand_does_not_launch_tui(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def unexpected_launch(_repository: Path) -> int:
        """在测试中拒绝任何 TUI 启动。"""
        raise AssertionError("headless 不应启动 TUI")

    monkeypatch.setattr("rivet.tui_launcher.launch_tui", unexpected_launch)

    rivet.main(["--headless"])

    assert "不启动 TUI" in capsys.readouterr().out
