"""验证 bubblewrap 的真实文件、进程、网络和环境隔离。"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from rivet.guard.sandbox import BubblewrapSandbox, SandboxError
from rivet.kernel.resources import ResourceScope
from rivet.tools.paths import WorkspaceBoundary


def _bwrap_path() -> Path | None:
    """优先使用验收显式提供的临时二进制。"""
    configured = os.environ.get("RIVET_BWRAP_PATH")
    discovered = configured or shutil.which("bwrap")
    return Path(discovered).resolve() if discovered else None


@pytest.mark.asyncio
async def test_missing_bubblewrap_fails_closed_without_bare_execution(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repository"
    transaction = tmp_path / "transaction"
    repository.mkdir()
    transaction.mkdir()
    marker = transaction / "must-not-exist"
    scope = ResourceScope("guard.sandbox.missing")
    sandbox = BubblewrapSandbox(
        WorkspaceBoundary(repository, transaction),
        scope=scope,
        executable=tmp_path / "missing-bwrap",
    )

    with pytest.raises(SandboxError) as captured:
        await sandbox.run(
            ("/usr/bin/python3", "-c", f"open({str(marker)!r}, 'w').close()"),
            timeout_seconds=2,
        )

    assert captured.value.code == "sandbox.unavailable"
    assert not marker.exists()
    await scope.close()


@pytest.mark.asyncio
async def test_bubblewrap_setup_failure_is_not_treated_as_child_failure(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repository"
    transaction = tmp_path / "transaction"
    repository.mkdir()
    transaction.mkdir()
    fake_bwrap = tmp_path / "bwrap"
    fake_bwrap.write_text(
        "#!/bin/sh\nprintf 'bwrap: namespace setup failed\\n' >&2\nexit 1\n",
        encoding="utf-8",
    )
    fake_bwrap.chmod(0o700)
    scope = ResourceScope("guard.sandbox.setup")
    sandbox = BubblewrapSandbox(
        WorkspaceBoundary(repository, transaction),
        scope=scope,
        executable=fake_bwrap,
    )

    with pytest.raises(SandboxError) as captured:
        await sandbox.run(("/usr/bin/true",), timeout_seconds=2)

    assert captured.value.code == "sandbox.setup_failed"
    await scope.close()


@pytest.mark.asyncio
async def test_bubblewrap_blocks_host_home_main_repo_network_and_descendants(
    tmp_path: Path,
) -> None:
    executable = _bwrap_path()
    if executable is None:
        pytest.skip("主机未安装 bubblewrap；完整验收会提供临时解包二进制")
    repository = tmp_path / "repository"
    transaction = tmp_path / "transaction"
    host_home = tmp_path / "host-home"
    repository.mkdir()
    transaction.mkdir()
    host_home.mkdir()
    main_file = repository / "main.txt"
    main_file.write_text("main\n", encoding="utf-8")
    secret = host_home / "secret.txt"
    secret.write_text("not-readable\n", encoding="utf-8")
    host_tmp_marker = Path("/tmp") / f"rivet-{tmp_path.name}-host-marker"
    host_tmp_marker.write_text("host\n", encoding="utf-8")
    scope = ResourceScope("guard.sandbox.real")
    violations: list[str] = []
    sandbox = BubblewrapSandbox(
        WorkspaceBoundary(repository, transaction),
        scope=scope,
        executable=executable,
        violation_sink=lambda violation: violations.append(violation.code),
    )
    script = (
        "import os,socket,subprocess,sys\n"
        "from pathlib import Path\n"
        "results=[]\n"
        "Path('created.txt').write_text('ok')\n"
        f"\ntry: Path({str(main_file)!r}).write_text('changed')\n"
        "except OSError: results.append('main-blocked')\n"
        f"\ntry: Path({str(secret)!r}).read_text()\n"
        "except OSError: results.append('home-blocked')\n"
        f"Path({str(host_tmp_marker)!r}).write_text('sandbox')\n"
        "results.append('tmp-private')\n"
        "\ntry: socket.create_connection(('127.0.0.1', 9), timeout=0.2)\n"
        "except OSError: results.append('network-blocked')\n"
        "child=subprocess.run([sys.executable,'-c',"
        '"from pathlib import Path; '
        f"Path({str(main_file)!r}).write_text('child')\"],check=False)\n"
        "results.append('child-blocked' if child.returncode else 'child-escaped')\n"
        "print(','.join(results))\n"
    )

    result = await sandbox.run(
        ("/usr/bin/python3", "-c", script),
        timeout_seconds=5,
    )

    output = result.stdout.decode()
    assert result.returncode == 0
    assert (transaction / "created.txt").read_text(encoding="utf-8") == "ok"
    assert main_file.read_text(encoding="utf-8") == "main\n"
    assert "main-blocked" in output
    assert "home-blocked" in output
    assert "tmp-private" in output
    assert host_tmp_marker.read_text(encoding="utf-8") == "host\n"
    assert "network-blocked" in output
    assert "child-blocked" in output
    assert "child-escaped" not in output
    assert violations == []
    host_tmp_marker.unlink()
    await scope.close()
    scope.assert_empty()


@pytest.mark.asyncio
async def test_bubblewrap_command_environment_never_contains_provider_key(
    tmp_path: Path,
) -> None:
    executable = _bwrap_path()
    if executable is None:
        pytest.skip("主机未安装 bubblewrap；完整验收会提供临时解包二进制")
    repository = tmp_path / "repository"
    transaction = tmp_path / "transaction"
    repository.mkdir()
    transaction.mkdir()
    scope = ResourceScope("guard.sandbox.environment")
    sandbox = BubblewrapSandbox(
        WorkspaceBoundary(repository, transaction),
        scope=scope,
        executable=executable,
        environment={
            "PATH": "/usr/bin:/bin",
            "DEEPSEEK_API_KEY": "must-not-cross-boundary",
        },
    )

    result = await sandbox.run(
        (
            "/usr/bin/python3",
            "-c",
            "import os; print('DEEPSEEK_API_KEY' in os.environ)",
        ),
        timeout_seconds=2,
    )

    assert result.stdout.decode().strip() == "False"
    await scope.close()
