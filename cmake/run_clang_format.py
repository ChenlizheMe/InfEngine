from __future__ import annotations

import argparse
import subprocess
from pathlib import Path


_SOURCE_SUFFIXES = frozenset({".c", ".cc", ".cpp", ".cxx", ".h", ".hh", ".hpp", ".hxx"})
_WINDOWS_SAFE_COMMAND_LENGTH = 24_000


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Format Infernux C/C++ sources in safe batches.")
    parser.add_argument("--clang-format", required=True, type=Path)
    parser.add_argument("--source-root", required=True, type=Path)
    return parser.parse_args()


def _source_files(source_root: Path) -> list[Path]:
    return sorted(
        path.resolve()
        for path in source_root.rglob("*")
        if path.is_file() and path.suffix.lower() in _SOURCE_SUFFIXES
    )


def _batches(executable: Path, files: list[Path]) -> list[list[Path]]:
    base_length = len(str(executable)) + len(" -i")
    batches: list[list[Path]] = []
    current: list[Path] = []
    current_length = base_length

    for path in files:
        argument_length = len(str(path)) + 3
        if current and current_length + argument_length > _WINDOWS_SAFE_COMMAND_LENGTH:
            batches.append(current)
            current = []
            current_length = base_length
        current.append(path)
        current_length += argument_length

    if current:
        batches.append(current)
    return batches


def main() -> None:
    args = _parse_args()
    executable = args.clang_format.resolve()
    source_root = args.source_root.resolve()

    if not executable.is_file():
        raise FileNotFoundError(f"clang-format executable does not exist: {executable}")
    if not source_root.is_dir():
        raise NotADirectoryError(f"C/C++ source root does not exist: {source_root}")

    files = _source_files(source_root)
    batches = _batches(executable, files)
    print(f"Formatting {len(files)} C/C++ files in {len(batches)} batch(es)", flush=True)
    for batch in batches:
        subprocess.run(
            [str(executable), "-i", *(str(path) for path in batch)],
            check=True,
        )


if __name__ == "__main__":
    main()
