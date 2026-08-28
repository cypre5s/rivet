"""提供 Phase 9 真实 Git 仓库与冻结验收 fixture。"""

from __future__ import annotations

import hashlib
import os
import subprocess
from datetime import UTC, datetime
from pathlib import Path

from rivet.contracts.transactions import AcceptanceSpec, TransactionRecord
from rivet.contracts.verification import (
    Verdict,
    VerificationKind,
    VerificationResult,
    VerificationStatus,
    VerificationStep,
)
from rivet.kernel.resources import ResourceScope
from rivet.transaction.hashing import canonical_json_bytes
from rivet.transaction.manager import TransactionManager
from rivet.verify.evidence import MANDATORY_EVIDENCE_FILES, EvidenceBundleWriter


def run_git(repository: Path, *arguments: str) -> str:
    """以固定身份和参数数组运行一次 fixture Git 命令。"""
    completed = subprocess.run(
        ["git", *arguments],
        cwd=repository,
        check=True,
        capture_output=True,
        env={
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
        },
    )
    return completed.stdout.decode("utf-8", errors="strict")


def initialize_repository(root: Path, *, detached: bool = False) -> Path:
    """创建含两个文本文件和一个二进制文件的确定性仓库。"""
    repository = root / "repository"
    repository.mkdir()
    run_git(repository, "init", "-q", "-b", "main")
    run_git(repository, "config", "user.name", "Fixture")
    run_git(repository, "config", "user.email", "fixture@example.invalid")
    (repository / "tracked.txt").write_text("base\n", encoding="utf-8")
    (repository / "second.txt").write_text("second base\n", encoding="utf-8")
    (repository / "binary.bin").write_bytes(b"\x00BASE\xff")
    run_git(repository, "add", "--", "tracked.txt", "second.txt", "binary.bin")
    run_git(repository, "commit", "-qm", "initial")
    if detached:
        run_git(repository, "checkout", "--detach", "-q")
    return repository


def make_manager(
    repository: Path,
    root: Path,
    scope: ResourceScope,
) -> TransactionManager:
    """使用测试私有 cache/state 根构造无副作用事务管理器。"""
    return TransactionManager(
        repository,
        scope=scope,
        cache_root=root / "cache" / "rivet",
        state_root=root / "state" / "transactions",
        clock=lambda: datetime(2026, 8, 28, tzinfo=UTC),
    )


def acceptance_spec(
    *,
    acceptance_id: str = "acceptance_fixture",
    expected_behavior: str = "事务修改可安全应用",
) -> AcceptanceSpec:
    """返回覆盖 fixture 文件的最小冻结验收条件。"""
    return AcceptanceSpec(
        acceptance_id=acceptance_id,
        user_goal="在隔离 Worktree 中修改 fixture",
        baseline_reproduction=(("git", "status", "--short"),),
        allowed_paths=("tracked.txt", "second.txt", "binary.bin", "新增.txt"),
        forbidden_paths=("forbidden.txt",),
        expected_behaviors=(expected_behavior,),
        preserved_behaviors=("主工作区在验证前保持不变",),
        verification_commands=(("git", "diff", "--check"),),
        behavior_verification_commands=(("git", "diff", "--check"),),
        max_wall_seconds=120,
        max_tokens=2_000,
        max_tool_calls=20,
        acceptable_risks=("仅修改 fixture",),
        non_goals=("不访问网络",),
    )


def passed_verdict(
    record: TransactionRecord,
    manager: TransactionManager,
) -> Verdict:
    """为事务层集成测试构造带最小完整证据的通过 Verdict。"""
    if record.acceptance_sha256 is None:
        raise AssertionError("事务尚未冻结 AcceptanceSpec")
    attempt_name = EvidenceBundleWriter(
        manager.evidence_root(record.transaction_id)
    ).next_attempt_name()
    evidence_id = f"evidence_fixture_{hashlib.sha256(record.transaction_id.encode()).hexdigest()[:16]}"
    step = VerificationStep(
        step_id="verification_fixture_attestation",
        kind=VerificationKind.RESOURCE,
        name="事务层 fixture 证据",
        required=True,
        command=("rivet-internal", "fixture-attestation"),
        timeout_seconds=1,
    )
    result = VerificationResult(
        step=step,
        status=VerificationStatus.PASSED,
        exit_code=0,
        duration_ms=0,
    )
    verdict = Verdict(
        transaction_id=record.transaction_id,
        acceptance_sha256=record.acceptance_sha256,
        evidence_id=evidence_id,
        evidence_manifest_path=f"evidence/{attempt_name}/manifest.json",
        status=VerificationStatus.PASSED,
        passed=True,
        results=(result,),
        decided_at=datetime(2026, 8, 28, tzinfo=UTC),
    )
    payloads = {name: f"fixture:{name}\n".encode() for name in MANDATORY_EVIDENCE_FILES}
    payloads["verdict.json"] = (
        canonical_json_bytes(verdict.model_dump(mode="json")) + b"\n"
    )
    if record.current_patch_id is not None:
        payloads["patch.diff"] = manager.patch_path(
            record.transaction_id,
            record.current_patch_id,
        ).read_bytes()
    EvidenceBundleWriter(manager.evidence_root(record.transaction_id)).write(
        transaction_id=record.transaction_id,
        acceptance_sha256=record.acceptance_sha256,
        files=payloads,
        evidence_id=evidence_id,
        attempt_name=attempt_name,
    )
    return verdict


def worktree_digest(repository: Path) -> str:
    """计算排除 Git 内部目录后的工作树字节、链接和模式摘要。"""
    digest = hashlib.sha256()
    for path in sorted(repository.rglob("*")):
        relative = path.relative_to(repository)
        if relative.parts[0] in {".git", ".rivet"}:
            continue
        if path.is_dir() and not path.is_symlink():
            continue
        digest.update(relative.as_posix().encode("utf-8"))
        digest.update((path.lstat().st_mode & 0o777).to_bytes(2, "big"))
        if path.is_symlink():
            digest.update(os.readlink(path).encode("utf-8"))
        else:
            digest.update(path.read_bytes())
    return digest.hexdigest()
