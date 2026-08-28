"""验证秘密和危险新增内容只产生脱敏结构化命中。"""

from rivet.contracts.verification import VerificationStatus
from rivet.verify.security import scan_added_content


def test_secret_scan_never_returns_matched_value() -> None:
    secret = "sk-" + ("z" * 32)

    report = scan_added_content(
        {"src/config.py": f'token = "{secret}"\n'.encode()},
    )

    assert report.status is VerificationStatus.FAILED
    assert {finding.rule_id for finding in report.findings} == {"provider_key"}
    assert secret not in report.model_dump_json()


def test_dangerous_shell_primitive_fails_security_step() -> None:
    report = scan_added_content(
        {"src/run.py": b"subprocess.run(command, shell=True)\n"},
    )

    assert report.status is VerificationStatus.FAILED
    assert report.findings[0].rule_id == "shell_execution"


def test_oversized_changed_content_is_inconclusive() -> None:
    report = scan_added_content(
        {"large.bin": b"x" * 33},
        max_total_bytes=32,
    )

    assert report.status is VerificationStatus.INCONCLUSIVE
    assert report.findings[0].rule_id == "scan_limit"
