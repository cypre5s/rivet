"""安全解析 JSON、YAML、TOML、XML、CSV 与 TSV。"""

from __future__ import annotations

import csv
import io
import json
import tomllib
from collections.abc import Mapping, Sequence
from typing import cast
from xml.etree.ElementTree import Element

from pydantic import JsonValue

from rivet.contracts.readers import ReaderStatus, SupportLevel

from .base import ReaderContext, ReaderError, ReaderPayload
from .text_reader import decode_text_bytes, whole_source_span


def _line_for_token(source: str, token: str) -> int:
    """以第一次词法出现位置提供保守来源行。"""
    index = source.find(token)
    return 1 if index < 0 else source.count("\n", 0, index) + 1


def _scalar_text(value: object) -> str:
    """稳定渲染结构化标量并限制单值长度。"""
    if value is None:
        rendered = "null"
    elif isinstance(value, bool):
        rendered = "true" if value else "false"
    elif isinstance(value, str):
        rendered = value
    else:
        rendered = str(value)
    return rendered[:4_096]


def _flatten(value: object, *, path: str, source: str, output: list[str]) -> None:
    """将 JSON-like 对象展平为带对象路径和行号的文本。"""
    if isinstance(value, Mapping):
        mapping = cast(Mapping[object, object], value)
        for raw_key in sorted(mapping, key=lambda item: str(item)):
            key = str(raw_key)
            _flatten(
                mapping[raw_key],
                path=f"{path}.{key}",
                source=source,
                output=output,
            )
        return
    if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
        for index, item in enumerate(cast(Sequence[object], value), start=1):
            _flatten(item, path=f"{path}[{index}]", source=source, output=output)
        return
    token = path.rsplit(".", maxsplit=1)[-1].split("[", maxsplit=1)[0]
    output.append(
        f"{path} @ line {_line_for_token(source, token)} = {_scalar_text(value)}"
    )


def _parse_jsonl(source: str) -> object:
    """逐行解析 JSONL 并保留一基记录序号。"""
    records: list[JsonValue] = []
    for line_number, line in enumerate(source.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            records.append(cast(JsonValue, json.loads(line)))
        except json.JSONDecodeError as error:
            raise ReaderError(
                "reader.structured.invalid_jsonl",
                f"JSONL 第 {line_number} 行无效",
            ) from error
    return records


def _parse_xml(source: str) -> list[str]:
    """用 defusedxml 遍历元素、属性和正文。"""
    from defusedxml import ElementTree

    try:
        root = ElementTree.fromstring(source)
    except Exception as error:
        raise ReaderError(
            "reader.structured.invalid_xml", "XML 无法安全解析"
        ) from error
    output: list[str] = []

    def visit(element: Element, parent_path: str) -> None:
        """递归渲染已由 defusedxml 验证的 Element。"""
        tag = element.tag
        path = f"{parent_path}/{tag}"
        attributes = element.attrib
        for key in sorted(attributes):
            output.append(
                f"{path}/@{key} @ line {_line_for_token(source, key)} = "
                f"{attributes[key][:4_096]}"
            )
        text = element.text
        if text and text.strip():
            output.append(
                f"{path} @ line {_line_for_token(source, text.strip())} = "
                f"{text.strip()[:4_096]}"
            )
        for child in element:
            visit(child, path)

    visit(root, "")
    return output


def _parse_delimited(source: str, *, delimiter: str) -> list[str]:
    """按表头将 CSV/TSV 渲染为一基行对象路径。"""
    reader = csv.DictReader(io.StringIO(source), delimiter=delimiter)
    output: list[str] = []
    for row_number, row in enumerate(reader, start=2):
        for key in reader.fieldnames or ():
            value = row.get(key, "") or ""
            output.append(
                f"row[{row_number}].{key} @ line {row_number} = {value[:4_096]}"
            )
    return output


class StructuredReader:
    """将常见结构化数据转换为可定位对象路径。"""

    reader_id = "reader.structured"
    reader_version = "1.0.0"

    async def read(self, context: ReaderContext) -> ReaderPayload:
        """选择格式专用安全解析器并输出稳定顺序。"""
        content_bytes = context.inspection.absolute_path.read_bytes()
        source, encoding = decode_text_bytes(content_bytes)
        detected_format = context.inspection.detected_format
        value: object = None
        lines: list[str] = []
        try:
            if detected_format == "json":
                value = json.loads(source)
            elif detected_format == "jsonl":
                value = _parse_jsonl(source)
            elif detected_format == "yaml":
                import yaml

                value = yaml.safe_load(source)
            elif detected_format == "toml":
                value = tomllib.loads(source)
            elif detected_format == "xml":
                lines = _parse_xml(source)
                value = None
            elif detected_format in {"csv", "tsv"}:
                lines = _parse_delimited(
                    source,
                    delimiter="," if detected_format == "csv" else "\t",
                )
                value = None
            else:
                raise ReaderError(
                    "reader.structured.format_unknown", "结构化格式未注册"
                )
        except ReaderError:
            raise
        except (ValueError, TypeError) as error:
            raise ReaderError(
                "reader.structured.invalid_content", "结构化内容无法解析"
            ) from error
        if detected_format not in {"xml", "csv", "tsv"}:
            _flatten(value, path="$", source=source, output=lines)
        content = "\n".join(lines)
        if content:
            content += "\n"
        return ReaderPayload(
            status=ReaderStatus.SUCCESS,
            support_level=SupportLevel.NATIVE,
            content=content,
            metadata={
                "encoding": encoding,
                "record_count": len(lines),
            },
            source_spans=whole_source_span(context.inspection.source_path, source),
        )
