"""实现有限文本读取和事务根内的持久原子写入。"""

from __future__ import annotations

import codecs
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

from rivet.tools.errors import FileToolError
from rivet.tools.paths import WorkspaceBoundary

DEFAULT_MAX_FILE_BYTES = 2 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class TextReadResult:
    """描述已解码文本及真实返回行范围。"""

    path: str
    content: str
    encoding: str
    start_line: int
    end_line: int
    truncated: bool = False


def _decode_text(content: bytes) -> tuple[str, str]:
    """只接受 UTF-8（可带 BOM），并拒绝二进制与其他编码。"""
    if b"\x00" in content:
        raise FileToolError("file.binary_content", "文件包含二进制 NUL 字节")
    codec_name = "utf-8-sig" if content.startswith(codecs.BOM_UTF8) else "utf-8"
    try:
        decoded = content.decode(codec_name, errors="strict")
    except UnicodeDecodeError as error:
        raise FileToolError(
            "file.encoding_unsupported", "文件不是受支持的 UTF-8 文本"
        ) from error
    control_count = sum(
        ord(character) < 32 and character not in "\n\r\t\f\b" for character in decoded
    )
    if decoded and control_count / len(decoded) > 0.02:
        raise FileToolError("file.binary_content", "文件包含过多二进制控制字符")
    return decoded, codec_name


class FileReader:
    """从主仓库读取有大小边界的安全文本。"""

    def __init__(
        self,
        boundary: WorkspaceBoundary,
        *,
        max_file_bytes: int = DEFAULT_MAX_FILE_BYTES,
    ) -> None:
        if max_file_bytes <= 0:
            raise ValueError("文件大小上限必须大于零")
        self._boundary = boundary
        self._max_file_bytes = max_file_bytes

    def read_text(self, relative_path: str) -> TextReadResult:
        """读取完整文本并报告编码和行数。"""
        path, content, encoding = self._load(relative_path)
        line_count = len(content.splitlines())
        return TextReadResult(
            path=self._boundary.repository_relative(path),
            content=content,
            encoding=encoding,
            start_line=1,
            end_line=max(1, line_count),
        )

    def read_range(
        self,
        relative_path: str,
        *,
        start_line: int,
        end_line: int,
    ) -> TextReadResult:
        """按一基闭区间读取行，保留原始换行符。"""
        if start_line < 1 or end_line < start_line:
            raise FileToolError("file.line_range_invalid", "行范围必须是一基闭区间")
        path, content, encoding = self._load(relative_path)
        lines = content.splitlines(keepends=True)
        selected = lines[start_line - 1 : end_line]
        actual_end = start_line + len(selected) - 1 if selected else start_line
        return TextReadResult(
            path=self._boundary.repository_relative(path),
            content="".join(selected),
            encoding=encoding,
            start_line=start_line,
            end_line=actual_end,
            truncated=end_line < len(lines),
        )

    def _load(self, relative_path: str) -> tuple[Path, str, str]:
        """在读取前检查类型和 stat 大小，并在读取后再次限制大小。"""
        path = self._boundary.resolve_repository(relative_path, require_file=True)
        if path.stat().st_size > self._max_file_bytes:
            raise FileToolError("file.size_exceeded", "文件超过读取大小上限")
        content = path.read_bytes()
        if len(content) > self._max_file_bytes:
            raise FileToolError("file.size_exceeded", "文件超过读取大小上限")
        decoded, encoding = _decode_text(content)
        return path, decoded, encoding


class TransactionFileWriter:
    """只修改独立事务根，并以同目录临时文件原子替换。"""

    def __init__(self, boundary: WorkspaceBoundary) -> None:
        self._boundary = boundary

    def write(self, relative_path: str, content: str) -> None:
        """原子覆盖已存在的事务普通文件。"""
        target = self._boundary.resolve_transaction(
            relative_path, require_exists=True, require_file=True
        )
        self._atomic_write(target, content, mode=target.stat().st_mode & 0o777)

    def create(self, relative_path: str, content: str) -> None:
        """原子创建不存在的事务文件。"""
        target = self._boundary.resolve_transaction(relative_path, require_exists=False)
        if target.exists():
            raise FileToolError("file.already_exists", "事务目标文件已存在")
        self._atomic_write(target, content, mode=0o600)

    def replace(
        self,
        relative_path: str,
        old_text: str,
        new_text: str,
        *,
        expected_count: int = 1,
    ) -> int:
        """只在匹配次数符合冻结预期时执行文本替换。"""
        if not old_text or expected_count <= 0:
            raise FileToolError("file.replace_invalid", "替换文本和预期次数必须有效")
        target = self._boundary.resolve_transaction(
            relative_path, require_exists=True, require_file=True
        )
        try:
            content = target.read_text(encoding="utf-8", errors="strict")
        except UnicodeDecodeError as error:
            raise FileToolError(
                "file.replace_encoding", "替换工具只接受 UTF-8 文本"
            ) from error
        actual_count = content.count(old_text)
        if actual_count != expected_count:
            raise FileToolError(
                "file.replace_count_mismatch", "替换匹配次数与预期不一致"
            )
        self._atomic_write(
            target,
            content.replace(old_text, new_text),
            mode=target.stat().st_mode & 0o777,
        )
        return actual_count

    def delete(self, relative_path: str) -> None:
        """删除事务普通文件，不允许目录或 symlink。"""
        target = self._boundary.resolve_transaction(
            relative_path, require_exists=True, require_file=True
        )
        target.unlink()
        self._fsync_directory(target.parent)

    @staticmethod
    def _atomic_write(target: Path, content: str, *, mode: int) -> None:
        """在目标目录 fsync 临时文件、replace，再 fsync 目录。"""
        target.parent.mkdir(parents=True, exist_ok=True)
        file_descriptor, temporary_name = tempfile.mkstemp(
            prefix=".rivet-", suffix=".tmp", dir=target.parent
        )
        temporary_path = Path(temporary_name)
        try:
            os.fchmod(file_descriptor, mode)
            temporary_file = os.fdopen(file_descriptor, "wb")
            file_descriptor = -1
            with temporary_file:
                temporary_file.write(content.encode("utf-8"))
                temporary_file.flush()
                os.fsync(temporary_file.fileno())
            os.replace(temporary_path, target)
            TransactionFileWriter._fsync_directory(target.parent)
        except BaseException:
            if file_descriptor >= 0:
                os.close(file_descriptor)
            temporary_path.unlink(missing_ok=True)
            raise

    @staticmethod
    def _fsync_directory(directory: Path) -> None:
        """确保持久化目录项变更。"""
        file_descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(file_descriptor)
        finally:
            os.close(file_descriptor)
