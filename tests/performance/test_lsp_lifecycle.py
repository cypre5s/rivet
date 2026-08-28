"""验证 LSP 未请求、空闲和崩溃路径的资源边界。"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

from rivet.context.lsp_client import LspRequestTimeoutError
from rivet.context.lsp_manifest import LspServerManifest
from rivet.context.lsp_models import LspPosition
from rivet.context.lsp_sidecar import LspRestartLimitError, LspSidecar
from rivet.kernel.resources import ResourceScope


def _manifest(
    repository: Path,
    *,
    behavior: str,
    marker: Path | None = None,
    idle_timeout_seconds: float = 300.0,
    request_timeout_seconds: float = 1.0,
) -> LspServerManifest:
    """构造指向真实测试进程的语言清单。"""
    server = Path("tests/fixtures/context/lsp_server.py").resolve()
    arguments = [
        str(server),
        "--behavior",
        behavior,
        "--definition-uri",
        (repository / "target.py").as_uri(),
    ]
    if marker is not None:
        arguments.extend(("--marker", str(marker)))
    return LspServerManifest(
        server_id="fixture",
        language_ids=("python",),
        suffixes=(".py",),
        executable_candidates=(sys.executable,),
        arguments=tuple(arguments),
        initialization_options={},
        idle_timeout_seconds=idle_timeout_seconds,
        request_timeout_seconds=request_timeout_seconds,
        max_restarts=1,
    )


@pytest.mark.asyncio
async def test_lsp_stays_stopped_until_first_semantic_request(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    (repository / "target.py").write_text("symbol = 1\n", encoding="utf-8")
    scope = ResourceScope("context.lsp.not_requested")
    sidecar = LspSidecar(
        _manifest(repository, behavior="normal"),
        repository_root=repository,
        scope=scope,
    )

    assert sidecar.is_running is False
    assert sidecar.start_count == 0
    assert scope.counts().active_process_count == 0
    await sidecar.close()
    await scope.close()


@pytest.mark.asyncio
async def test_lsp_exits_after_idle_timeout(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    (repository / "target.py").write_text("symbol = 1\n", encoding="utf-8")
    scope = ResourceScope("context.lsp.idle")
    sidecar = LspSidecar(
        _manifest(repository, behavior="normal", idle_timeout_seconds=0.05),
        repository_root=repository,
        scope=scope,
    )

    assert await sidecar.document_symbols("target.py")
    assert sidecar.is_running is True
    await asyncio.sleep(0.15)

    assert sidecar.is_running is False
    assert scope.counts().active_process_count == 0
    scope.assert_empty()
    await sidecar.close()
    await scope.close()


@pytest.mark.asyncio
async def test_lsp_crash_restarts_at_most_once(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    (repository / "target.py").write_text("symbol = 1\n", encoding="utf-8")
    marker = tmp_path / "crashed.marker"
    scope = ResourceScope("context.lsp.restart")
    sidecar = LspSidecar(
        _manifest(repository, behavior="crash-once", marker=marker),
        repository_root=repository,
        scope=scope,
    )

    locations = await sidecar.definition("target.py", LspPosition(0, 1))

    assert {location.path for location in locations} == {"target.py"}
    assert sidecar.start_count == 2
    assert sidecar.restart_count == 1
    await sidecar.close()
    scope.assert_empty()
    await scope.close()


@pytest.mark.asyncio
async def test_lsp_second_crash_is_reported_without_loop(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    (repository / "target.py").write_text("symbol = 1\n", encoding="utf-8")
    scope = ResourceScope("context.lsp.restart_limit")
    sidecar = LspSidecar(
        _manifest(repository, behavior="crash-always"),
        repository_root=repository,
        scope=scope,
    )

    with pytest.raises(LspRestartLimitError, match="最多重启一次"):
        await sidecar.definition("target.py", LspPosition(0, 1))

    assert sidecar.start_count == 2
    assert sidecar.restart_count == 1
    await sidecar.close()
    scope.assert_empty()
    await scope.close()


@pytest.mark.asyncio
async def test_lsp_request_timeout_does_not_restart_or_leak(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    (repository / "target.py").write_text("symbol = 1\n", encoding="utf-8")
    scope = ResourceScope("context.lsp.timeout")
    sidecar = LspSidecar(
        _manifest(
            repository,
            behavior="no-response",
            request_timeout_seconds=0.05,
        ),
        repository_root=repository,
        scope=scope,
    )

    with pytest.raises(LspRequestTimeoutError, match="请求.*超时"):
        await sidecar.definition("target.py", LspPosition(0, 1))

    assert sidecar.restart_count == 0
    await sidecar.close()
    scope.assert_empty()
    await scope.close()
