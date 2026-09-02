"""验证 capability 唯一提供者与错误边界。"""

from __future__ import annotations

import pytest

from rivet.contracts.modules import ModuleManifest
from rivet.kernel.capabilities import CapabilityRegistry
from rivet.kernel.errors import CapabilityConflictError, CapabilityNotFoundError


def _manifest(
    module_id: str,
    capability_id: str,
) -> ModuleManifest:
    return ModuleManifest(
        module_id=module_id,
        factory="tests.fixtures.kernel.fake_modules:create_recording_module",
        provides=(capability_id,),
    )


def test_registry_returns_unique_provider() -> None:
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


def test_registry_rejects_missing_provider() -> None:
    registry = CapabilityRegistry(())

    with pytest.raises(CapabilityNotFoundError, match="reader.missing.read"):
        registry.provider_for("reader.missing.read")
