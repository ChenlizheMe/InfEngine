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
import shutil
from pathlib import Path


class BinaryDistribution(Distribution):
    def has_ext_modules(self):
        return True


class CleanPackageDataBuild(_build_py):
    """Do not let removed package data survive setuptools' reusable tree."""

    _FORBIDDEN_NATIVE_FILES = (
        "InfernuxRuntime.dll",
        "SPIRV.dll",
        "SPVRemapper.dll",
        "glslang-default-resource-limits.dll",
        "glslang.dll",
    )

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

        font_output = Path(self.build_lib) / "Infernux" / "resources" / "fonts"
        if font_output.is_dir():
            shutil.rmtree(font_output)
        super().run()
        native_output = Path(self.build_lib) / "Infernux" / "lib"
        for filename in self._FORBIDDEN_NATIVE_FILES:
            (native_output / filename).unlink(missing_ok=True)


setup(distclass=BinaryDistribution, cmdclass={"build_py": CleanPackageDataBuild})
