from __future__ import annotations

# Keep the managed runtime limited to the packages needed to launch Infernux
# projects and build standalone players from a project's private Python copy.
# Hub UI dependencies like PySide6 belong in the frozen launcher, not here.
_RUNTIME_PACKAGE_SPECS: tuple[tuple[str, str], ...] = (
    ("pip", "pip"),
    ("setuptools", "setuptools"),
    ("wheel", "wheel"),
    ("ordered-set", "ordered_set"),
    ("nuitka", "nuitka"),
    ("numpy", "numpy"),
    ("numba", "numba"),
    ("watchdog", "watchdog"),
    ("Pillow", "PIL"),
    ("imageio", "imageio"),
    ("av", "av"),
    ("mcp", "mcp"),
    ("fastmcp", "fastmcp"),
)


def runtime_package_specs() -> tuple[tuple[str, str], ...]:
    return _RUNTIME_PACKAGE_SPECS


def runtime_packages() -> tuple[str, ...]:
    return tuple(package for package, _module in _RUNTIME_PACKAGE_SPECS)


def runtime_modules() -> tuple[str, ...]:
    return tuple(module for _package, module in _RUNTIME_PACKAGE_SPECS)


__all__ = [
    "runtime_modules",
    "runtime_package_specs",
    "runtime_packages",
]
