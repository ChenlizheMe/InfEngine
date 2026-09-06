"""Package-relative resource probe executed before the fixture scene loads."""

import json
import os
from pathlib import Path

import infernux as inx


_ENVIRONMENT_KEY = "_INFERNUX_FIXTURE_PACKAGE_MESSAGE"


class MultiplatformResourcePreload(inx.InxPreload):
    def preload(self, context: inx.PreloadContext) -> None:
        message_path = context.package_path("runtime/message.txt")
        message = Path(message_path).read_text(encoding="utf-8").strip()
        if not message:
            raise RuntimeError("Multiplatform package resource is empty")
        config_path = Path(context.package_path("runtime/resource.json"))
        config = json.loads(config_path.read_text(encoding="utf-8"))
        resource_directory = Path(context.package_path("runtime"))
        if config_path.parent != resource_directory:
            raise RuntimeError("Package resource directory did not retain its layout")
        if (resource_directory / config["message"]).read_text(
            encoding="utf-8"
        ).strip() != message:
            raise RuntimeError("Package resource relative reference was not preserved")
        cooked_message = Path(
            inx.Application.asset_path("Assets/Data/preload_message.txt")
        ).read_text(encoding="utf-8").strip()
        if cooked_message != "Cooked asset reached the package preload.":
            raise RuntimeError("Cooked asset was not readable during package preload")
        os.environ[_ENVIRONMENT_KEY] = message
        inx.Debug.log(
            "INFERNUX_PLATFORM_FIXTURE_PRELOAD_RESOURCE_READY "
            f"value={message}"
        )

    def unload(self) -> None:
        os.environ.pop(_ENVIRONMENT_KEY, None)
