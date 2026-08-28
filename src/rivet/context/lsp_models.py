"""验证 LSP 位置、Location、LocationLink 与 DocumentSymbol。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import cast
from urllib.parse import unquote, urlsplit


class LspResultError(RuntimeError):
    """表示语言服务结果结构无效或越过仓库边界。"""


@dataclass(frozen=True, slots=True)
class LspPosition:
    """表示零基 UTF-16 字符位置。"""

    line: int
    character: int

    def __post_init__(self) -> None:
        if self.line < 0 or self.character < 0:
            raise ValueError("LSP 位置不得为负数")

    def to_json(self) -> dict[str, object]:
        """转换为 LSP JSON 字段。"""
        return {"line": self.line, "character": self.character}


@dataclass(frozen=True, slots=True)
class LspRange:
    """表示零基半开 LSP 范围。"""

    start: LspPosition
    end: LspPosition

    def __post_init__(self) -> None:
        if (self.end.line, self.end.character) < (
            self.start.line,
            self.start.character,
        ):
            raise ValueError("LSP 范围结束位置不得早于开始位置")


@dataclass(frozen=True, slots=True)
class LspLocation:
    """保存仓库相对路径与已验证范围。"""

    path: str
    range: LspRange


@dataclass(frozen=True, slots=True)
class LspDocumentSymbol:
    """保存文档符号及递归子符号。"""

    name: str
    kind: int
    range: LspRange
    selection_range: LspRange
    children: tuple[LspDocumentSymbol, ...] = ()


def _mapping(value: object, summary: str) -> dict[str, object]:
    """将未知 JSON 值收窄为字符串键对象。"""
    if not isinstance(value, dict):
        raise LspResultError(summary)
    return cast(dict[str, object], value)


def _integer(value: object, summary: str) -> int:
    """拒绝 bool 并收窄非负整数。"""
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise LspResultError(summary)
    return value


def parse_position(value: object) -> LspPosition:
    """解析严格的零基位置。"""
    position = _mapping(value, "LSP position 必须是对象")
    return LspPosition(
        _integer(position.get("line"), "LSP line 无效"),
        _integer(position.get("character"), "LSP character 无效"),
    )


def parse_range(value: object) -> LspRange:
    """解析并校验半开范围顺序。"""
    range_value = _mapping(value, "LSP range 必须是对象")
    try:
        return LspRange(
            parse_position(range_value.get("start")),
            parse_position(range_value.get("end")),
        )
    except ValueError as error:
        raise LspResultError("LSP range 顺序无效") from error


def repository_path_from_uri(uri: object, repository_root: Path) -> str:
    """只接受位于仓库内的本地 file URI。"""
    if not isinstance(uri, str):
        raise LspResultError("LSP URI 必须是字符串")
    parsed = urlsplit(uri)
    if parsed.scheme != "file" or parsed.netloc not in {"", "localhost"}:
        raise LspResultError("LSP 结果只允许本地 file URI")
    candidate = Path(unquote(parsed.path)).resolve(strict=False)
    root = repository_root.resolve(strict=True)
    try:
        relative = candidate.relative_to(root)
    except ValueError as error:
        raise LspResultError("LSP 结果越过仓库边界") from error
    if not relative.parts:
        raise LspResultError("LSP 结果必须指向仓库文件")
    return relative.as_posix()


def parse_locations(value: object, repository_root: Path) -> tuple[LspLocation, ...]:
    """统一解析 null、Location、Location[] 与 LocationLink[]。"""
    if value is None:
        return ()
    raw_locations: list[object] = (
        cast(list[object], value) if isinstance(value, list) else [value]
    )
    locations: list[LspLocation] = []
    for raw_location in raw_locations:
        location = _mapping(raw_location, "LSP location 必须是对象")
        if "targetUri" in location:
            uri = location.get("targetUri")
            range_value = location.get(
                "targetSelectionRange", location.get("targetRange")
            )
        else:
            uri = location.get("uri")
            range_value = location.get("range")
        locations.append(
            LspLocation(
                path=repository_path_from_uri(uri, repository_root),
                range=parse_range(range_value),
            )
        )
    return tuple(
        sorted(
            set(locations),
            key=lambda item: (
                item.path,
                item.range.start.line,
                item.range.start.character,
                item.range.end.line,
                item.range.end.character,
            ),
        )
    )


def parse_document_symbols(value: object) -> tuple[LspDocumentSymbol, ...]:
    """解析层级 DocumentSymbol；SymbolInformation 转为无子项符号。"""
    if value is None:
        return ()
    if not isinstance(value, list):
        raise LspResultError("documentSymbol 结果必须是数组")

    def parse_symbol(raw_symbol: object) -> LspDocumentSymbol:
        symbol = _mapping(raw_symbol, "DocumentSymbol 必须是对象")
        name = symbol.get("name")
        kind = symbol.get("kind")
        if not isinstance(name, str) or not name:
            raise LspResultError("DocumentSymbol name 无效")
        parsed_kind = _integer(kind, "DocumentSymbol kind 无效")
        if "location" in symbol:
            location = _mapping(
                symbol.get("location"), "SymbolInformation location 无效"
            )
            symbol_range = parse_range(location.get("range"))
            selection_range = symbol_range
        else:
            symbol_range = parse_range(symbol.get("range"))
            selection_range = parse_range(symbol.get("selectionRange"))
        raw_children = symbol.get("children", [])
        if not isinstance(raw_children, list):
            raise LspResultError("DocumentSymbol children 无效")
        return LspDocumentSymbol(
            name=name,
            kind=parsed_kind,
            range=symbol_range,
            selection_range=selection_range,
            children=tuple(
                parse_symbol(child) for child in cast(list[object], raw_children)
            ),
        )

    symbols = tuple(parse_symbol(item) for item in cast(list[object], value))
    return tuple(
        sorted(
            symbols,
            key=lambda item: (
                item.range.start.line,
                item.range.start.character,
                item.name,
            ),
        )
    )
