"""离线核对 Python、TUI 与 OpenTUI 原生组件的许可证证据。"""

from __future__ import annotations

import argparse
import importlib.metadata
import json
import re
import sys
import tomllib
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import cast

from packaging.requirements import Requirement

LICENSE_FILE_PATTERN = re.compile(r"^(?:licen[cs]e|copying|notice)(?:[._-].*)?$", re.I)
PROHIBITED_LICENSE_PARTS = frozenset({"AGPL", "SSPL", "BUSL"})
EXPECTED_NATIVE_LICENSES = frozenset(
    {
        "LICENSE",
        "LICENSE-GHOSTTY",
        "LICENSE-LCMS2",
        "LICENSE-LIBWEBP",
        "LICENSE-STB",
        "LICENSE-WUFFS",
    }
)


@dataclass(frozen=True, slots=True)
class LicenseRecord:
    """保存包版本和不依赖网络的许可证证据。"""

    ecosystem: str
    package: str
    version: str
    declared_license: str | None
    license_files: tuple[str, ...]
    passed: bool


def inspect_licenses(repository: Path) -> dict[str, object]:
    """核对直接依赖、开发依赖、TUI 包和本机 OpenTUI 原生许可证。"""
    root = repository.resolve(strict=True)
    python_records = tuple(
        _python_record(requirement)
        for requirement in _python_requirements(root / "pyproject.toml")
    )
    npm_records = tuple(
        _npm_record(root / "tui" / "node_modules", package)
        for package in _npm_dependencies(root / "tui" / "package.json")
    )
    native_records = _native_records(root / "tui" / "node_modules" / "@opentui")
    records = (*python_records, *npm_records, *native_records)
    notices = (root / "THIRD_PARTY_NOTICES.md").read_text(encoding="utf-8")
    notice_packages = ("@opentui/core", "@opentui/react", "@opentui/keymap", "react")
    notices_complete = all(package in notices for package in notice_packages)
    return {
        "schema_version": 1,
        "passed": bool(records)
        and all(record.passed for record in records)
        and notices_complete,
        "record_count": len(records),
        "notices_complete": notices_complete,
        "records": [asdict(record) for record in records],
        "policy": {
            "network_used": False,
            "prohibited_license_parts": sorted(PROHIBITED_LICENSE_PARTS),
            "scope": "direct Python/TUI dependencies and installed OpenTUI native bundles",
        },
    }


def _python_requirements(path: Path) -> tuple[Requirement, ...]:
    """从项目、可选和开发组收集去重的直接 Python 依赖。"""
    with path.open("rb") as stream:
        document = cast(dict[str, object], tomllib.load(stream))
    project = _mapping(document, "project")
    raw_requirements: list[str] = []
    raw_requirements.extend(_string_list(project, "dependencies"))
    optional = project.get("optional-dependencies", {})
    if not isinstance(optional, dict):
        raise ValueError("optional-dependencies 无效")
    for values in cast(dict[str, object], optional).values():
        raw_requirements.extend(_coerce_string_list(values))
    groups = document.get("dependency-groups", {})
    if not isinstance(groups, dict):
        raise ValueError("dependency-groups 无效")
    for values in cast(dict[str, object], groups).values():
        raw_requirements.extend(_coerce_string_list(values))
    requirements = {
        Requirement(value).name.lower(): Requirement(value)
        for value in raw_requirements
    }
    return tuple(requirements[name] for name in sorted(requirements))


def _python_record(requirement: Requirement) -> LicenseRecord:
    """读取已安装 distribution 元数据和许可证文件名。"""
    try:
        distribution = importlib.metadata.distribution(requirement.name)
    except importlib.metadata.PackageNotFoundError:
        return LicenseRecord("python", requirement.name, "missing", None, (), False)
    metadata = distribution.metadata
    declared = metadata.get("License-Expression") or metadata.get("License")
    if declared is None:
        classifiers = metadata.get_all("Classifier") or []
        license_classifiers = [
            classifier.removeprefix("License :: ")
            for classifier in classifiers
            if classifier.startswith("License :: ")
        ]
        declared = "; ".join(license_classifiers) or None
    license_files = tuple(
        sorted(
            str(path)
            for path in distribution.files or ()
            if LICENSE_FILE_PATTERN.match(Path(str(path)).name)
        )
    )
    passed = _license_evidence_passes(declared, license_files)
    return LicenseRecord(
        "python",
        requirement.name,
        distribution.version,
        declared,
        license_files,
        passed,
    )


def _npm_dependencies(path: Path) -> tuple[str, ...]:
    """读取直接运行和开发 npm 依赖。"""
    raw_document = cast(object, json.loads(path.read_text(encoding="utf-8")))
    if not isinstance(raw_document, dict):
        raise ValueError("TUI package.json 无效")
    document = cast(dict[str, object], raw_document)
    names: set[str] = set()
    for section in ("dependencies", "devDependencies"):
        values = document.get(section, {})
        if not isinstance(values, dict):
            raise ValueError(f"TUI {section} 无效")
        names.update(cast(dict[str, object], values))
    return tuple(sorted(names))


