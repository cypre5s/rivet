"""验证独立验收命令的 argv 分类、路径冻结与失败边界。"""

from __future__ import annotations

import errno
from pathlib import Path

import pytest

import rivet.cli.model_commands as model_commands
from rivet.cli.errors import CliVerificationError
from rivet.contracts.transactions import Command


def _repository(tmp_path: Path) -> Path:
    """创建不依赖 Git 的最小路径分类仓库。"""
    repository = tmp_path / "repository"
    repository.mkdir()
    return repository


@pytest.mark.parametrize(
    "source",
    (
        "print('ok')",
        "value = 'x' * 8192; print(value)",
        "value = 1\nprint(value)\n",
        "消息 = '你好，Rivet'\nprint(消息)",
        "text = \"含 空格 和 '引号'\"; print(text)",
    ),
    ids=("short", "long", "multiline", "unicode", "spaces-and-quotes"),
)
def test_python_inline_source_is_never_probed_as_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    source: str,
) -> None:
    repository = _repository(tmp_path)
    original_exists = Path.exists
    probed_paths: list[Path] = []

    def record_exists(path: Path) -> bool:
        """记录路径探测并保留 pathlib 的正常结果。"""
        probed_paths.append(path)
        return original_exists(path)

    monkeypatch.setattr(Path, "exists", record_exists)

    protected = model_commands.resolve_behavior_verifier_paths(
        repository,
        (("python", "-c", source),),
    )

    assert protected == ()
    assert repository / source not in probed_paths


@pytest.mark.parametrize("module_name", ("pytest", "custom_package"))
def test_python_module_name_is_not_probed_as_literal_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    module_name: str,
) -> None:
    repository = _repository(tmp_path)
    package = repository / "custom_package"
    package.mkdir()
    (package / "__init__.py").write_text("", encoding="utf-8")
    original_exists = Path.exists
    probed_paths: list[Path] = []

    def record_exists(path: Path) -> bool:
        """区分模块名和映射后的 Python 文件路径。"""
        probed_paths.append(path)
        return original_exists(path)

    monkeypatch.setattr(Path, "exists", record_exists)

    protected = model_commands.resolve_behavior_verifier_paths(
        repository,
        (("python", "-m", module_name),),
    )

    assert repository / module_name not in probed_paths
    if module_name == "custom_package":
        assert protected == ("custom_package/__init__.py",)
    else:
        assert protected == ()


@pytest.mark.parametrize(
    "script_argument",
    ("script.py", "./script.py", "checks/script.py"),
)
def test_python_script_path_is_frozen(
    tmp_path: Path,
    script_argument: str,
) -> None:
    repository = _repository(tmp_path)
    script = repository / "checks" / "script.py"
    script.parent.mkdir()
    script.write_text("raise SystemExit(0)\n", encoding="utf-8")
    if script_argument != "checks/script.py":
        (repository / "script.py").write_text(
            "raise SystemExit(0)\n",
            encoding="utf-8",
        )

    protected = model_commands.resolve_behavior_verifier_paths(
        repository,
        (("python", script_argument),),
    )

    expected = "checks/script.py" if "/" in script_argument.strip("./") else "script.py"
    assert protected == (expected,)


@pytest.mark.parametrize("escape_kind", ("absolute", "traversal", "symlink"))
def test_python_script_escape_is_rejected(
    tmp_path: Path,
    escape_kind: str,
) -> None:
    repository = _repository(tmp_path)
    outside = tmp_path / "outside.py"
    outside.write_text("raise SystemExit(0)\n", encoding="utf-8")
    if escape_kind == "absolute":
        script_argument = str(outside)
    elif escape_kind == "traversal":
        script_argument = "../outside.py"
    else:
        (repository / "verify.py").symlink_to(outside)
        script_argument = "verify.py"

    with pytest.raises(CliVerificationError) as raised:
        model_commands.resolve_behavior_verifier_paths(
            repository,
            (("python", script_argument),),
        )

    assert raised.value.code == "verification.behavior_path_outside_repository"


def test_path_probe_oserror_becomes_stable_domain_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = _repository(tmp_path)
    candidate = repository / "script.py"
    original_exists = Path.exists

    def fail_candidate(path: Path) -> bool:
        """只在目标脚本探测处模拟底层文件系统错误。"""
        if path == candidate:
            raise OSError(errno.ENAMETOOLONG, "fixture")
        return original_exists(path)

    monkeypatch.setattr(Path, "exists", fail_candidate)

    with pytest.raises(CliVerificationError) as raised:
        model_commands.resolve_behavior_verifier_paths(
            repository,
            (("python", "script.py"),),
        )

    assert raised.value.code == "verification.behavior_path_unreadable"
    assert "fixture" not in raised.value.summary


@pytest.mark.parametrize(
    "command",
    (
        ("git", "diff", "--check"),
        ("bash", "-c", "printf 'ok'"),
        ("node", "--eval", "console.log('ok')"),
    ),
)
def test_non_python_non_path_arguments_are_not_probed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    command: Command,
) -> None:
    repository = _repository(tmp_path)
    original_exists = Path.exists
    probed_paths: list[Path] = []

    def record_exists(path: Path) -> bool:
        """记录普通 argv 是否被误判为仓库路径。"""
        probed_paths.append(path)
        return original_exists(path)

    monkeypatch.setattr(Path, "exists", record_exists)

    protected = model_commands.resolve_behavior_verifier_paths(
        repository,
        (command,),
    )

    assert protected == ()
    assert probed_paths == []


@pytest.mark.parametrize(
    "command",
    (
        (),
        ("",),
        ("python", "-c"),
        ("python", "-m"),
        ("python", "bad\x00.py"),
    ),
)
def test_invalid_verifier_argv_is_rejected(
    tmp_path: Path,
    command: tuple[str, ...],
) -> None:
    repository = _repository(tmp_path)

    with pytest.raises(CliVerificationError) as raised:
        model_commands.resolve_behavior_verifier_paths(
            repository,
            (command,),
        )

    assert raised.value.code == "verification.behavior_command_invalid"
