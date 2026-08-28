"""验证内置语言清单和 doctor 可执行文件探测。"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from rivet.context.lsp_doctor import LspDoctor
from rivet.context.lsp_manifest import LspManifestRegistry


def test_builtin_manifests_cover_python_and_typescript() -> None:
    registry = LspManifestRegistry.load_builtin(repository_root=Path.cwd())

    assert registry.for_path("src/app.py").server_id == "pyright"
    assert registry.for_path("web/app.tsx").server_id == "typescript"


def test_doctor_detects_both_local_language_servers() -> None:
    registry = LspManifestRegistry.load_builtin(repository_root=Path.cwd())
    report = LspDoctor(registry).inspect()

    assert {item.server_id for item in report.servers} == {"pyright", "typescript"}
    assert all(item.available for item in report.servers)
    assert report.ready is True


def test_rivet_doctor_json_is_machine_readable() -> None:
    completed = subprocess.run(
        [sys.executable, "-m", "rivet", "doctor", "--json"],
        cwd=Path.cwd(),
        check=False,
        capture_output=True,
        text=True,
    )

    payload = json.loads(completed.stdout)
    assert completed.returncode == 0
    assert payload["ready"] is True
    assert {server["server_id"] for server in payload["servers"]} == {
        "pyright",
        "typescript",
    }
