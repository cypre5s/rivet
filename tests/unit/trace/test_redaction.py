"""验证事件和文本秘密在持久化前被递归脱敏。"""

from __future__ import annotations

from typing import cast

from rivet.trace.builder import TraceEventBuilder
from rivet.trace.redaction import (
    REDACTED_TEXT,
    SecretRedactor,
    StreamingSecretRedactor,
)
from tests.fixtures.trace.events import make_event


def test_redactor_removes_forbidden_keys_and_token_values() -> None:
    api_key = "sk-" + ("a" * 32)
    bearer = "Bearer " + ("b" * 32)
    redactor = SecretRedactor(environment={"DEEPSEEK_API_KEY": api_key})

    redacted = redactor.redact_payload(
        {
            "api_key": api_key,
            "nested": {
                "authorization": bearer,
                "message": f"request used {api_key} and {bearer}",
            },
        }
    )

    serialized = str(redacted)
    assert api_key not in serialized
    assert bearer not in serialized
    assert REDACTED_TEXT in serialized
    assert "api_key" in cast(list[object], redacted["_redacted_fields"])


def test_redactor_rebuilds_valid_event_without_plaintext() -> None:
    token = "token-value-" + ("c" * 24)
    redactor = SecretRedactor(environment={"SERVICE_TOKEN": token})
    event = make_event(
        1,
        payload={"message": f"provider returned {token}"},
    )

    redacted_event = redactor.redact_event(event)

    assert token not in redacted_event.model_dump_json()
    assert REDACTED_TEXT in cast(str, redacted_event.payload["message"])


def test_builder_preserves_correlation_and_redacts_before_validation() -> None:
    secret = "sk-" + ("f" * 32)
    builder = TraceEventBuilder(
        redactor=SecretRedactor(environment={"SERVICE_KEY": secret}),
        clock=lambda: make_event(1).timestamp,
        event_id_factory=lambda: "event_builder_1",
    )

    event = builder.build(
        event_type="trace.created",
        run_id="run_trace_test",
        session_id="session_trace_test",
        transaction_id="tx_trace_test",
        parent_event_id="event_trace_1",
        payload={"authorization": secret, "message": secret},
    )

    assert event.transaction_id == "tx_trace_test"
    assert event.parent_event_id == "event_trace_1"
    assert secret not in event.model_dump_json()


def test_streaming_redactor_emits_safe_snapshots_without_partial_secret() -> None:
    secret = "sk-" + ("z" * 40)
    streamer = StreamingSecretRedactor(
        SecretRedactor(environment={"DEEPSEEK_API_KEY": secret}),
        min_increment=1,
    )
    snapshots = [
        snapshot
        for snapshot in (
            streamer.feed("这是第一段安全说明。" * 8),
            streamer.feed(f"密钥是 {secret[:18]}"),
            streamer.feed(f"{secret[18:]}。后续结论已经生成。" * 4),
            streamer.finalize(),
        )
        if snapshot is not None
    ]

    assert len(snapshots) >= 2
    assert snapshots[-1].count(REDACTED_TEXT) >= 1
    assert all(secret not in snapshot for snapshot in snapshots)
    assert all(secret[:18] not in snapshot for snapshot in snapshots)
