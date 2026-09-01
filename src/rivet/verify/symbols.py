"""从补丁前后源码抽取可核验的 changed symbols。"""

from __future__ import annotations

from collections.abc import Mapping

from rivet.context.errors import ContextSyntaxError
from rivet.context.syntax import extract_syntax_document, supports_syntax_path


def extract_changed_symbols(
    *,
    before: Mapping[str, str],
    after: Mapping[str, str],
) -> tuple[str, ...]:
    """比较 Python/JavaScript/TypeScript 符号和对应源码片段。"""
    changed: set[str] = set()
    for path in sorted(set(before) | set(after)):
        if not supports_syntax_path(path):
            continue
        old_symbols = _symbols(path, before.get(path))
        new_symbols = _symbols(path, after.get(path))
        for key in new_symbols.keys() - old_symbols.keys():
            changed.add(_render("added", path, key))
        for key in old_symbols.keys() - new_symbols.keys():
            changed.add(_render("removed", path, key))
        for key in old_symbols.keys() & new_symbols.keys():
            if old_symbols[key] != new_symbols[key]:
                changed.add(_render("modified", path, key))
    return tuple(sorted(changed))


def _symbols(path: str, content: str | None) -> dict[tuple[str, str], str]:
    """返回符号键与其精确行片段；缺失或解析失败时失败关闭为空。"""
    if content is None:
        return {}
    try:
        document = extract_syntax_document(path, content)
    except (ContextSyntaxError, ImportError, UnicodeError):
        return {}
    lines = content.splitlines(keepends=True)
    return {
        (symbol.kind, symbol.name): "".join(
            lines[symbol.start_line - 1 : symbol.end_line]
        )
        for symbol in document.symbols
    }


def _render(change: str, path: str, key: tuple[str, str]) -> str:
    kind, name = key
    return f"{change}:{path}:{kind}:{name}"
