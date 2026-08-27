"""按调用延迟导入 Tree-sitter 并抽取轻量语法结构。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import TYPE_CHECKING

from .errors import ContextSyntaxError

if TYPE_CHECKING:
    from tree_sitter import Node, Parser

SUPPORTED_SUFFIXES = frozenset({".py", ".js", ".jsx", ".ts", ".tsx"})


@dataclass(frozen=True, slots=True)
class SyntaxSymbol:
    """保存函数、类、方法或测试的名称和一基行范围。"""

    name: str
    kind: str
    start_line: int
    end_line: int


@dataclass(frozen=True, slots=True)
class SyntaxDocument:
    """保存单文件的符号、导入与测试名。"""

    path: str
    symbols: tuple[SyntaxSymbol, ...]
    imports: tuple[str, ...]
    test_names: tuple[str, ...]


def supports_syntax_path(path: str) -> bool:
    """判断路径是否由本阶段官方 grammar 覆盖。"""
    return PurePosixPath(path).suffix.lower() in SUPPORTED_SUFFIXES


def _create_parser(suffix: str) -> Parser:
    """只在语法能力被调用时导入 binding 与对应 grammar。"""
    from tree_sitter import Language, Parser

    if suffix == ".py":
        import tree_sitter_python

        language = Language(tree_sitter_python.language())
    elif suffix in {".ts", ".tsx"}:
        import tree_sitter_typescript

        capsule = (
            tree_sitter_typescript.language_tsx()
            if suffix == ".tsx"
            else tree_sitter_typescript.language_typescript()
        )
        language = Language(capsule)
    elif suffix in {".js", ".jsx"}:
        import tree_sitter_javascript

        language = Language(tree_sitter_javascript.language())
    else:
        raise ContextSyntaxError(f"不支持的语法文件后缀：{suffix}")
    parser = Parser()
    parser.language = language
    return parser


def _text(source: bytes, node: Node) -> str:
    """按 Tree-sitter 字节区间提取严格 UTF-8 文本。"""
    return source[node.start_byte : node.end_byte].decode("utf-8", errors="strict")


def _name_from_field(source: bytes, node: Node) -> str | None:
    """读取各语言定义节点的 name 字段。"""
    name_node = node.child_by_field_name("name")
    return _text(source, name_node) if name_node is not None else None


def extract_syntax_document(path: str, content: str) -> SyntaxDocument:
    """解析一个受支持文本并稳定抽取局部结构。"""
    suffix = PurePosixPath(path).suffix.lower()
    parser = _create_parser(suffix)
    source = content.encode("utf-8")
    tree = parser.parse(source)
    symbols: list[SyntaxSymbol] = []
    imports: list[tuple[int, str]] = []
    tests: list[tuple[int, str]] = []
    stack = [tree.root_node]
    while stack:
        node = stack.pop()
        stack.extend(reversed(node.named_children))
        name: str | None = None
        kind: str | None = None
        if node.type in {"function_definition", "function_declaration"}:
            name = _name_from_field(source, node)
            kind = "function"
        elif node.type in {"class_definition", "class_declaration"}:
            name = _name_from_field(source, node)
            kind = "class"
        elif node.type == "method_definition":
            name = _name_from_field(source, node)
            kind = "method"
        elif node.type == "variable_declarator":
            value = node.child_by_field_name("value")
            if value is not None and value.type in {
                "arrow_function",
                "function_expression",
            }:
                name = _name_from_field(source, node)
                kind = "function"
        if name is not None and kind is not None:
            symbol = SyntaxSymbol(
                name=name,
                kind="test" if name.startswith("test") else kind,
                start_line=node.start_point.row + 1,
                end_line=max(node.start_point.row + 1, node.end_point.row + 1),
            )
            symbols.append(symbol)
            if name.startswith("test"):
                tests.append((symbol.start_line, name))
        if node.type in {"import_statement", "import_from_statement"}:
            imports.append((node.start_point.row + 1, _text(source, node)))
        if node.type == "call_expression":
            function_node = node.child_by_field_name("function")
            arguments_node = node.child_by_field_name("arguments")
            if function_node is not None and arguments_node is not None:
                function_name = _text(source, function_node)
                if function_name in {"describe", "it", "test"}:
                    named_arguments = arguments_node.named_children
                    if named_arguments and named_arguments[0].type in {
                        "string",
                        "string_fragment",
                    }:
                        test_name = _text(source, named_arguments[0]).strip("'\"")
                        tests.append((node.start_point.row + 1, test_name))
    ordered_symbols = tuple(
        sorted(symbols, key=lambda item: (item.start_line, item.name, item.kind))
    )
    ordered_imports = tuple(text for _, text in sorted(set(imports)))
    ordered_tests = tuple(name for _, name in sorted(set(tests)))
    return SyntaxDocument(
        path=path,
        symbols=ordered_symbols,
        imports=ordered_imports,
        test_names=ordered_tests,
    )
