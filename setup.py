"""
Minimal setup.py to force platform-specific wheel tags.

Infernux ships pre-built native extensions (.pyd / .dll) as package data,
so the wheel must NOT be tagged 'py3-none-any'.  Overriding has_ext_modules()
makes setuptools produce a platform wheel (e.g. cp312-win_amd64).
"""

from setuptools import setup
from setuptools.dist import Distribution
from setuptools.command.build_py import build_py as _build_py

import shutil
from pathlib import Path


class BinaryDistribution(Distribution):
    def has_ext_modules(self):
        return True


class CleanFontDataBuild(_build_py):
    """Do not let removed fonts survive in setuptools' reusable build tree."""

    def run(self):
        font_output = Path(self.build_lib) / "Infernux" / "resources" / "fonts"
        if font_output.is_dir():
            shutil.rmtree(font_output)
        super().run()


setup(distclass=BinaryDistribution, cmdclass={"build_py": CleanFontDataBuild})
