"""验证 CLI 在 Lease 失败时仍关闭 Kernel 与 Trace。"""

from __future__ import annotations

from typing import cast

import pytest

from rivet.cli.runtime import CliRuntime, close_cli_runtime
from rivet.kernel.module_runtime import CapabilityLease


class _ObservedLease:
    def __init__(
        self,
        name: str,
        events: list[str],
        *,
        error: BaseException | None = None,
    ) -> None:
        self._name = name
        self._events = events
        self._error = error

    async def release(self) -> None:
        self._events.append(f"release:{self._name}")
        if self._error is not None:
            raise self._error


class _ObservedRuntime:
    def __init__(
        self,
        events: list[str],
        *,
        error: BaseException | None = None,
    ) -> None:
        self._events = events
        self._error = error

    async def close(self) -> None:
        self._events.append("runtime.close")
        if self._error is not None:
            raise self._error


@pytest.mark.asyncio
async def test_close_runtime_continues_after_release_failure_and_preserves_first_error() -> (
    None
):
    events: list[str] = []
    release_error = RuntimeError("first release failure")
    close_error = RuntimeError("later runtime close failure")
    leases = cast(
        tuple[CapabilityLease[object], ...],
        (
            _ObservedLease("first", events),
            _ObservedLease("last", events, error=release_error),
        ),
    )
    runtime = cast(CliRuntime, _ObservedRuntime(events, error=close_error))

    with pytest.raises(RuntimeError) as captured:
        await close_cli_runtime(runtime, leases)

    assert captured.value is release_error
    assert events == ["release:last", "release:first", "runtime.close"]
