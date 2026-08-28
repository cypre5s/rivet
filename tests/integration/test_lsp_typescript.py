"""使用真实 TypeScript Language Server 验证跨文件语义。"""

from __future__ import annotations

from pathlib import Path

import pytest

from rivet.context.lsp_manifest import LspManifestRegistry
from rivet.context.lsp_models import LspPosition
from rivet.context.lsp_sidecar import LspSidecar
from rivet.kernel.resources import ResourceScope


@pytest.mark.asyncio
async def test_typescript_definition_references_and_symbols(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    (repository / "tsconfig.json").write_text(
        '{"compilerOptions":{"strict":true,"module":"NodeNext",'
        '"moduleResolution":"NodeNext"},"include":["*.ts"]}\n',
        encoding="utf-8",
    )
    (repository / "model.ts").write_text(
        "export class User { readonly id = 1 }\n", encoding="utf-8"
    )
    (repository / "use.ts").write_text(
        'import { User } from "./model.js"\nexport const current = new User()\n',
        encoding="utf-8",
    )
    registry = LspManifestRegistry.load_builtin(repository_root=Path.cwd())
    manifest = registry.for_path("use.ts")
    scope = ResourceScope("context.lsp.typescript")
    sidecar = LspSidecar(manifest, repository_root=repository, scope=scope)

    definitions = await sidecar.definition("use.ts", LspPosition(0, 9))
    references = await sidecar.references("use.ts", LspPosition(0, 9))
    symbols = await sidecar.document_symbols("model.ts")

    assert {location.path for location in definitions} == {"model.ts"}
    assert {location.path for location in references} >= {"model.ts", "use.ts"}
    assert "User" in {symbol.name for symbol in symbols}
    await sidecar.close()
    await scope.close()
    scope.assert_empty()
