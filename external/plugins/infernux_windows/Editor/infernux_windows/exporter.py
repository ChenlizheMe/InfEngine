"""Windows x64 Player exporter."""

from __future__ import annotations

import sys

from Infernux.engine.build.contracts import BuildTarget, PlatformExporter
from Infernux.engine.build.host_player_export import (
    HOST_PLAYER_CAPABILITIES,
    create_host_player_plan,
    execute_host_player_build,
    host_machine_is_x64,
    inspect_host_player_request,
)


def windows_target() -> BuildTarget | None:
    if sys.platform != "win32" or not host_machine_is_x64():
        return None
    return BuildTarget(
        "windows-x64",
        "Windows x64",
        "windows",
        "x86_64",
        HOST_PLAYER_CAPABILITIES,
    )


class WindowsPlatformExporter(PlatformExporter):
    @property
    def exporter_id(self) -> str:
        return "infernux/platform-windows"

    def targets(self):
        target = windows_target()
        return (target,) if target is not None else ()

    def doctor(self, request):
        return inspect_host_player_request(
            request,
            windows_target(),
            exporter_id=self.exporter_id,
        )

    def create_plan(self, request):
        return create_host_player_plan(request)

    def execute(self, request, plan):
        return execute_host_player_build(request, plan)


__all__ = ["WindowsPlatformExporter", "windows_target"]
