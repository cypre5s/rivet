"""验证 NUL 分隔 Git 状态对 Unicode、换名和换行文件名无损。"""

from rivet.transaction.git_backend import parse_porcelain_paths


def test_porcelain_parser_preserves_unicode_and_newline() -> None:
    raw = " M tracked.txt\0?? 新增.txt\0?? line\nbreak.txt\0".encode()

    paths = parse_porcelain_paths(raw)

    assert paths == ("line\nbreak.txt", "tracked.txt", "新增.txt")


def test_porcelain_parser_keeps_rename_destination_and_source() -> None:
    raw = b"R  renamed.txt\x00original.txt\x00"

    paths = parse_porcelain_paths(raw)

    assert paths == ("original.txt", "renamed.txt")
