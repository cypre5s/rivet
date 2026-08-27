"""验证 argv 隔离、输出上限和完整进程树回收。"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import pytest

from rivet.kernel.resources import ResourceScope
from rivet.tools.paths import WorkspaceBoundary
from rivet.tools.process import ProcessRunner


@pytest.mark.asyncio
async def test_shell_injection_text_is_only_an_argument(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    transaction = tmp_path / "transaction"
    repository.mkdir()
    transaction.mkdir()
    marker = transaction / "must-not-be-removed"
    marker.write_text("safe", encoding="utf-8")
    scope = ResourceScope("tools.process.argv")
    runner = ProcessRunner(WorkspaceBoundary(repository, transaction), scope=scope)
    injection = "; rm -rf must-not-be-removed"

    result = await runner.run(
        (sys.executable, "-c", "import sys; print(sys.argv[1])", injection),
        timeout_seconds=2,
    )

    assert result.returncode == 0
    assert result.stdout.decode().strip() == injection
    assert marker.read_text(encoding="utf-8") == "safe"
    await scope.close()


@pytest.mark.asyncio
async def test_binary_stdout_is_preserved_without_decode_failure(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repository"
    transaction = tmp_path / "transaction"
    repository.mkdir()
    transaction.mkdir()
    scope = ResourceScope("tools.process.binary")
    runner = ProcessRunner(WorkspaceBoundary(repository, transaction), scope=scope)

    result = await runner.run(
        (sys.executable, "-c", "import os; os.write(1, b'\\xff\\x00ok')"),
        timeout_seconds=2,
    )

    assert result.stdout == b"\xff\x00ok"
    assert result.returncode == 0
    await scope.close()


@pytest.mark.asyncio
async def test_infinite_stdout_is_bounded_and_process_is_reaped(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    transaction = tmp_path / "transaction"
    repository.mkdir()
    transaction.mkdir()
    scope = ResourceScope("tools.process.infinite")
    runner = ProcessRunner(
        WorkspaceBoundary(repository, transaction),
        scope=scope,
        max_capture_bytes=16_384,
        termination_grace_seconds=0.2,
    )

    result = await runner.run(
        (
            sys.executable,
            "-c",
            "import os\nwhile True: os.write(1, b'x' * 4096)",
        ),
        timeout_seconds=0.1,
    )

    assert result.timed_out
    assert result.stdout_truncated
    assert len(result.stdout) == 16_384
    assert result.stdout_total_bytes > len(result.stdout)
    assert result.returncode is not None
    await scope.close()
    scope.assert_empty()


@pytest.mark.asyncio
async def test_timeout_terminates_grandchild_process_group(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    transaction = tmp_path / "transaction"
    repository.mkdir()
    transaction.mkdir()
    scope = ResourceScope("tools.process.tree")
    runner = ProcessRunner(
        WorkspaceBoundary(repository, transaction),
        scope=scope,
        termination_grace_seconds=0.1,
    )
    script = (
        "import subprocess,sys,time\n"
        "child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(30)'])\n"
        "print(child.pid, flush=True)\n"
        "time.sleep(30)"
    )

    result = await runner.run(
        (sys.executable, "-c", script),
        timeout_seconds=0.2,
    )
    child_pid = int(result.stdout.decode().strip())

    assert result.timed_out
    for _ in range(100):
        status_path = Path(f"/proc/{child_pid}/stat")
        if not status_path.exists() or status_path.read_text().split()[2] == "Z":
            break
        await asyncio.sleep(0.01)
    else:
        pytest.fail("grandchild process remained alive")
    await scope.close()


@pytest.mark.asyncio
async def test_sigterm_resistant_process_is_killed_and_waited(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    transaction = tmp_path / "transaction"
    repository.mkdir()
    transaction.mkdir()
    scope = ResourceScope("tools.process.kill")
    runner = ProcessRunner(
        WorkspaceBoundary(repository, transaction),
        scope=scope,
        termination_grace_seconds=0.05,
    )
    script = (
        "import signal,time\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "print('ready', flush=True)\n"
        "time.sleep(30)"
    )

    result = await runner.run(
        (sys.executable, "-c", script),
        timeout_seconds=0.1,
    )

    assert result.timed_out
    assert result.returncode == -9
    await scope.close()


@pytest.mark.asyncio
async def test_cancellation_terminates_spawned_process_tree(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    transaction = tmp_path / "transaction"
    repository.mkdir()
    transaction.mkdir()
    scope = ResourceScope("tools.process.cancel")
    runner = ProcessRunner(
        WorkspaceBoundary(repository, transaction),
        scope=scope,
        termination_grace_seconds=0.1,
    )
    script = (
        "import pathlib,subprocess,sys,time\n"
        "child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(30)'])\n"
        "pathlib.Path('child.pid').write_text(str(child.pid))\n"
        "time.sleep(30)"
    )

    running = asyncio.create_task(
        runner.run((sys.executable, "-c", script), timeout_seconds=30)
    )
    child_pid_path = transaction / "child.pid"
    for _ in range(100):
        if child_pid_path.exists():
            break
        await asyncio.sleep(0.01)
    else:
        running.cancel()
        pytest.fail("child process did not start")
    child_pid = int(child_pid_path.read_text(encoding="utf-8"))
    running.cancel()

    with pytest.raises(asyncio.CancelledError):
        await running
    for _ in range(100):
        status_path = Path(f"/proc/{child_pid}/stat")
        if not status_path.exists() or status_path.read_text().split()[2] == "Z":
            break
        await asyncio.sleep(0.01)
    else:
        pytest.fail("cancelled grandchild process remained alive")
    await scope.close()
    scope.assert_empty()


@pytest.mark.asyncio
async def test_environment_is_whitelisted(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    transaction = tmp_path / "transaction"
    repository.mkdir()
    transaction.mkdir()
    scope = ResourceScope("tools.process.environment")
    runner = ProcessRunner(
        WorkspaceBoundary(repository, transaction),
        scope=scope,
        environment={
            "PATH": os.environ.get("PATH", ""),
            "DEEPSEEK_API_KEY": "test-value",
            "UNSAFE_SECRET": "hidden",
        },
    )

    result = await runner.run(
        (
            sys.executable,
            "-c",
            "import os; print('UNSAFE_SECRET' in os.environ, "
            "'DEEPSEEK_API_KEY' in os.environ)",
        ),
        timeout_seconds=2,
    )

    assert result.stdout.decode().strip() == "False False"
    await scope.close()
