"""验证 Tree-sitter 延迟导入和多语言结构抽取。"""

from __future__ import annotations

import subprocess
import sys

from rivet.context.syntax import extract_syntax_document


def test_context_import_does_not_import_tree_sitter() -> None:
    script = (
        "import sys; import rivet.context.engine; "
        "assert not any(name == 'tree_sitter' or name.startswith('tree_sitter_') "
        "for name in sys.modules)"
    )

    result = subprocess.run(
        [sys.executable, "-c", script], check=False, capture_output=True, text=True
    )

    assert result.returncode == 0, result.stderr


def test_python_syntax_extracts_functions_classes_imports_and_tests() -> None:
    document = extract_syntax_document(
        "tests/test_service.py",
        "from app.service import Service\n\n"
        "class TestService:\n"
        "    def test_runs(self):\n"
        "        return Service()\n",
    )

    assert {symbol.name for symbol in document.symbols} >= {"TestService", "test_runs"}
    assert document.imports == ("from app.service import Service",)
    assert document.test_names == ("test_runs",)


def test_typescript_and_javascript_syntax_extract_symbols() -> None:
    typescript = extract_syntax_document(
        "src/card.ts",
        "import { trim } from './text'\n"
        "export class UserCard { displayName(): string { return trim('x') } }\n",
    )
    javascript = extract_syntax_document(
        "src/router.js",
        "export function registerRoute(path) { return path }\n"
        "test('route', () => registerRoute('/'))\n",
    )

    assert {symbol.name for symbol in typescript.symbols} >= {
        "UserCard",
        "displayName",
    }
    assert {symbol.name for symbol in javascript.symbols} >= {"registerRoute"}
    assert javascript.test_names == ("route",)
