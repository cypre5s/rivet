"""传播用户、仓库、外部内容和工具输出的来源污点。"""

from __future__ import annotations

from dataclasses import dataclass

from rivet.contracts.guard import TaintSource


@dataclass(frozen=True, slots=True)
class TaintedText:
    """将文本与不可丢失的来源集合绑定。"""

    content: str
    sources: frozenset[TaintSource]

    def __post_init__(self) -> None:
        if not self.sources:
            raise ValueError("污点文本必须记录至少一个来源")

    @classmethod
    def from_user(cls, content: str) -> TaintedText:
        """标记用户在当前交互中直接给出的文字。"""
        return cls(content, frozenset({TaintSource.USER_INSTRUCTION}))

    @classmethod
    def from_repository(cls, content: str) -> TaintedText:
        """标记从仓库或本地文件抽取的不可信文字。"""
        return cls(content, frozenset({TaintSource.REPOSITORY_DATA}))

    @classmethod
    def from_external(cls, content: str) -> TaintedText:
        """标记从网络或外部资料得到的不可信文字。"""
        return cls(content, frozenset({TaintSource.EXTERNAL_CONTENT}))

    @classmethod
    def from_tool(cls, content: str) -> TaintedText:
        """标记本地工具观察产生的不可信文字。"""
        return cls(content, frozenset({TaintSource.TOOL_OUTPUT}))

    @classmethod
    def combine(
        cls,
        *values: TaintedText,
        separator: str = "",
    ) -> TaintedText:
        """拼接文本并取全部来源的并集。"""
        if not values:
            raise ValueError("至少需要一个污点文本")
        return cls(
            separator.join(value.content for value in values),
            frozenset(source for value in values for source in value.sources),
        )

    @property
    def is_trusted_for_permission(self) -> bool:
        """仅当全部来源都是当前用户指令时允许产生询问。"""
        return self.sources == frozenset({TaintSource.USER_INSTRUCTION})
