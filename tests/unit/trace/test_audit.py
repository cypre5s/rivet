"""验证 Demand Traceability=100% 与 Orphan Activation=0 审计器。"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import JsonValue

from rivet.contracts.events import TraceEventEnvelope
from rivet.trace.audit import audit_demand_trace
from rivet.trace.models import PersistedTraceEvent, serialize_persisted_event
from scripts.audit_trace import main as audit_trace_main


def _record(
    sequence: int,
    *,
    event_id: str,
    event_type: str,
    payload: dict[str, JsonValue],
    parent_event_id: str | None = None,
) -> PersistedTraceEvent:
    return PersistedTraceEvent(
        sequence=sequence,
        event=TraceEventEnvelope(
            event_id=event_id,
            event_type=event_type,
            timestamp=datetime(2026, 9, 2, tzinfo=UTC),
            run_id="run_audit",
            session_id="session_audit",
            parent_event_id=parent_event_id,
            payload=payload,
        ),
    )


def _valid_trace() -> tuple[PersistedTraceEvent, ...]:
    return (
        _record(
            1,
            event_id="event_root",
            event_type="demand.created",
            payload={
                "capability_id": "task.fix",
                "demand_id": "demand_root",
                "demand_source": "USER_EXPLICIT",
                "operation_id": None,
                "parent_demand_id": None,
                "reason": "用户请求",
            },
        ),
        _record(
            2,
            event_id="event_required",
            event_type="demand.created",
            parent_event_id="event_root",
            payload={
                "capability_id": "transaction.worktree",
                "demand_id": "demand_required",
                "demand_source": "KERNEL_REQUIRED",
                "operation_id": "fix:tx",
                "parent_demand_id": "demand_root",
                "reason": "FIX 需要隔离事务",
            },
        ),
        _record(
            3,
            event_id="event_activation",
            event_type="module.activated",
            parent_event_id="event_required",
            payload={
                "demand_id": "demand_required",
                "demand_sequence": 2,
                "demand_source": "KERNEL_REQUIRED",
                "module_id": "transaction.git",
                "requested_capability_id": "transaction.worktree",
            },
        ),
    )


def test_valid_runtime_trace_proves_both_ci_invariants() -> None:
    report = audit_demand_trace(_valid_trace())

    assert report.passed
    assert report.orphan_activation_count == 0
    assert report.demand_traceability_percent == 100.0
    assert report.traceable_activation_count == report.activation_count == 1


def test_orphan_activation_is_counted_and_fails_the_gate() -> None:
    records = list(_valid_trace())
    activation = records[-1]
    records[-1] = activation.model_copy(
        update={
            "event": activation.event.model_copy(
                update={"parent_event_id": "event_root"}
            )
        }
    )

    report = audit_demand_trace(tuple(records))

    assert not report.passed
    assert report.orphan_activation_count == 1
    assert report.demand_traceability_percent == 0.0
    assert "activation.orphan" in report.violations


def test_user_root_cannot_directly_authorize_module_activation() -> None:
    records = list(_valid_trace())
    activation = records[-1]
    records[-1] = activation.model_copy(
        update={
            "event": activation.event.model_copy(
                update={
                    "parent_event_id": "event_root",
                    "payload": {
                        "demand_id": "demand_root",
                        "demand_sequence": 1,
                        "demand_source": "USER_EXPLICIT",
                        "module_id": "transaction.git",
                        "requested_capability_id": "task.fix",
                    },
                }
            )
        }
    )

    report = audit_demand_trace(tuple(records))

    assert not report.passed
    assert report.orphan_activation_count == 1
    assert "activation.orphan" in report.violations


def test_activation_capability_must_match_kernel_required_demand() -> None:
    records = list(_valid_trace())
    activation = records[-1]
    records[-1] = activation.model_copy(
        update={
            "event": activation.event.model_copy(
                update={
                    "payload": {
                        **activation.event.payload,
                        "requested_capability_id": "guard.local_execution",
                    }
                }
            )
        }
    )

    report = audit_demand_trace(tuple(records))

    assert not report.passed
    assert report.orphan_activation_count == 1
    assert "activation.orphan" in report.violations


def test_kernel_required_cannot_become_a_root_demand() -> None:
    root = _valid_trace()[1].model_copy(
        update={
            "sequence": 1,
            "event": _valid_trace()[1].event.model_copy(
                update={"parent_event_id": None}
            ),
        }
    )

    report = audit_demand_trace((root,))

    assert not report.passed
    assert report.valid_demand_count == 0
    assert "demand.parent_invalid" in report.violations


def test_model_tool_call_requires_non_empty_operation_id() -> None:
    root, child, *_ = _valid_trace()
    forged_child = child.model_copy(
        update={
            "event": child.event.model_copy(
                update={
                    "payload": {
                        **child.event.payload,
                        "demand_source": "MODEL_TOOL_CALL",
                        "operation_id": "",
                    }
                }
            )
        }
    )

    report = audit_demand_trace((root, forged_child))

    assert not report.passed
    assert report.valid_demand_count == 1
    assert "demand.operation_id_missing" in report.violations


def test_demand_requires_non_empty_canonical_reason() -> None:
    root, *_ = _valid_trace()
    forged_root = root.model_copy(
        update={
            "event": root.event.model_copy(
                update={"payload": {**root.event.payload, "reason": "  "}}
            )
        }
    )

    report = audit_demand_trace((forged_root,))

    assert not report.passed
    assert report.valid_demand_count == 0
    assert "demand.reason_invalid" in report.violations


def test_offline_audit_cli_reports_exact_ci_metrics(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    events_path = tmp_path / "events.ndjson"
    events_path.write_bytes(
        b"".join(serialize_persisted_event(record) for record in _valid_trace())
    )

    assert audit_trace_main((str(events_path),)) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload == {
        "activation_count": 1,
        "demand_count": 2,
        "demand_traceability_percent": 100.0,
        "orphan_activation_count": 0,
        "passed": True,
        "violations": [],
    }
