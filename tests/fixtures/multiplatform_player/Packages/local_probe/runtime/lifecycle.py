"""Local author package with no installation record or source manifest."""

import os
from pathlib import Path

import infernux as inx


class LocalAuthorPreload(inx.InxPreload):
    def preload(self, context: inx.PreloadContext) -> None:
        message = Path(context.package_path("runtime/message.txt")).read_text(
            encoding="utf-8"
        ).strip()
        if message != "Local author package reached Player preload.":
            raise RuntimeError("Local author package resource was not exported")
        os.environ["_INFERNUX_FIXTURE_LOCAL_AUTHOR_MESSAGE"] = message

    def unload(self) -> None:
        os.environ.pop("_INFERNUX_FIXTURE_LOCAL_AUTHOR_MESSAGE", None)
