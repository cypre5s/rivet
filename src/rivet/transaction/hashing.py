"""提供 AcceptanceSpec 与事务事实的规范 JSON 哈希。"""

from __future__ import annotations

import hashlib
import json

from rivet.contracts.transactions import AcceptanceSpec


def canonical_json_bytes(payload: object) -> bytes:
    """以 UTF-8、稳定字段顺序和最小分隔符序列化 JSON。"""
    return json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def sha256_digest(content: bytes) -> str:
    """返回公共契约使用的带算法前缀摘要。"""
    return f"sha256:{hashlib.sha256(content).hexdigest()}"


def acceptance_sha256(specification: AcceptanceSpec) -> str:
    """计算包含 schema_version 的冻结 AcceptanceSpec 哈希。"""
    return sha256_digest(canonical_json_bytes(specification.model_dump(mode="json")))
