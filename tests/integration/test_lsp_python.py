"""使用真实 Pyright 兼容服务验证 Python 跨文件语义。"""

from __future__ import annotations

from pathlib import Path

import pytest

from rivet.context.lsp_manifest import LspManifestRegistry
from rivet.context.lsp_models import LspPosition
from rivet.context.lsp_sidecar import LspSidecar
from rivet.kernel.resources import ResourceScope


@pytest.mark.asyncio
async def test_python_definition_references_and_symbols(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    (repository / "model.py").write_text("class User:\n    pass\n", encoding="utf-8")
    (repository / "use.py").write_text(
        "from model import User\n\ndef build() -> User:\n    return User()\n",
        encoding="utf-8",
    )
    registry = LspManifestRegistry.load_builtin(repository_root=Path.cwd())
    manifest = registry.for_path("use.py")
    scope = ResourceScope("context.lsp.python")
    sidecar = LspSidecar(manifest, repository_root=repository, scope=scope)

    definitions = await sidecar.definition("use.py", LspPosition(0, 18))
    references = await sidecar.references("use.py", LspPosition(0, 18))
    symbols = await sidecar.document_symbols("model.py")

    assert {location.path for location in definitions} == {"model.py"}
    assert {location.path for location in references} >= {"model.py", "use.py"}
    assert "User" in {symbol.name for symbol in symbols}
    await sidecar.close()
    await scope.close()
    scope.assert_empty()
