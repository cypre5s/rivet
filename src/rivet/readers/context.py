"""将不可信 Reader 输出转换为带边界标记的 Context Item。"""

from __future__ import annotations

import hashlib

from rivet.contracts.common import Timestamp
from rivet.contracts.context import ContextItem, ContextLevel, ContextUseState
from rivet.contracts.readers import ReaderResult


def reader_result_to_context(
    result: ReaderResult,
    *,
    selected_at: Timestamp,
) -> ContextItem:
    """保留来源和哈希，并用显式标签阻止文档指令越权。"""
    wrapped = (
        f"[不可信文件数据 source={result.source_path}]\n"
        f"{result.content}"
        "[/不可信文件数据]\n"
    )
    identifier_hash = hashlib.sha256(
        f"{result.source_path}\0{result.source_sha256}\0{result.reader_id}".encode()
    ).hexdigest()[:24]
    content_hash = hashlib.sha256(wrapped.encode("utf-8")).hexdigest()
    return ContextItem(
        context_item_id=f"context_reader_{identifier_hash}",
        repository_path=result.source_path,
        span=result.source_spans[0] if result.source_spans else None,
        content=wrapped,
        reason=f"Reader {result.reader_id} 提供的不可信文件数据",
        retrieval_level=ContextLevel.LEXICAL,
        content_sha256=f"sha256:{content_hash}",
        token_estimate=(len(wrapped.encode("utf-8")) + 3) // 4,
        selected_at=selected_at,
        freshness=result.source_sha256,
        use_state=ContextUseState.SELECTED,
    )
