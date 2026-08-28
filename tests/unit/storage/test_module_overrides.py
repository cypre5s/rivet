"""验证应用级与工作区级模块启用覆盖的隔离语义。"""

from pathlib import Path

from rivet.contracts.modules import (
    ActivationPolicy,
    ModuleManifest,
    ModuleOverrideChange,
    ModuleScope,
)
from rivet.storage.module_overrides import SQLiteModuleOverrideStore


def _manifest(module_id: str, scope: ModuleScope) -> ModuleManifest:
    """构造仅用于覆盖存储的静态 Manifest。"""
    return ModuleManifest(
        module_id=module_id,
        module_version="1.0.0",
        activation=ActivationPolicy.ON_DEMAND,
        factory="tests.fixtures.kernel.fake_modules:create_recording_module",
        scope=scope,
        provides=(f"{module_id}.use",),
    )


def test_application_override_is_shared_and_workspace_override_is_isolated(
    tmp_path: Path,
) -> None:
    repository_one = tmp_path / "repository-one"
    repository_two = tmp_path / "repository-two"
    repository_one.mkdir()
    repository_two.mkdir()
    database_path = tmp_path / "state" / "module-state.sqlite3"
    application_manifest = _manifest("test.application", ModuleScope.APPLICATION)
    workspace_manifest = _manifest("test.workspace", ModuleScope.WORKSPACE)
    manifests = (application_manifest, workspace_manifest)
    store_one = SQLiteModuleOverrideStore(database_path, repository_one)
    store_two = SQLiteModuleOverrideStore(database_path, repository_two)

    store_one.set_many(
        (
            ModuleOverrideChange(
                module_id=application_manifest.module_id,
                scope=ModuleScope.APPLICATION,
                enabled=False,
                source="cli",
            ),
            ModuleOverrideChange(
                module_id=workspace_manifest.module_id,
                scope=ModuleScope.WORKSPACE,
                enabled=False,
                source="cli",
            ),
        )
    )

    assert store_one.load(manifests) == {
        "test.application": False,
        "test.workspace": False,
    }
    assert store_two.load(manifests) == {
        "test.application": False,
        "test.workspace": None,
    }

    store_two.set_many(
        (
            ModuleOverrideChange(
                module_id=workspace_manifest.module_id,
                scope=ModuleScope.WORKSPACE,
                enabled=True,
                source="tui",
            ),
        )
    )
    assert store_one.load(manifests)["test.workspace"] is False
    assert store_two.load(manifests)["test.workspace"] is True
