"""验证模块依赖图的确定性与失败关闭行为。"""

from __future__ import annotations

import pytest

from rivet.contracts.modules import ActivationPolicy, ModuleManifest
from rivet.kernel.errors import ModuleDependencyError
from rivet.kernel.module_graph import stable_activation_order


def _manifest(module_id: str, requires: tuple[str, ...] = ()) -> ModuleManifest:
    suffix = module_id.rsplit(".", maxsplit=1)[-1]
    return ModuleManifest(
        module_id=module_id,
        module_version="1.0.0",
        activation=ActivationPolicy.ON_DEMAND,
        factory="tests.fixtures.kernel.fake_modules:create_recording_module",
        provides=(f"test.{suffix}.capability",),
        requires=requires,
    )


def test_graph_orders_dependencies_before_dependents_stably() -> None:
    manifests = (
        _manifest("test.root", ("test.beta", "test.alpha")),
        _manifest("test.beta"),
        _manifest("test.alpha"),
    )

    order = stable_activation_order(manifests)

    assert order == ("test.alpha", "test.beta", "test.root")


def test_graph_rejects_dependency_cycle() -> None:
    manifests = (
        _manifest("test.alpha", ("test.beta",)),
        _manifest("test.beta", ("test.alpha",)),
    )

    with pytest.raises(ModuleDependencyError, match="环"):
        stable_activation_order(manifests)


def test_graph_rejects_missing_dependency() -> None:
    manifests = (_manifest("test.alpha", ("test.missing",)),)

    with pytest.raises(ModuleDependencyError, match="test.missing"):
        stable_activation_order(manifests)
