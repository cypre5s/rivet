"""验证 Evidence changed symbols 来自真实 Python/JS/TS 语法差异。"""

from __future__ import annotations

from rivet.verify.symbols import extract_changed_symbols


def test_python_symbol_add_modify_and_delete_are_reported() -> None:
    symbols = extract_changed_symbols(
        before={
            "src/service.py": (
                "def retained():\n    return 'old'\n\ndef removed():\n    return 1\n"
            )
        },
        after={
            "src/service.py": (
                "def retained():\n    return 'new'\n\ndef added():\n    return 2\n"
            )
        },
    )

    assert symbols == (
        "added:src/service.py:function:added",
        "modified:src/service.py:function:retained",
        "removed:src/service.py:function:removed",
    )


def test_typescript_rename_records_removed_and_added_symbols() -> None:
    symbols = extract_changed_symbols(
        before={"src/old.ts": "export function oldName() { return 1; }\n"},
        after={"src/new.ts": "export function newName() { return 1; }\n"},
    )

    assert symbols == (
        "added:src/new.ts:function:newName",
        "removed:src/old.ts:function:oldName",
    )


def test_unsupported_or_symbol_free_files_do_not_invent_symbols() -> None:
    assert (
        extract_changed_symbols(
            before={"README.md": "old\n"},
            after={"README.md": "new\n"},
        )
        == ()
    )
