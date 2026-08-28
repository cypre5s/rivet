"""验证语义升级策略、Context Item 转换、边际收益和 fallback。"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from rivet.context.lsp_client import LspProcessExitedError
from rivet.context.lsp_manifest import LspManifestRegistry, LspServerManifest
from rivet.context.lsp_models import LspLocation, LspPosition, LspRange
from rivet.context.semantic import (
    SemanticContextRetriever,
    SemanticOperation,
    SemanticRequest,
    SemanticRetrievalStatus,
)
from rivet.contracts.context import ContextBudget, ContextLevel
from rivet.kernel.resources import ResourceScope

NOW = datetime(2026, 8, 28, tzinfo=UTC)
BUDGET = ContextBudget(
    total_tokens=2_000,
    required_tokens=200,
    working_tokens=1_600,
    history_tokens=200,
)


class FakeSidecar:
    """记录是否被调用并返回固定语义结果或失败。"""

    def __init__(
        self, locations: tuple[LspLocation, ...], *, fail: bool = False
    ) -> None:
        self.locations = locations
        self.fail = fail
        self.call_count = 0
        self.is_running = False

    async def definition(
        self, path: str, position: LspPosition
    ) -> tuple[LspLocation, ...]:
        del path, position
        self.call_count += 1
        self.is_running = True
        if self.fail:
            raise LspProcessExitedError("fixture crash")
        return self.locations

    async def references(
        self, path: str, position: LspPosition
    ) -> tuple[LspLocation, ...]:
        return await self.definition(path, position)

    async def close(self) -> None:
        self.is_running = False


def _registry(repository: Path) -> LspManifestRegistry:
    """构造无需真实 executable 的 Python suffix 注册表。"""
    return LspManifestRegistry(
        (
            LspServerManifest(
                server_id="fixture",
                language_ids=("python",),
                suffixes=(".py",),
                executable_candidates=("unused",),
                arguments=(),
                initialization_options={},
                repository_root=repository,
            ),
        )
    )


def _location(path: str) -> LspLocation:
    """构造目标文件首行范围。"""
    return LspLocation(
        path,
        LspRange(LspPosition(0, 0), LspPosition(0, 6)),
    )


def _repository(tmp_path: Path) -> Path:
    """创建包含跨文件符号的小型仓库。"""
    repository = tmp_path / "repository"
    repository.mkdir()
    (repository / "use.py").write_text(
        "from target import helper\nresult = helper()\n", encoding="utf-8"
    )
    (repository / "target.py").write_text(
        "def helper():\n    return 1\n", encoding="utf-8"
    )
    return repository


@pytest.mark.asyncio
async def test_nonsemantic_task_does_not_start_lsp_when_syntax_is_sufficient(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    fake = FakeSidecar((_location("target.py"),))
    scope = ResourceScope("context.semantic.not_needed")
    retriever = SemanticContextRetriever(
        repository,
        scope=scope,
        registry=_registry(repository),
        sidecar_factory=lambda _manifest: fake,
        clock=lambda: NOW,
    )

    result = await retriever.retrieve(
        "修复 helper 格式",
        budget=BUDGET,
        semantic_request=SemanticRequest(
            "use.py", LspPosition(0, 19), SemanticOperation.DEFINITION
        ),
    )

    assert result.status is SemanticRetrievalStatus.NOT_NEEDED
    assert fake.call_count == 0
    assert all(
        item.retrieval_level < ContextLevel.LSP for item in result.selection.items
    )
    await retriever.close()
    await scope.close()


@pytest.mark.asyncio
async def test_explicit_cross_file_task_converts_lsp_location_to_context_item(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    fake = FakeSidecar((_location("target.py"),))
    scope = ResourceScope("context.semantic.definition")
    retriever = SemanticContextRetriever(
        repository,
        scope=scope,
        registry=_registry(repository),
        sidecar_factory=lambda _manifest: fake,
        clock=lambda: NOW,
    )

    result = await retriever.retrieve(
        "查找跨文件 definition",
        budget=BUDGET,
        semantic_request=SemanticRequest(
            "use.py", LspPosition(0, 19), SemanticOperation.DEFINITION
        ),
    )

    lsp_items = [
        item
        for item in result.selection.items
        if item.retrieval_level is ContextLevel.LSP
    ]
    assert result.status is SemanticRetrievalStatus.COMPLETED
    assert fake.call_count == 1
    assert [item.repository_path for item in lsp_items] == ["target.py"]
    assert "LSP Definition 精确定位" in lsp_items[0].reason
    await retriever.close()
    await scope.close()


@pytest.mark.asyncio
async def test_lsp_failure_returns_explicit_syntax_fallback(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    fake = FakeSidecar((), fail=True)
    scope = ResourceScope("context.semantic.fallback")
    retriever = SemanticContextRetriever(
        repository,
        scope=scope,
        registry=_registry(repository),
        sidecar_factory=lambda _manifest: fake,
        clock=lambda: NOW,
    )

    result = await retriever.retrieve(
        "查找 helper 的 references 引用",
        budget=BUDGET,
        semantic_request=SemanticRequest(
            "use.py", LspPosition(0, 19), SemanticOperation.REFERENCES
        ),
    )

    assert result.status is SemanticRetrievalStatus.FALLBACK_SYNTAX
    assert result.fallback_used is True
    assert result.failure_code == "context.lsp.unavailable"
    assert all(
        item.retrieval_level < ContextLevel.LSP for item in result.selection.items
    )
    await retriever.close()
    await scope.close()


@pytest.mark.asyncio
async def test_duplicate_lsp_content_stops_on_low_marginal_gain(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    fake = FakeSidecar((_location("target.py"),))
    scope = ResourceScope("context.semantic.duplicate")
    retriever = SemanticContextRetriever(
        repository,
        scope=scope,
        registry=_registry(repository),
        sidecar_factory=lambda _manifest: fake,
        clock=lambda: NOW,
        minimum_marginal_gain=0.95,
    )

    result = await retriever.retrieve(
        "查找 helper 的跨文件 definition",
        budget=BUDGET,
        semantic_request=SemanticRequest(
            "use.py", LspPosition(0, 19), SemanticOperation.DEFINITION
        ),
    )

    assert result.status is SemanticRetrievalStatus.NO_MARGINAL_GAIN
    assert result.marginal_gain < 0.95
    assert all(
        item.retrieval_level < ContextLevel.LSP for item in result.selection.items
    )
    await retriever.close()
    await scope.close()
