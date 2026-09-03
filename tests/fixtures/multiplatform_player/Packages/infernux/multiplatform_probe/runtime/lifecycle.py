"""Package-relative resource probe executed before the fixture scene loads."""

import os
from pathlib import Path

from Infernux.debug import Debug
from Infernux.lifecycle import InxPreload, PreloadContext


_ENVIRONMENT_KEY = "_INFERNUX_FIXTURE_PACKAGE_MESSAGE"


class MultiplatformResourcePreload(InxPreload):
    def preload(self, context: PreloadContext) -> None:
        message_path = context.package_path("runtime/message.txt")
        message = Path(message_path).read_text(encoding="utf-8").strip()
        if not message:
            raise RuntimeError("Multiplatform package resource is empty")
        os.environ[_ENVIRONMENT_KEY] = message
        Debug.log(
            "INFERNUX_PLATFORM_FIXTURE_PRELOAD_RESOURCE_READY "
            f"value={message}"
        )

    def unload(self) -> None:
        os.environ.pop(_ENVIRONMENT_KEY, None)
