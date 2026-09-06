"""
Minimal setup.py to force platform-specific wheel tags.

Infernux ships pre-built native extensions (.pyd / .dll) as package data,
so the wheel must NOT be tagged 'py3-none-any'.  Overriding has_ext_modules()
makes setuptools produce a platform wheel (e.g. cp313-win_amd64).
"""

from setuptools import setup
from setuptools.dist import Distribution
from setuptools.command.build_py import build_py as _build_py

import os
import json
import shutil
from pathlib import Path


class BinaryDistribution(Distribution):
    def has_ext_modules(self):
        return True


class CleanPackageDataBuild(_build_py):
    """Do not let removed package data survive setuptools' reusable tree."""

    def run(self):
        if os.environ.get("INFERNUX_STAGED_WHEEL_BUILD") != "1":
            raise RuntimeError(
                "Infernux wheels contain compiled native runtime files and cannot be "
                "built directly from the source checkout. Build the platform wheel "
                "through the CMake package_python target instead."
            )

        native_source = Path.cwd() / "python" / "Infernux" / "lib"
        native_extensions = tuple(native_source.glob("_Infernux*.pyd")) + tuple(
            native_source.glob("_Infernux*.so")
        ) + tuple(native_source.glob("_Infernux*.dylib"))
        if not native_extensions:
            raise RuntimeError(
                "The staged Infernux wheel source is missing the compiled _Infernux "
                "native extension. Rebuild the CMake package_python target."
            )
        contract_path = native_source / "PlayerNativeContract.json"
        expected_contract = {
            "contract": "infernux.player-native",
            "runtime_linkage": "static",
        }
        try:
            contract = json.loads(contract_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RuntimeError(
                "The staged wheel source has no readable Player native contract."
            ) from exc
        if contract != expected_contract:
            raise RuntimeError("The staged wheel source has an invalid Player native contract.")

        font_output = Path(self.build_lib) / "Infernux" / "resources" / "fonts"
        if font_output.is_dir():
            shutil.rmtree(font_output)
        super().run()
        public_stub = Path.cwd() / "python" / "infernux.pyi"
        if not public_stub.is_file():
            raise RuntimeError(
                "The staged Infernux wheel source is missing python/infernux.pyi. "
                "Rebuild the CMake package_python target."
            )
        shutil.copy2(public_stub, Path(self.build_lib) / "infernux.pyi")


setup(distclass=BinaryDistribution, cmdclass={"build_py": CleanPackageDataBuild})
