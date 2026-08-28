"""用 AST 检查薄内核与契约层的单向依赖边界。"""

from __future__ import annotations

import ast
import sys
from argparse import ArgumentParser
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path

FORBIDDEN_CONTRACT_IMPORT_PREFIXES = (
    "rivet.context",
    "rivet.guard",
    "rivet.ipc",
    "rivet.kernel",
    "rivet.providers",
    "rivet.readers",
    "rivet.storage",
    "rivet.tools",
    "rivet.trace",
    "rivet.transaction",
    "rivet.verify",
)


@dataclass(frozen=True, slots=True)
class ArchitectureViolation:
    """记录稳定规则、相对路径、行号与脱离源码的摘要。"""

    rule_id: str
    path: str
    line: int
    summary: str


def _imported_modules(tree: ast.AST) -> Iterator[tuple[str, int]]:
    """从 AST 中提取绝对 import 根名与行号。"""
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield alias.name, node.lineno
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            yield node.module, node.lineno


def _matches_prefix(module_name: str, prefix: str) -> bool:
    """匹配完整包名或其子模块，避免相似前缀误报。"""
    return module_name == prefix or module_name.startswith(f"{prefix}.")


def find_architecture_violations(
    repository_root: Path,
) -> tuple[ArchitectureViolation, ...]:
    """检查 contracts 是否反向依赖 Kernel 或具体能力层。"""
    contract_root = repository_root / "src" / "rivet" / "contracts"
    if not contract_root.is_dir():
        return ()

    violations: list[ArchitectureViolation] = []
    for source_path in sorted(contract_root.rglob("*.py")):
        relative_path = source_path.relative_to(repository_root).as_posix()
        try:
            tree = ast.parse(
                source_path.read_text(encoding="utf-8"), filename=relative_path
            )
        except (OSError, SyntaxError) as error:
            violations.append(
                ArchitectureViolation(
                    rule_id="source.unreadable",
                    path=relative_path,
                    line=getattr(error, "lineno", 0) or 0,
                    summary="源文件无法解析",
                )
            )
            continue
        for module_name, line in _imported_modules(tree):
            if any(
                _matches_prefix(module_name, prefix)
                for prefix in FORBIDDEN_CONTRACT_IMPORT_PREFIXES
            ):
                violations.append(
                    ArchitectureViolation(
                        rule_id="contracts.reverse_dependency",
                        path=relative_path,
                        line=line,
                        summary=f"contracts 不得导入 {module_name}",
                    )
                )
    return tuple(
        sorted(
            violations,
            key=lambda violation: (
                violation.path,
                violation.line,
                violation.rule_id,
            ),
        )
    )


def _build_parser() -> ArgumentParser:
    """构造可指定仓库根的架构门禁参数。"""
    parser = ArgumentParser(description="检查 Rivet 架构依赖边界")
    parser.add_argument("--repository", type=Path, default=Path.cwd())
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """输出可定位的边界违规并返回 CI 退出码。"""
    arguments = _build_parser().parse_args(argv)
    repository_root = arguments.repository.resolve()
    violations = find_architecture_violations(repository_root)
    if violations:
        print("架构检查失败：发现反向依赖", file=sys.stderr)
        for violation in violations:
            print(
                f"- {violation.path}:{violation.line} "
                f"[{violation.rule_id}] {violation.summary}",
                file=sys.stderr,
            )
        return 1
    print("架构检查通过：未发现边界违规")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
