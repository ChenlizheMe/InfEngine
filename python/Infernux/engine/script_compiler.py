"""
ScriptCompiler - Python script compilation checker for Infernux.

Validates Python scripts for syntax errors when files are modified.
Reports errors to the Console Panel for developer visibility.

Features:
- Syntax and bytecode checking from one captured source snapshot
- Import validation
- Error reporting with line numbers
- Integration with watchdog file monitoring
"""

import os
from typing import Optional, List
from dataclasses import dataclass

from Infernux.debug import Debug


@dataclass
class ScriptError:
    """Represents a script compilation error."""
    file_path: str
    line_number: int
    column: int
    message: str
    error_type: str  # 'syntax', 'import', 'semantic'
    
    def __str__(self) -> str:
        return f"{os.path.basename(self.file_path)}:{self.line_number}:{self.column}: {self.message}"


class ScriptCompiler:
    """
    Validates Python scripts for errors.
    
    Usage:
        compiler = ScriptCompiler()
        
        # Check a single file
        errors = compiler.check_file("/path/to/script.py")
        if errors:
            for error in errors:
                print(error)
        
        # Check and report to Debug console
        compiler.check_and_report("/path/to/script.py")
    """
    
    def __init__(self):
        self._last_errors: List[ScriptError] = []
    
    def check_file(self, file_path: str) -> List[ScriptError]:
        """
        Check a Python file for syntax errors.
        
        Args:
            file_path: Path to the Python file
            
        Returns:
            List of ScriptError objects (empty if no errors)
        """
        errors = []
        
        if not os.path.exists(file_path):
            errors.append(ScriptError(
                file_path=file_path,
                line_number=0,
                column=0,
                message="File not found",
                error_type="file"
            ))
            return errors
        
        if not file_path.endswith('.py'):
            return errors
        
        with open(file_path, "rb") as source_file:
            source = source_file.read()
        return self.check_source(file_path, source)

    def check_source(self, file_path: str, source: bytes | str) -> List[ScriptError]:
        """Validate exactly the captured source bytes supplied by the caller.

        This deliberately does not touch the filesystem.  It prevents a
        watcher from pairing one revision's hash with a later disk snapshot.
        """
        if not file_path.endswith('.py'):
            return []
        payload = source.encode("utf-8") if isinstance(source, str) else bytes(source)
        errors: List[ScriptError] = []
        try:
            compile(payload, file_path, "exec")
        except SyntaxError as error:
            errors.append(ScriptError(
                file_path=file_path,
                line_number=error.lineno or 0,
                column=error.offset or 0,
                message=str(error.msg),
                error_type="syntax",
            ))
        except Exception as error:
            errors.append(ScriptError(
                file_path=file_path,
                line_number=0,
                column=0,
                message=f"Unexpected error during source compile: {error}",
                error_type="compile",
            ))
        self._last_errors = errors
        return errors
    
    def check_and_report(self, file_path: str) -> bool:
        """
        Check a file and report errors to Debug console.
        
        Args:
            file_path: Path to the Python file
            
        Returns:
            True if no errors, False if errors found
        """
        errors = self.check_file(file_path)
        
        if not errors:
            # Optionally log success
            Debug.log_internal(f"[OK] Script compiled: {os.path.basename(file_path)}")
            return True
        
        # Report errors
        for error in errors:
            error_msg = f"[{error.error_type.upper()}] {error.file_path}:{error.line_number}:{error.column}\n{error.message}"
            Debug.log_error(error_msg,
                            source_file=error.file_path,
                            source_line=error.line_number)
        
        return False


# Global compiler instance
_compiler: Optional[ScriptCompiler] = None


def get_script_compiler() -> ScriptCompiler:
    """Get the global ScriptCompiler instance."""
    global _compiler
    if _compiler is None:
        _compiler = ScriptCompiler()
    return _compiler
