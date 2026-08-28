"""验证 Phase 2 空 Kernel 的 RSS 与惰性导入门禁。"""

from __future__ import annotations

from typing import cast

from scripts.measure_kernel import collect_kernel_baseline


def test_empty_kernel_rss_and_help_imports_meet_limits() -> None:
    baseline = collect_kernel_baseline()
    empty_kernel = cast(dict[str, object], baseline["empty_kernel"])
    help_entrypoint = cast(dict[str, object], baseline["help_entrypoint"])
    manifest_loading = cast(dict[str, object], baseline["manifest_loading"])

    assert cast(float, empty_kernel["peak_rss_mib"]) <= 80
    assert empty_kernel["resource_count"] == 0
    assert empty_kernel["forbidden_loaded_modules"] == []
    assert help_entrypoint["forbidden_loaded_modules"] == []
    assert cast(float, manifest_loading["p95_ms"]) <= 30
