"""验证仓库清单分类和任务信号抽取。"""

from rivet.context.inventory import (
    FileRole,
    InventoryEntry,
    RepositorySnapshot,
    classify_repository_path,
)
from rivet.context.signals import extract_task_signals


def test_inventory_classifies_project_structure() -> None:
    assert classify_repository_path("pyproject.toml") is FileRole.MANIFEST
    assert classify_repository_path("src/rivet/__main__.py") is FileRole.ENTRYPOINT
    assert classify_repository_path("tests/test_context.py") is FileRole.TEST
    assert classify_repository_path("tsconfig.json") is FileRole.BUILD_CONFIG
    assert classify_repository_path("src/rivet/context/engine.py") is FileRole.SOURCE
    assert classify_repository_path("docs/design.md") is FileRole.DOCUMENTATION
    assert classify_repository_path("assets/logo.png") is FileRole.OTHER


def test_snapshot_exposes_project_landmarks() -> None:
    entries = tuple(
        InventoryEntry(path, 1, 1, role, True)
        for path, role in (
            ("pyproject.toml", FileRole.MANIFEST),
            ("src/app/__main__.py", FileRole.ENTRYPOINT),
            ("tests/test_app.py", FileRole.TEST),
            ("tsconfig.json", FileRole.BUILD_CONFIG),
        )
    )
    snapshot = RepositorySnapshot(entries, "sha256:" + ("a" * 64))

    assert snapshot.manifests == ("pyproject.toml",)
    assert snapshot.entrypoints == ("src/app/__main__.py",)
    assert snapshot.tests == ("tests/test_app.py",)
    assert snapshot.build_configs == ("tsconfig.json",)


def test_task_signals_extract_stack_path_symbols_and_keywords() -> None:
    signals = extract_task_signals(
        'Traceback: File "src/payments/gateway.py", line 17, in charge\n'
        "PaymentDeclined: calculate_total failed"
    )

    assert signals.stack_paths == ("src/payments/gateway.py",)
    assert {"PaymentDeclined", "calculate_total", "charge"} <= set(signals.symbols)
    assert "failed" in signals.keywords


def test_task_signals_are_stable_and_bounded() -> None:
    task = " ".join(f"symbol_{index}" for index in range(200))

    first = extract_task_signals(task)
    second = extract_task_signals(task)

    assert first == second
    assert len(first.search_terms) <= 32


def test_task_signals_keep_chinese_structure_hint() -> None:
    signals = extract_task_signals("更新项目依赖与构建配置")

    assert signals.keywords == ("更新项目依赖与构建配置",)
