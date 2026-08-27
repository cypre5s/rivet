"""提供可逐字节输出或中途断开的 httpx 异步流。"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Iterable

import httpx


class RecordedByteStream(httpx.AsyncByteStream):
    """按固定 chunk 顺序输出并记录关闭。"""

    def __init__(
        self,
        chunks: Iterable[bytes],
        *,
        fail_after_chunks: int | None = None,
    ) -> None:
        self._chunks = tuple(chunks)
        self._fail_after_chunks = fail_after_chunks
        self.closed = False

    async def __aiter__(self) -> AsyncIterator[bytes]:
        """在配置位置制造可重试读错误。"""
        for index, chunk in enumerate(self._chunks):
            if self._fail_after_chunks == index:
                raise httpx.ReadError("recorded connection interrupted")
            yield chunk

    async def aclose(self) -> None:
        """记录响应流已释放。"""
        self.closed = True


class BlockingByteStream(httpx.AsyncByteStream):
    """在发出开始信号后阻塞，用于验证取消会关闭连接。"""

    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.closed = False

    async def __aiter__(self) -> AsyncIterator[bytes]:
        """保持读取挂起，直到调用方取消请求。"""
        self.started.set()
        await asyncio.Event().wait()
        if False:
            yield b""

    async def aclose(self) -> None:
        """记录 httpx 已退出响应上下文。"""
        self.closed = True
