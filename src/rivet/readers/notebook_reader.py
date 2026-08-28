"""读取 Jupyter Notebook 单元并限制模型输出正文。"""

from __future__ import annotations

import json
from typing import cast

from rivet.contracts.readers import ReaderStatus, SupportLevel

from .base import ReaderContext, ReaderError, ReaderPayload
from .text_reader import decode_text_bytes, whole_source_span

MAX_CELL_OUTPUT_CHARS = 8_192


def _joined_text(value: object) -> str:
    """合并 Notebook 字符串或字符串数组字段。"""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        items = cast(list[object], value)
        if all(isinstance(item, str) for item in items):
            return "".join(cast(list[str], items))
    return ""


class NotebookReader:
    """抽取 Markdown、代码和受限输出而不执行任何单元。"""

    reader_id = "reader.notebook"
    reader_version = "1.0.0"

    async def read(self, context: ReaderContext) -> ReaderPayload:
        """严格解析 nbformat JSON 并按原单元顺序渲染。"""
        content_bytes = context.inspection.absolute_path.read_bytes()
        source, encoding = decode_text_bytes(content_bytes)
        try:
            raw_payload: object = json.loads(source)
        except json.JSONDecodeError as error:
            raise ReaderError(
                "reader.notebook.invalid_json", "Notebook JSON 无效"
            ) from error
        if not isinstance(raw_payload, dict):
            raise ReaderError("reader.notebook.invalid_schema", "Notebook cells 无效")
        payload = cast(dict[str, object], raw_payload)
        raw_cells = payload.get("cells")
        if not isinstance(raw_cells, list):
            raise ReaderError("reader.notebook.invalid_schema", "Notebook cells 无效")
        cells = cast(list[object], raw_cells)
        sections: list[str] = []
        output_truncated = False
        for index, raw_cell in enumerate(cells, start=1):
            if not isinstance(raw_cell, dict):
                raise ReaderError(
                    "reader.notebook.invalid_schema", "Notebook cell 无效"
                )
            cell = cast(dict[str, object], raw_cell)
            cell_type = cell.get("cell_type")
            if not isinstance(cell_type, str):
                raise ReaderError(
                    "reader.notebook.invalid_schema", "Notebook cell_type 无效"
                )
            execution_count = cell.get("execution_count")
            heading = f"cell[{index}] {cell_type}"
            if cell_type == "code":
                heading += f" execution_count={execution_count}"
            sections.append(f"## {heading}\n{_joined_text(cell.get('source'))}")
            if cell_type != "code":
                continue
            outputs = cell.get("outputs", [])
            if not isinstance(outputs, list):
                raise ReaderError(
                    "reader.notebook.invalid_schema", "Notebook outputs 无效"
                )
            for output_index, raw_output in enumerate(
                cast(list[object], outputs), start=1
            ):
                if not isinstance(raw_output, dict):
                    continue
                output = cast(dict[str, object], raw_output)
                rendered = _joined_text(output.get("text"))
                if not rendered:
                    data = output.get("data")
                    if isinstance(data, dict):
                        rendered = _joined_text(
                            cast(dict[str, object], data).get("text/plain")
                        )
                if len(rendered) > MAX_CELL_OUTPUT_CHARS:
                    rendered = rendered[:MAX_CELL_OUTPUT_CHARS]
                    output_truncated = True
                if rendered:
                    sections.append(f"### output[{index}.{output_index}]\n{rendered}")
        raw_nbformat = payload.get("nbformat")
        nbformat = raw_nbformat if isinstance(raw_nbformat, int) else None
        return ReaderPayload(
            status=ReaderStatus.TRUNCATED if output_truncated else ReaderStatus.SUCCESS,
            support_level=SupportLevel.NATIVE,
            content="\n\n".join(sections) + ("\n" if sections else ""),
            metadata={
                "encoding": encoding,
                "cell_count": len(cells),
                "nbformat": nbformat,
            },
            warnings=("reader.notebook.output_truncated",) if output_truncated else (),
            source_spans=whole_source_span(context.inspection.source_path, source),
            truncated=output_truncated,
        )
