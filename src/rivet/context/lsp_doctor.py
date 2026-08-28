"""检测静态 LSP Manifest 对应的本地可执行文件。"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass

from .lsp_manifest import (
    LspManifestRegistry,
    LspServerUnavailableError,
)


@dataclass(frozen=True, slots=True)
class LspDoctorItem:
    """保存单个语言服务的脱敏探测结果。"""

    server_id: str
    available: bool
    executable: str | None
    next_action: str | None


@dataclass(frozen=True, slots=True)
class LspDoctorReport:
    """汇总所有内置语言服务的就绪状态。"""

    ready: bool
    servers: tuple[LspDoctorItem, ...]

    def to_json(self) -> str:
        """返回稳定字段顺序的机器可读 JSON。"""
        return json.dumps(asdict(self), ensure_ascii=False, sort_keys=True)


class LspDoctor:
    """只检查文件存在与可执行位，不启动语言服务。"""

    def __init__(self, registry: LspManifestRegistry) -> None:
        self._registry = registry

    def inspect(self) -> LspDoctorReport:
        """按 server_id 返回全部成功或可操作失败。"""
        items: list[LspDoctorItem] = []
        for manifest in self._registry.manifests:
            try:
                executable = manifest.resolve_executable()
            except LspServerUnavailableError:
                items.append(
                    LspDoctorItem(
                        server_id=manifest.server_id,
                        available=False,
                        executable=None,
                        next_action=(
                            "安装并确保至少一个 Manifest executable candidate 可执行"
                        ),
                    )
                )
            else:
                items.append(
                    LspDoctorItem(
                        server_id=manifest.server_id,
                        available=True,
                        executable=str(executable),
                        next_action=None,
                    )
                )
        servers = tuple(items)
        return LspDoctorReport(
            ready=bool(servers) and all(item.available for item in servers),
            servers=servers,
        )
