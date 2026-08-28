"""校验 IPC JSON Schema golden 与手工 TypeScript 镜像是否同步。"""

from __future__ import annotations

import hashlib
import json
import re
import sys
from argparse import ArgumentParser
from collections.abc import Sequence
from pathlib import Path
from typing import cast

from pydantic import BaseModel

from rivet.contracts.ipc import IPC_PROTOCOL_VERSION, IpcEvent, IpcRequest, IpcResponse

IPC_MODELS: tuple[type[BaseModel], ...] = (IpcRequest, IpcResponse, IpcEvent)
SCHEMA_FIXTURE_PATH = Path("tests/fixtures/contracts/ipc_schema_v1.json")
TYPESCRIPT_MIRROR_PATH = Path("tui/src/contracts/ipc.ts")
SCHEMA_HASH_PATTERN = re.compile(r'IPC_SCHEMA_SHA256 = "([0-9a-f]{64})"')
FIELD_MAP_PATTERN = re.compile(r"IPC_FIELD_MAP = (\{.*\}) as const;")


def build_ipc_schema_document() -> dict[str, object]:
    """生成按模型名稳定排序的 IPC Schema 文档。"""
    return {
        "protocol_version": IPC_PROTOCOL_VERSION,
        "models": {
            model.__name__: model.model_json_schema()
            for model in sorted(IPC_MODELS, key=lambda item: item.__name__)
        },
    }


def _canonical_json(payload: object) -> str:
    """生成用于跨语言哈希的规范 JSON。"""
    return json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def ipc_schema_sha256(schema_document: object) -> str:
    """计算不受空白和字段顺序影响的 Schema SHA-256。"""
    return hashlib.sha256(_canonical_json(schema_document).encode("utf-8")).hexdigest()


def _field_map() -> dict[str, list[str]]:
    """提取 TypeScript 镜像必须包含的 IPC 顶层字段。"""
    return {
        model.__name__: list(model.model_fields)
        for model in sorted(IPC_MODELS, key=lambda item: item.__name__)
    }


def _render_typescript(schema_hash: str) -> str:
    """生成手工定义、但由 Schema 哈希和字段图门禁约束的镜像。"""
    field_map = _canonical_json(_field_map())
    return f'''export const IPC_PROTOCOL_VERSION = 1 as const;
export const IPC_SCHEMA_SHA256 = "{schema_hash}";
export const IPC_FIELD_MAP = {field_map} as const;

export type JsonValue =
  | null
  | boolean
  | number
  | string
  | JsonValue[]
  | {{ [key: string]: JsonValue }};

export interface ErrorDetail {{
  schema_version: 1;
  code: string;
  summary: string;
  next_action: string;
  retryable: boolean;
  run_id: string | null;
  session_id: string | null;
  transaction_id: string | null;
  trace_event_id: string | null;
  cause_redacted: string | null;
}}

export interface IpcRequest {{
  schema_version: 1;
  message_type: "request";
  protocol_version: 1;
  request_id: string;
  method: string;
  params: {{ [key: string]: JsonValue }};
}}

export interface IpcResponse {{
  schema_version: 1;
  message_type: "response";
  protocol_version: 1;
  request_id: string;
  ok: boolean;
  result: JsonValue | null;
  error: ErrorDetail | null;
}}

export interface IpcEvent {{
  schema_version: 1;
  message_type: "event";
  protocol_version: 1;
  event_id: string;
  event_type: string;
  sequence: number;
  payload: {{ [key: string]: JsonValue }};
}}

export type IpcMessage = IpcRequest | IpcResponse | IpcEvent;
'''


def write_ipc_contracts(repository_root: Path) -> None:
    """原子性需求由后续阶段实现；本阶段只写入可审查生成件。"""
    schema_document = build_ipc_schema_document()
    schema_path = repository_root / SCHEMA_FIXTURE_PATH
    typescript_path = repository_root / TYPESCRIPT_MIRROR_PATH
    schema_path.parent.mkdir(parents=True, exist_ok=True)
    typescript_path.parent.mkdir(parents=True, exist_ok=True)
    schema_path.write_text(
        f"{json.dumps(schema_document, ensure_ascii=False, indent=2, sort_keys=True)}\n",
        encoding="utf-8",
    )
    typescript_path.write_text(
        _render_typescript(ipc_schema_sha256(schema_document)), encoding="utf-8"
    )


def verify_ipc_contracts(repository_root: Path) -> tuple[str, ...]:
    """比对当前 Python Schema、golden、TypeScript 哈希和顶层字段图。"""
    schema_path = repository_root / SCHEMA_FIXTURE_PATH
    typescript_path = repository_root / TYPESCRIPT_MIRROR_PATH
    if not schema_path.is_file() or not typescript_path.is_file():
        return ("IPC 生成件不完整",)

    expected_schema = build_ipc_schema_document()
    loaded_schema = cast(object, json.loads(schema_path.read_text(encoding="utf-8")))
    violations: list[str] = []
    if loaded_schema != expected_schema:
        violations.append("IPC JSON Schema golden 与 Python 契约不一致")

    typescript_source = typescript_path.read_text(encoding="utf-8")
    hash_match = SCHEMA_HASH_PATTERN.search(typescript_source)
    expected_hash = ipc_schema_sha256(expected_schema)
    if hash_match is None or hash_match.group(1) != expected_hash:
        violations.append("IPC TypeScript 镜像 Schema 哈希不一致")

    field_map_match = FIELD_MAP_PATTERN.search(typescript_source)
    if field_map_match is None:
        violations.append("IPC TypeScript 镜像缺少字段图")
    else:
        loaded_field_map = cast(object, json.loads(field_map_match.group(1)))
        if loaded_field_map != _field_map():
            violations.append("IPC TypeScript 镜像字段图不一致")
    return tuple(violations)


def _build_parser() -> ArgumentParser:
    """构造默认校验、显式生成的 IPC 契约命令。"""
    parser = ArgumentParser(description="校验 IPC Python/TypeScript 契约")
    parser.add_argument("--repository", type=Path, default=Path.cwd())
    parser.add_argument("--write", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """生成或校验 IPC 契约文件。"""
    arguments = _build_parser().parse_args(argv)
    repository_root = arguments.repository.resolve()
    if arguments.write:
        write_ipc_contracts(repository_root)
    violations = verify_ipc_contracts(repository_root)
    if violations:
        for violation in violations:
            print(f"- {violation}", file=sys.stderr)
        return 1
    print("IPC 契约校验通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
