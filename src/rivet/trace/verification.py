"""把独立 Verify 的开始、结论与失败写入同一 NDJSON 因果链。"""

from __future__ import annotations

from rivet.contracts.verification import Verdict
from rivet.trace.builder import TraceEventBuilder
from rivet.trace.store import TraceStore


class VerificationTraceJournal:
    """只记录有界验证事实；命令输出留在 Evidence 文件中。"""

    def __init__(
        self,
        trace: TraceStore,
        *,
        builder: TraceEventBuilder | None = None,
    ) -> None:
        self._trace = trace
        self._builder = builder or TraceEventBuilder()

    async def started(
        self,
        *,
        run_id: str,
        session_id: str,
        transaction_id: str,
        parent_event_id: str,
    ) -> str:
        """在任何验证命令执行前持久化开始事实。"""
        event = self._builder.build(
            event_type="verification.started",
            run_id=run_id,
            session_id=session_id,
            transaction_id=transaction_id,
            parent_event_id=parent_event_id,
            input_summary="独立验证已开始",
            payload={"transaction_id": transaction_id},
        )
        await self._trace.emit(event)
        return event.event_id

    async def completed(
        self,
        *,
        run_id: str,
        session_id: str,
        transaction_id: str,
        parent_event_id: str,
        verdict: Verdict,
        manifest_sha256: str,
    ) -> None:
        """记录程序化 Verdict 和三重绑定，不复制验证日志。"""
        await self._trace.emit(
            self._builder.build(
                event_type="verification.completed",
                run_id=run_id,
                session_id=session_id,
                transaction_id=transaction_id,
                parent_event_id=parent_event_id,
                result_summary=f"独立验证 {verdict.status.value}",
                payload={
                    "acceptance_sha256": verdict.acceptance_sha256,
                    "base_commit": verdict.base_commit,
                    "evidence_id": verdict.evidence_id,
                    "manifest_sha256": manifest_sha256,
                    "passed": verdict.passed,
                    "patch_sha256": verdict.patch_sha256,
                    "results": [
                        {
                            "kind": result.step.kind.value,
                            "status": result.status.value,
                            "step_id": result.step.step_id,
                        }
                        for result in verdict.results
                    ],
                    "status": verdict.status.value,
                    "transaction_id": transaction_id,
                },
            )
        )

    async def failed(
        self,
        *,
        run_id: str,
        session_id: str,
        transaction_id: str,
        parent_event_id: str,
        error: BaseException,
    ) -> None:
        """记录失败类型而不把异常文本或命令输出写入 Trace。"""
        await self._trace.emit(
            self._builder.build(
                event_type="verification.failed",
                run_id=run_id,
                session_id=session_id,
                transaction_id=transaction_id,
                parent_event_id=parent_event_id,
                result_summary="独立验证失败",
                payload={
                    "error_type": type(error).__name__,
                    "transaction_id": transaction_id,
                },
            )
        )
