"""扫描补丁新增内容中的秘密与高风险执行原语。"""

from __future__ import annotations

import hashlib
import re

from pydantic import Field

from rivet.contracts.common import ContractModel, SummaryText
from rivet.contracts.verification import VerificationStatus

SECRET_PATTERNS = (
    ("provider_key", re.compile(rb"\bsk-[A-Za-z0-9_-]{20,}\b")),
    ("github_token", re.compile(rb"\bghp_[A-Za-z0-9]{20,}\b")),
    (
        "github_fine_grained_token",
        re.compile(rb"\bgithub_pat_[A-Za-z0-9_]{20,}\b"),
    ),
    ("aws_access_key", re.compile(rb"\bAKIA[0-9A-Z]{16}\b")),
    (
        "authorization_header",
        re.compile(
            rb"(?i)\bauthorization\s*[:=]\s*(?:bearer\s+)?[A-Za-z0-9._~+/=-]{16,}"
        ),
    ),
    ("bearer_token", re.compile(rb"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{20,}")),
    (
        "assigned_secret",
        re.compile(
            rb"(?i)\b(?:api[_-]?key|access[_-]?token|secret[_-]?key|password)"
            rb"\s*[:=]\s*[\"']?[A-Za-z0-9._~+/=-]{16,}"
        ),
    ),
    (
        "private_key",
        re.compile(rb"-----BEGIN (?:[A-Z0-9 ]+ )?PRIVATE KEY-----"),
    ),
)
DANGEROUS_PATTERNS = (
    ("shell_execution", re.compile(rb"\bshell\s*=\s*True\b")),
    ("shell_execution", re.compile(rb"\bcreate_subprocess_shell\s*\(")),
    ("shell_execution", re.compile(rb"\bos\.system\s*\(")),
    ("dynamic_execution", re.compile(rb"(?m)^\s*(?:eval|exec)\s*\(")),
)


class SecurityFinding(ContractModel):
    """只保存规则和安全位置，不保存命中内容。"""

    rule_id: str = Field(pattern=r"^[a-z][a-z0-9_]{1,63}$")
    location: SummaryText


class SecurityScanReport(ContractModel):
    """汇总新增内容扫描状态和脱敏命中。"""

    status: VerificationStatus
    findings: tuple[SecurityFinding, ...] = ()
    scanned_files: int = Field(ge=0)
    scanned_bytes: int = Field(ge=0)


def _safe_location(location: str) -> str:
    """凭据若出现在文件名中则只返回短哈希。"""
    encoded = location.encode("utf-8", errors="surrogateescape")
    if any(pattern.search(encoded) for _, pattern in SECRET_PATTERNS):
        digest = hashlib.sha256(encoded).hexdigest()
        return f"redacted-path:{digest[:16]}"
    return location[:4_096]


def scan_added_content(
    files: dict[str, bytes],
    *,
    max_total_bytes: int = 32 * 1024 * 1024,
) -> SecurityScanReport:
    """有界扫描当前新增内容，超限时返回 INCONCLUSIVE。"""
    if max_total_bytes <= 0:
        raise ValueError("扫描字节上限必须大于零")
    total_bytes = 0
    findings: set[tuple[str, str]] = set()
    for location in sorted(files):
        content = files[location]
        total_bytes += len(content)
        if total_bytes > max_total_bytes:
            limit_finding = SecurityFinding(
                rule_id="scan_limit",
                location="changed-content",
            )
            return SecurityScanReport(
                status=VerificationStatus.INCONCLUSIVE,
                findings=(limit_finding,),
                scanned_files=len(files),
                scanned_bytes=total_bytes,
            )
        safe_location = _safe_location(location)
        for rule_id, pattern in (*SECRET_PATTERNS, *DANGEROUS_PATTERNS):
            if pattern.search(content) is not None:
                findings.add((rule_id, safe_location))
    finding_models = tuple(
        SecurityFinding(rule_id=rule_id, location=location)
        for rule_id, location in sorted(findings)
    )
    return SecurityScanReport(
        status=(
            VerificationStatus.FAILED if finding_models else VerificationStatus.PASSED
        ),
        findings=finding_models,
        scanned_files=len(files),
        scanned_bytes=total_bytes,
    )


def contains_secret(content: bytes) -> bool:
    """判断证据载荷是否含任一凭据模式。"""
    return any(pattern.search(content) is not None for _, pattern in SECRET_PATTERNS)
