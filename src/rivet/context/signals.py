"""从用户任务提取路径、符号和有限搜索词。"""

from __future__ import annotations

import re
from dataclasses import dataclass

MAX_SEARCH_TERMS = 32
STACK_PATH_PATTERN = re.compile(
    r"(?:File\s+[\"'])?((?:[A-Za-z0-9_.-]+/)+[A-Za-z0-9_.-]+"
    r"\.(?:py|pyi|js|jsx|ts|tsx|go|rs|java|kt|cs|cpp|c|h|toml|json|yaml|yml))"
)
STACK_SYMBOL_PATTERN = re.compile(r"\bin\s+([A-Za-z_][A-Za-z0-9_]*)")
TOKEN_PATTERN = re.compile(r"[A-Za-z_][A-Za-z0-9_.-]{2,}|[\u4e00-\u9fff]{2,}")
STOP_WORDS = frozenset(
    {
        "and",
        "error",
        "file",
        "fix",
        "from",
        "line",
        "the",
        "traceback",
        "with",
    }
)


@dataclass(frozen=True, slots=True)
class TaskSignals:
    """保存按首次出现顺序去重的任务检索信号。"""

    stack_paths: tuple[str, ...]
    symbols: tuple[str, ...]
    keywords: tuple[str, ...]
    search_terms: tuple[str, ...]


def _stable_unique(values: list[str]) -> tuple[str, ...]:
    """保留首次出现顺序并按大小写不敏感方式去重。"""
    seen: set[str] = set()
    selected: list[str] = []
    for value in values:
        key = value.casefold()
        if key not in seen:
            seen.add(key)
            selected.append(value)
    return tuple(selected)


def extract_task_signals(task: str) -> TaskSignals:
    """提取可解释且有数量上限的路径、符号和关键词。"""
    stack_paths = _stable_unique(STACK_PATH_PATTERN.findall(task))
    stack_symbols = list(STACK_SYMBOL_PATTERN.findall(task))
    tokens = TOKEN_PATTERN.findall(task)
    symbols = _stable_unique(
        stack_symbols
        + [
            token
            for token in tokens
            if "_" in token
            or (any(character.isupper() for character in token[1:]))
            or token[:1].isupper()
        ]
    )
    symbol_keys = {symbol.casefold() for symbol in symbols}
    keywords = _stable_unique(
        [
            token
            for token in tokens
            if token.casefold() not in STOP_WORDS
            and token.casefold() not in symbol_keys
            and "/" not in token
        ]
    )
    path_terms: list[str] = []
    for path in stack_paths:
        path_terms.append(path)
        filename = path.rsplit("/", maxsplit=1)[-1]
        path_terms.extend((filename, filename.rsplit(".", maxsplit=1)[0]))
    search_terms = _stable_unique(path_terms + list(symbols) + list(keywords))[
        :MAX_SEARCH_TERMS
    ]
    return TaskSignals(
        stack_paths=stack_paths,
        symbols=symbols,
        keywords=keywords,
        search_terms=search_terms,
    )
