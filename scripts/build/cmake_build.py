"""
Wrapper around ``cmake --build`` that deduplicates environment variables
whose names differ only by case (e.g. SYSTEMDRIVE vs SystemDrive).

MSBuild on .NET Framework uses a case-sensitive Hashtable for env vars,
so duplicate-cased keys crash CL.exe.  Python's os.environ on Windows is
case-insensitive and keeps only one entry per key, which is exactly what
we need.

Usage (drop-in replacement for ``cmake --build``):
    python scripts/build/cmake_build.py --preset release
    python scripts/build/cmake_build.py --preset debug
    python scripts/build/cmake_build.py --preset release --target _Infernux
"""

import os
import subprocess
import sys


def main() -> int:
    # os.environ on Windows is case-insensitive — duplicates are merged.
    env = {k: v for k, v in os.environ.items()}
    args = ["cmake", "--build"] + sys.argv[1:]
    result = subprocess.run(args, env=env)
    return result.returncode


if __name__ == "__main__":
    sys.exit(main())
