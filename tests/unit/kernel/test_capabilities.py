"""验证 capability 唯一提供者与错误边界。"""

from __future__ import annotations

import pytest

from rivet.contracts.modules import ActivationPolicy, ModuleManifest
from rivet.kernel.capabilities import CapabilityRegistry
from rivet.kernel.errors import CapabilityConflictError, CapabilityNotFoundError


def _manifest(
    module_id: str,
    capability_id: str,
    *,
    enabled: bool = True,
) -> ModuleManifest:
    return ModuleManifest(
        module_id=module_id,
        module_version="1.0.0",
        activation=ActivationPolicy.ON_DEMAND,
        factory="tests.fixtures.kernel.fake_modules:create_recording_module",
        enabled=enabled,
        provides=(capability_id,),
    )


def test_registry_returns_unique_enabled_provider() -> None:
    manifest = _manifest("reader.text", "reader.text.read")

    registry = CapabilityRegistry((manifest,))

    assert registry.provider_for("reader.text.read") == manifest


def test_registry_rejects_capability_conflict() -> None:
    manifests = (
        _manifest("reader.text", "reader.text.read"),
        _manifest("reader.markdown", "reader.text.read"),
    )

    with pytest.raises(CapabilityConflictError, match="reader.text.read"):
        CapabilityRegistry(manifests)


def test_registry_does_not_expose_disabled_provider() -> None:
    registry = CapabilityRegistry(
        (_manifest("reader.disabled", "reader.disabled.read", enabled=False),)
    )

    with pytest.raises(CapabilityNotFoundError, match="reader.disabled.read"):
        registry.provider_for("reader.disabled.read")
