"""扫描工作树、暂存区和 Git 历史中的常见凭据。"""

from __future__ import annotations

import hashlib
import os
import re
import subprocess
import sys
from argparse import ArgumentParser
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath


@dataclass(frozen=True, slots=True)
class SecretRule:
    """定义一个只输出规则名、不输出命中内容的扫描规则。"""

    rule_id: str
    pattern: re.Pattern[str]


@dataclass(frozen=True, slots=True)
class SecretFinding:
    """表示经过脱敏的凭据命中。"""

    location: str
    rule_id: str


SECRET_RULES = (
    SecretRule("provider_key", re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b")),
    SecretRule("github_token", re.compile(r"\bghp_[A-Za-z0-9]{20,}\b")),
    SecretRule(
        "github_fine_grained_token",
        re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b"),
    ),
    SecretRule("aws_access_key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    SecretRule(
        "authorization_header",
        re.compile(
            r"(?i)\bauthorization\s*[:=]\s*(?:bearer\s+)?[A-Za-z0-9._~+/=-]{16,}"
        ),
    ),
    SecretRule(
        "bearer_token",
        re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{20,}"),
    ),
    SecretRule(
        "assigned_secret",
        re.compile(
            r"(?i)\b(?:api[_-]?key|access[_-]?token|secret[_-]?key|password)"
            r"\s*[:=]\s*[\"']?[A-Za-z0-9._~+/=-]{16,}"
        ),
    ),
    SecretRule(
        "private_key",
        re.compile(r"-----BEGIN (?:[A-Z0-9 ]+ )?PRIVATE KEY-----"),
    ),
)
SENSITIVE_FILENAMES = frozenset(
    {
        "credentials.json",
        "id_dsa",
        "id_ecdsa",
        "id_ed25519",
        "id_rsa",
        "service-account.json",
    }
)


class GitInspectionError(RuntimeError):
    """表示 Git 对象无法在不暴露原始输出的前提下读取。"""


def _run_git(repository_root: Path, arguments: Sequence[str]) -> bytes:
    """用参数数组读取 Git 数据，错误时不回显可疑内容。"""
    completed = subprocess.run(
        ["git", *arguments],
        cwd=repository_root,
        check=False,
        capture_output=True,
    )
    if completed.returncode != 0:
        raise GitInspectionError("凭据扫描时 Git 读取失败")
    return completed.stdout


def _safe_location(location: str) -> str:
    """防止恶意文件名本身携带凭据并被终端回显。"""
    if any(rule.pattern.search(location) for rule in SECRET_RULES):
        digest = hashlib.sha256(location.encode("utf-8", "surrogateescape")).hexdigest()
        return f"<redacted-path:{digest[:12]}>"
    return location


def _is_sensitive_path(relative_path: str) -> bool:
    """识别不应进入工作树或 Git 对象的凭据文件名。"""
    path = PurePosixPath(relative_path)
    filename = path.name.lower()
    if filename == ".env.example":
        return False
    if filename == ".env" or filename.startswith(".env."):
        return True
    return filename in SENSITIVE_FILENAMES


def scan_bytes(content: bytes, location: str) -> tuple[SecretFinding, ...]:
    """在不返回命中文本的前提下扫描一段内容。"""
    text = content.decode("utf-8", errors="ignore")
    safe_location = _safe_location(location)
    findings = {
        SecretFinding(location=safe_location, rule_id=rule.rule_id)
        for rule in SECRET_RULES
        if rule.pattern.search(text) is not None
    }
    return tuple(sorted(findings, key=lambda finding: finding.rule_id))


def _worktree_entries(repository_root: Path) -> Iterator[tuple[str, bytes]]:
    """迭代已跟踪和未忽略的未跟踪文件，不跟随符号链接。"""
    raw_paths = _run_git(
        repository_root,
        ["ls-files", "-z", "--cached", "--others", "--exclude-standard"],
    )
    for raw_path in raw_paths.split(b"\0"):
        if not raw_path:
            continue
        relative_path = os.fsdecode(raw_path)
        absolute_path = repository_root / relative_path
        try:
            if absolute_path.is_symlink():
                content = os.fsencode(os.readlink(absolute_path))
            elif absolute_path.is_file():
                content = absolute_path.read_bytes()
            else:
                continue
        except OSError as error:
            raise GitInspectionError("凭据扫描时工作树读取失败") from error
        yield relative_path, content


def _staged_entries(repository_root: Path) -> Iterator[tuple[str, bytes]]:
    """迭代暂存区内将被提交的文件内容。"""
    raw_paths = _run_git(
        repository_root,
        ["diff", "--cached", "--name-only", "--diff-filter=ACMR", "-z"],
    )
    for raw_path in raw_paths.split(b"\0"):
        if not raw_path:
            continue
        relative_path = os.fsdecode(raw_path)
        content = _run_git(repository_root, ["show", f":{relative_path}"])
        yield relative_path, content


def _history_entries(repository_root: Path) -> Iterator[tuple[str, bytes]]:
    """去重遍历全部历史 blob，不输出对象原文。"""
    raw_commits = _run_git(repository_root, ["rev-list", "--all"])
    seen_blobs: set[str] = set()
    for raw_commit in raw_commits.splitlines():
        commit = raw_commit.decode("ascii")
        tree_entries = _run_git(
            repository_root, ["ls-tree", "-r", "-z", "--full-tree", commit]
        )
        for raw_entry in tree_entries.split(b"\0"):
            if not raw_entry:
                continue
            metadata, raw_path = raw_entry.split(b"\t", maxsplit=1)
            metadata_parts = metadata.split()
            if len(metadata_parts) != 3 or metadata_parts[1] != b"blob":
                continue
            blob_id = metadata_parts[2].decode("ascii")
            if blob_id in seen_blobs:
                continue
            seen_blobs.add(blob_id)
            relative_path = os.fsdecode(raw_path)
            content = _run_git(repository_root, ["cat-file", "blob", blob_id])
            yield f"history:{relative_path}@{blob_id[:12]}", content


def scan_entries(entries: Iterable[tuple[str, bytes]]) -> tuple[SecretFinding, ...]:
    """扫描一组文件或 Git 对象并去重。"""
    findings: set[SecretFinding] = set()
    for location, content in entries:
        path_for_policy = location.removeprefix("history:").split("@", maxsplit=1)[0]
        if _is_sensitive_path(path_for_policy):
            findings.add(
                SecretFinding(
                    location=_safe_location(location), rule_id="sensitive_filename"
                )
            )
        findings.update(scan_bytes(content, location))
    return tuple(
        sorted(findings, key=lambda finding: (finding.location, finding.rule_id))
    )


def find_repository_root(start_path: Path) -> Path:
    """解析 Git 根目录，不依赖调用者的当前路径格式。"""
    raw_root = _run_git(start_path.resolve(), ["rev-parse", "--show-toplevel"])
    return Path(os.fsdecode(raw_root).strip()).resolve()


def _build_parser() -> ArgumentParser:
    """构造可覆盖默认工作树与暂存区范围的参数。"""
    parser = ArgumentParser(description="扫描常见凭据且不回显命中内容")
    parser.add_argument("--worktree", action="store_true")
    parser.add_argument("--staged", action="store_true")
    parser.add_argument("--history", action="store_true")
    parser.add_argument("--repository", type=Path, default=Path.cwd())
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """运行选定范围的扫描并输出脱敏结果。"""
    arguments = _build_parser().parse_args(argv)
    default_scope = not (arguments.worktree or arguments.staged or arguments.history)

    try:
        repository_root = find_repository_root(arguments.repository)
        entries: list[tuple[str, bytes]] = []
        if arguments.worktree or default_scope:
            entries.extend(_worktree_entries(repository_root))
        if arguments.staged or default_scope:
            entries.extend(_staged_entries(repository_root))
        if arguments.history:
            entries.extend(_history_entries(repository_root))
        findings = scan_entries(entries)
    except (GitInspectionError, OSError) as error:
        print(str(error), file=sys.stderr)
        return 2

    if findings:
        print("凭据扫描失败：发现可疑文件或内容", file=sys.stderr)
        for finding in findings:
            print(f"- {finding.location}: {finding.rule_id}", file=sys.stderr)
        return 1

    print("凭据扫描通过：未发现已知凭据模式")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
