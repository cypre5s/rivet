"""验证持久化 JSON、Schema 和 IPC 镜像稳定。"""

import json
from pathlib import Path
from typing import cast

from pydantic import BaseModel

from rivet.contracts import CONTRACT_MODELS
from rivet.contracts.events import TraceEventEnvelope
from rivet.contracts.ipc import IpcRequest
from rivet.contracts.messages import SystemMessage
from scripts.verify_ipc_contracts import verify_ipc_contracts

GOLDEN_FIXTURE_PATH = Path("tests/fixtures/contracts/golden_v1.json")


def test_all_contract_models_generate_json_schema() -> None:
    for contract_model in CONTRACT_MODELS:
        schema = contract_model.model_json_schema()

        assert schema["type"] == "object"
        assert schema.get("additionalProperties") is False


def test_golden_json_roundtrips_without_semantic_change() -> None:
    fixture = cast(
        dict[str, object], json.loads(GOLDEN_FIXTURE_PATH.read_text(encoding="utf-8"))
    )
    model_types: dict[str, type[BaseModel]] = {
        "system_message": SystemMessage,
        "trace_event": TraceEventEnvelope,
        "ipc_request": IpcRequest,
    }

    for fixture_name, model_type in model_types.items():
        payload = fixture[fixture_name]
        model = model_type.model_validate_json(json.dumps(payload, ensure_ascii=False))
        restored = model_type.model_validate_json(model.model_dump_json())

        assert restored == model


def test_ipc_typescript_mirror_matches_python_schema() -> None:
    assert verify_ipc_contracts(Path.cwd()) == ()
