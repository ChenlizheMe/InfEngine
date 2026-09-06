"""Validate the instance extensions exposed by one explicit Vulkan loader/ICD."""

from __future__ import annotations

import argparse
import ctypes
import os
from pathlib import Path


VK_SUCCESS = 0


class VkExtensionProperties(ctypes.Structure):
    _fields_ = [
        ("extension_name", ctypes.c_char * 256),
        ("spec_version", ctypes.c_uint32),
    ]


def enumerate_instance_extensions(loader_path: Path) -> set[str]:
    if os.name != "nt":
        raise RuntimeError("This acceptance probe targets the Windows Vulkan loader")
    loader = ctypes.WinDLL(str(loader_path.resolve()))
    enumerate_extensions = loader.vkEnumerateInstanceExtensionProperties
    enumerate_extensions.argtypes = [
        ctypes.c_char_p,
        ctypes.POINTER(ctypes.c_uint32),
        ctypes.POINTER(VkExtensionProperties),
    ]
    enumerate_extensions.restype = ctypes.c_int32

    count = ctypes.c_uint32()
    result = enumerate_extensions(None, ctypes.byref(count), None)
    if result != VK_SUCCESS:
        raise RuntimeError(
            f"vkEnumerateInstanceExtensionProperties(count) returned {result}"
        )
    properties = (VkExtensionProperties * count.value)()
    result = enumerate_extensions(None, ctypes.byref(count), properties)
    if result != VK_SUCCESS:
        raise RuntimeError(
            f"vkEnumerateInstanceExtensionProperties(data) returned {result}"
        )
    return {
        bytes(item.extension_name).split(b"\0", 1)[0].decode("ascii")
        for item in properties[: count.value]
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--loader", required=True, type=Path)
    parser.add_argument("--require", action="append", default=[])
    args = parser.parse_args()

    extensions = enumerate_instance_extensions(args.loader)
    missing = sorted(set(args.require) - extensions)
    if missing:
        raise RuntimeError(
            "Vulkan ICD is missing required instance extensions: " + ", ".join(missing)
        )
    print("Vulkan instance extensions: " + ", ".join(sorted(extensions)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
