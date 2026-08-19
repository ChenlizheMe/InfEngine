"""Private process entry point for responsive native Player packaging."""

from __future__ import annotations

import json
import os
import sys
import traceback

from Infernux.engine.player_package_native import write_pack


def _publish(path: str, payload: dict[str, object]) -> None:
    temporary = path + f".tmp.{os.getpid()}"
    with open(temporary, "w", encoding="utf-8") as output:
        json.dump(payload, output, ensure_ascii=False)
        output.flush()
        os.fsync(output.fileno())
    os.replace(temporary, path)


def main() -> int:
    if len(sys.argv) != 3:
        return 2
    request_path, response_path = sys.argv[1:]
    try:
        with open(request_path, "r", encoding="utf-8") as source:
            request = json.load(source)
        manifest = write_pack(
            request["files"],
            request["destination"],
            compression_level=request.get("compression_level"),
            profile=str(request.get("profile", "development")),
        )
        _publish(response_path, {"ok": True, "manifest": manifest})
        return 0
    except BaseException as exc:
        try:
            _publish(
                response_path,
                {
                    "ok": False,
                    "error": f"{type(exc).__name__}: {exc}",
                    "traceback": traceback.format_exc(),
                },
            )
        except BaseException:
            traceback.print_exc()
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
