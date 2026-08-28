"""集中验证事务状态只能沿冻结路径推进。"""

from __future__ import annotations

from rivet.contracts.transactions import TransactionState

from .errors import TransactionError

ALLOWED_TRANSITIONS = {
    TransactionState.CREATED: {
        TransactionState.SNAPSHOTTED,
        TransactionState.BASELINED,
        TransactionState.ABORTED,
    },
    TransactionState.SNAPSHOTTED: {
        TransactionState.BASELINED,
        TransactionState.ABORTED,
    },
    TransactionState.BASELINED: {
        TransactionState.PLANNED,
        TransactionState.ABORTED,
    },
    TransactionState.PLANNED: {
        TransactionState.PATCHING,
        TransactionState.ABORTED,
    },
    TransactionState.PATCHING: {
        TransactionState.PATCHING,
        TransactionState.VERIFYING,
        TransactionState.ABORTED,
    },
    TransactionState.VERIFYING: {
        TransactionState.VERIFIED,
        TransactionState.REJECTED,
        TransactionState.ABORTED,
    },
    TransactionState.VERIFIED: {
        TransactionState.APPLIED,
        TransactionState.ABORTED,
    },
    TransactionState.REJECTED: {
        TransactionState.PATCHING,
        TransactionState.ABORTED,
    },
    TransactionState.APPLIED: {TransactionState.APPLIED},
    TransactionState.ABORTED: {TransactionState.ABORTED},
}


def validate_transition(
    current: TransactionState,
    target: TransactionState,
) -> TransactionState:
    """返回获准目标状态，拒绝跳阶段和重新打开终态。"""
    if target not in ALLOWED_TRANSITIONS[current]:
        raise TransactionError(
            "transaction.state_transition_invalid",
            f"事务状态迁移无效：{current.value} -> {target.value}",
        )
    return target