def _npm_record(node_modules: Path, package: str) -> LicenseRecord:
    """核对一个已安装 npm 包的声明和许可证文件。"""
    package_root = node_modules.joinpath(*package.split("/"))
    package_json = package_root / "package.json"
    if not package_json.is_file() or package_json.is_symlink():
        return LicenseRecord("npm", package, "missing", None, (), False)
    raw_document = cast(object, json.loads(package_json.read_text(encoding="utf-8")))
    if not isinstance(raw_document, dict):
        return LicenseRecord("npm", package, "invalid", None, (), False)
    document = cast(dict[str, object], raw_document)
    version = document.get("version")
    declared = document.get("license")
    license_files = tuple(
        sorted(
            path.name
            for path in package_root.iterdir()
            if path.is_file() and LICENSE_FILE_PATTERN.match(path.name)
        )
    )
    return LicenseRecord(
        "npm",
        package,
        version if isinstance(version, str) else "invalid",
        declared if isinstance(declared, str) else None,
        license_files,
        isinstance(version, str)
        and _license_evidence_passes(
            declared if isinstance(declared, str) else None,
            license_files,
        ),
    )


def _native_records(opentui_root: Path) -> tuple[LicenseRecord, ...]:
    """要求每个本机 OpenTUI 二进制包携带完整静态组件许可证。"""
    records: list[LicenseRecord] = []
    for package_root in sorted(opentui_root.glob("core-*-*")):
        if not package_root.is_dir():
            continue
        package_json = package_root / "package.json"
        if not package_json.is_file():
            continue
        raw_document = cast(
            object, json.loads(package_json.read_text(encoding="utf-8"))
        )
        document = (
            cast(dict[str, object], raw_document)
            if isinstance(raw_document, dict)
            else {}
        )
        version = document.get("version")
        declared = document.get("license")
        present = frozenset(
            path.name for path in package_root.iterdir() if path.is_file()
        )
        records.append(
            LicenseRecord(
                "opentui-native",
                f"@opentui/{package_root.name}",
                version if isinstance(version, str) else "invalid",
                declared if isinstance(declared, str) else None,
                tuple(sorted(EXPECTED_NATIVE_LICENSES.intersection(present))),
                isinstance(version, str)
                and EXPECTED_NATIVE_LICENSES.issubset(present)
                and _license_evidence_passes(
                    declared if isinstance(declared, str) else None,
                    tuple(present),
                ),
            )
        )
    return tuple(records)


def _license_evidence_passes(
    declared: str | None,
    license_files: tuple[str, ...] | list[str],
) -> bool:
    """要求声明或随包文件存在，并拒绝明确禁止的许可证族。"""
    evidence = declared or ("license-file" if license_files else "")
    normalized = evidence.upper()
    return bool(evidence) and not any(
        prohibited in normalized for prohibited in PROHIBITED_LICENSE_PARTS
    )


def _mapping(payload: dict[str, object], key: str) -> dict[str, object]:
    """读取必需映射。"""
    value = payload.get(key)
    if not isinstance(value, dict):
        raise ValueError(f"{key} 必须是映射")
    return cast(dict[str, object], value)


def _string_list(payload: dict[str, object], key: str) -> list[str]:
    """读取必需字符串列表。"""
    return _coerce_string_list(payload.get(key, []))


def _coerce_string_list(value: object) -> list[str]:
    """验证并返回字符串列表。"""
    if not isinstance(value, list) or not all(
        isinstance(item, str) for item in cast(list[object], value)
    ):
        raise ValueError("依赖列表无效")
    return cast(list[str], value)


def _build_parser() -> argparse.ArgumentParser:
    """构造仓库和 JSON 输出参数。"""
    parser = argparse.ArgumentParser(description="离线核对第三方许可证证据")
    parser.add_argument("--repository", type=Path, default=Path.cwd())
    parser.add_argument("--json", action="store_true", dest="json_output")
    return parser


def main() -> int:
    """执行许可证证据检查并返回稳定退出码。"""
    arguments = _build_parser().parse_args()
    try:
        result = inspect_licenses(cast(Path, arguments.repository))
    except (OSError, ValueError, json.JSONDecodeError) as error:
        print(f"许可证检查无法完成：{type(error).__name__}", file=sys.stderr)
        return 2
    if cast(bool, arguments.json_output):
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    elif result["passed"]:
        print(f"许可证检查通过：核对 {result['record_count']} 项直接依赖证据")
    else:
        print("许可证检查失败：存在缺失或禁止的许可证证据", file=sys.stderr)
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
