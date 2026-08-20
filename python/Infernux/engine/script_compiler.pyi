"""ScriptCompiler — syntax and import checking for user scripts.

Example::

    compiler = get_script_compiler()
    errors = compiler.check_file("/path/to/script.py")
    for err in errors:
        print(err)
"""

from __future__ import annotations

from typing import List


class ScriptError:
    """A single error found in a user script."""

    file_path: str
    line_number: int
    column: int
    message: str
    error_type: str  # "error" or "warning"

    def __str__(self) -> str: ...


class ScriptCompiler:
    """Validates Python scripts for syntax and import errors."""

    def __init__(self) -> None: ...

    def check_file(self, file_path: str) -> List[ScriptError]:
        """Check *file_path* for syntax and compile errors.

        Args:
            file_path: Absolute path to a ``.py`` file.

        Returns:
            List of :class:`ScriptError` objects (empty if clean).
        """
        ...

    def check_source(self, file_path: str, source: bytes | str) -> List[ScriptError]:
        """Validate the supplied source snapshot without rereading the file."""
        ...

    def check_and_report(self, file_path: str) -> bool:
        """Check a script and log any errors to the console.

        Returns:
            ``True`` if the script compiled without errors.
        """
        ...


def get_script_compiler() -> ScriptCompiler:
    """Return the shared :class:`ScriptCompiler` singleton."""
    ...
