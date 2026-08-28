"""验证公共标识、路径和版本不变量。"""

from datetime import UTC, datetime

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from pydantic import TypeAdapter, ValidationError

from rivet.contracts.common import RepositoryPath, RunId, SourceSpan
from rivet.contracts.messages import UserMessage


@pytest.mark.parametrize(
    "invalid_path",
    [
        "",
        "/etc/passwd",
        "../../etc/passwd",
        "src/../secret.txt",
        "src\\main.py",
        "./src/main.py",
        "src//main.py",
        "src/main.py/",
    ],
)
def test_repository_path_rejects_escape_or_noncanonical_form(
    invalid_path: str,
) -> None:
    with pytest.raises(ValidationError):
        TypeAdapter(RepositoryPath).validate_python(invalid_path)


def test_repository_path_accepts_relative_posix_path() -> None:
    assert TypeAdapter(RepositoryPath).validate_python("src/rivet/cli.py") == (
        "src/rivet/cli.py"
    )


def test_identifier_rejects_wrong_prefix() -> None:
    with pytest.raises(ValidationError):
        TypeAdapter(RunId).validate_python("session_example")


def test_source_span_rejects_reversed_position() -> None:
    with pytest.raises(ValidationError):
        SourceSpan(
            repository_path="src/main.py",
            start_line=9,
            start_column=4,
            end_line=8,
            end_column=2,
        )


def test_contract_rejects_missing_extra_and_unknown_version() -> None:
    valid_payload = {
        "schema_version": 1,
        "role": "user",
        "content": "修复超时",
        "created_at": datetime(2026, 8, 28, tzinfo=UTC),
    }

    with pytest.raises(ValidationError):
        UserMessage.model_validate({"schema_version": 1, "role": "user"})
    with pytest.raises(ValidationError):
        UserMessage.model_validate({**valid_payload, "unexpected": True})
    with pytest.raises(ValidationError):
        UserMessage.model_validate({**valid_payload, "schema_version": 2})
    with pytest.raises(ValidationError):
        UserMessage.model_validate({**valid_payload, "content": 123})


@settings(max_examples=100, derandomize=True, deadline=None)
@given(
    content=st.text(
        alphabet=st.characters(blacklist_categories=("Cs",)),
        min_size=1,
        max_size=200,
    )
)
def test_user_message_json_roundtrip_is_stable_for_100_examples(content: str) -> None:
    message = UserMessage(
        content=content,
        created_at=datetime(2026, 8, 28, tzinfo=UTC),
    )

    restored = UserMessage.model_validate_json(message.model_dump_json())

    assert restored == message
