"""
Debug - Unity-style debug utility for Infernux.

Provides logging methods that output to the Console Panel.
Supports Log, Warning, Error levels with timestamps and stack traces.

Usage:
    from Infernux.debug import Debug
    
    Debug.log("Hello, World!")
    Debug.log_warning("This is a warning")
    Debug.log_error("This is an error")
    
    # With context object
    Debug.log("Player position updated", context=player)
"""

import traceback
import os
from datetime import datetime
from enum import Enum, auto
from typing import Any, Optional, List, Callable
from dataclasses import dataclass, field


class LogType(Enum):
    """Log message types matching Unity's LogType."""
    LOG = auto()
    WARNING = auto()
    ERROR = auto()
    ASSERT = auto()
    EXCEPTION = auto()


@dataclass
class LogEntry:
    """Represents a single log entry."""
    message: str
    log_type: LogType
    timestamp: datetime
    stack_trace: str = ""
    context: Any = None
    internal: bool = False
    source_file: str = ""
    source_line: int = 0
    
    def get_formatted_time(self) -> str:
        """Get formatted timestamp string."""
        return self.timestamp.strftime("%H:%M:%S.%f")[:-3]
    
    def get_icon(self) -> str:
        """Get icon/prefix for the log type."""
        icons = {
            LogType.LOG: "[I]",
            LogType.WARNING: "[W]",
            LogType.ERROR: "[E]",
            LogType.ASSERT: "[A]",
            LogType.EXCEPTION: "[!]",
        }
        return icons.get(self.log_type, "-")


def _sanitize_text(value: Any) -> str:
    """Normalize arbitrary text into a stable Unicode string."""
    if value is None:
        return ""
    if isinstance(value, bytes):
        text = value.decode('utf-8', errors='replace')
    else:
        text = str(value)
    text = text.replace('\x00', '�')
    return text.encode('utf-8', errors='replace').decode('utf-8', errors='replace')


def _safe_stream_write(stream, text: str) -> None:
    """Write text to a console stream without crashing on local code pages."""
    if stream is None:
        return
    text = _sanitize_text(text)
    encoding = getattr(stream, 'encoding', None) or 'utf-8'
    try:
        stream.write(text + os.linesep)
    except (UnicodeEncodeError, OSError):
        try:
            if hasattr(stream, 'buffer'):
                stream.buffer.write((text + os.linesep).encode(encoding, errors='replace'))
                stream.flush()
            else:
                stream.write((text + os.linesep).encode(encoding, errors='replace').decode(encoding, errors='replace'))
        except (UnicodeEncodeError, OSError):
            pass


class DebugConsole:
    """
    Central debug console that stores log entries.
    This is a singleton that can be accessed by the ConsolePanel.
    """
    
    _instance: Optional['DebugConsole'] = None

    def __init__(self):
        from Infernux.engine.runtime_dispatch import ReloadableCallbackRegistry

        self._entries: List[LogEntry] = []
        self._max_entries: int = 1000
        self._listener_registry = ReloadableCallbackRegistry()
        self._native_console = None  # C++ ConsolePanel (set by bootstrap)

        # Counters for quick filtering
        self._log_count: int = 0
        self._warning_count: int = 0
        self._error_count: int = 0
        DebugConsole._instance = self

    @classmethod
    def instance(cls) -> 'DebugConsole':
        """Get or create the singleton instance."""
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance
    
    def add_listener(self, callback: Callable[[LogEntry], None]):
        """Add a listener to be notified of new log entries."""
        self._listener_registry.add_listener(callback)
    
    def remove_listener(self, callback: Callable[[LogEntry], None]):
        """Remove a log listener."""
        self._listener_registry.remove_listener(callback)

    def set_native_console(self, native_console):
        """Attach the C++ ConsolePanel so Python Debug.log() messages are forwarded."""
        self._native_console = native_console

    @staticmethod
    def _write_internal_entry(entry: LogEntry):
        """Route internal editor diagnostics to engine.log only."""
        try:
            from Infernux.lib import inflog_internal

            payload = entry.message
            if entry.source_file:
                payload = f"{payload} [py:{entry.source_file}:{entry.source_line}]"
            inflog_internal(payload)
        except Exception:
            pass

    _LOGTYPE_TO_LOGLEVEL = None  # lazy-initialized mapping

    @staticmethod
    def _get_level_map():
        """Lazy-load LogType → LogLevel mapping (avoids import at module load)."""
        if DebugConsole._LOGTYPE_TO_LOGLEVEL is None:
            try:
                from Infernux.lib import LogLevel
                DebugConsole._LOGTYPE_TO_LOGLEVEL = {
                    LogType.LOG: LogLevel.Info,
                    LogType.WARNING: LogLevel.Warn,
                    LogType.ERROR: LogLevel.Error,
                    LogType.ASSERT: LogLevel.Error,
                    LogType.EXCEPTION: LogLevel.Error,
                }
            except Exception:
                DebugConsole._LOGTYPE_TO_LOGLEVEL = {}
        return DebugConsole._LOGTYPE_TO_LOGLEVEL
    
    def log(self, entry: LogEntry):
        """Add a log entry."""
        if entry.internal:
            self._write_internal_entry(entry)
            return

        # Trim old entries if needed
        if len(self._entries) >= self._max_entries:
            removed = self._entries.pop(0)
            self._update_counters(removed.log_type, -1)
        
        self._entries.append(entry)
        self._update_counters(entry.log_type, 1)
        
        # Notify listeners
        self._listener_registry.invoke(entry, propagate_exceptions=True)
        
        # Forward to C++ ConsolePanel if attached
        if self._native_console is not None:
            level_map = self._get_level_map()
            level = level_map.get(entry.log_type)
            if level is not None:
                self._native_console.log_from_python(
                    level, entry.message,
                    entry.stack_trace or "",
                    entry.source_file or "",
                    entry.source_line,
                )
        
        # Also print to stdout/stderr for development
        self._print_entry(entry)
    
    def _update_counters(self, log_type: LogType, delta: int):
        """Update log type counters."""
        if log_type == LogType.LOG:
            self._log_count += delta
        elif log_type == LogType.WARNING:
            self._warning_count += delta
        elif log_type in (LogType.ERROR, LogType.ASSERT, LogType.EXCEPTION):
            self._error_count += delta
    
    def _print_entry(self, entry: LogEntry):
        """Print entry to console for development."""
        prefix = f"[{entry.get_formatted_time()}] [{entry.log_type.name}]"
        message = _sanitize_text(f"{prefix} {entry.message}")
        
        if entry.log_type in (LogType.ERROR, LogType.ASSERT, LogType.EXCEPTION):
            import sys
            _safe_stream_write(sys.stderr, message)
            if entry.stack_trace:
                _safe_stream_write(sys.stderr, entry.stack_trace)
        else:
            import sys
            _safe_stream_write(sys.stdout, message)
    
    def get_entries(self) -> List[LogEntry]:
        """Get all log entries."""
        return self._entries.copy()
    
    def get_filtered_entries(self, 
                            show_logs: bool = True,
                            show_warnings: bool = True,
                            show_errors: bool = True) -> List[LogEntry]:
        """Get filtered log entries."""
        result = []
        for entry in self._entries:
            if entry.log_type == LogType.LOG and show_logs:
                result.append(entry)
            elif entry.log_type == LogType.WARNING and show_warnings:
                result.append(entry)
            elif entry.log_type in (LogType.ERROR, LogType.ASSERT, LogType.EXCEPTION) and show_errors:
                result.append(entry)
        return result
    
    def clear(self):
        """Clear all log entries."""
        self._entries.clear()
        self._log_count = 0
        self._warning_count = 0
        self._error_count = 0
        if self._native_console is not None:
            self._native_console.clear()

    @staticmethod
    def _source_key(source_file: str) -> str:
        if not source_file:
            return ""
        # Import lazily because Debug is available before the engine package
        # finishes bootstrapping. Filesystem identity itself remains owned by
        # the shared path service.
        from Infernux.engine.path_utils import path_key

        return path_key(source_file)

    def remove_source_entries(self, source_file: str) -> int:
        """Remove diagnostics emitted by one source file.

        Script publication uses this after a new revision has committed, so a
        corrected script loses only its stale errors while unrelated Console
        history remains intact.
        """
        source_key = self._source_key(source_file)
        if not source_key:
            return 0
        retained = [
            entry
            for entry in self._entries
            if self._source_key(entry.source_file) != source_key
        ]
        removed = len(self._entries) - len(retained)
        if removed:
            self._entries = retained
            self._log_count = 0
            self._warning_count = 0
            self._error_count = 0
            for entry in retained:
                self._update_counters(entry.log_type, 1)
        if self._native_console is not None:
            remove_native = getattr(
                self._native_console,
                "remove_entries_from_source",
                None,
            )
            if callable(remove_native):
                remove_native(source_file)
        return removed
    
    @property
    def log_count(self) -> int:
        return self._log_count
    
    @property
    def warning_count(self) -> int:
        return self._warning_count
    
    @property
    def error_count(self) -> int:
        return self._error_count


class Debug:
    """
    Unity-style Debug class for logging.
    All methods are static, no instantiation needed.
    
    Usage:
        Debug.log("Message")
        Debug.log_warning("Warning message")
        Debug.log_error("Error message")
    """
    
    @staticmethod
    def _create_entry(message: Any, log_type: LogType, 
                      context: Any = None, include_trace: bool = False,
                      internal: bool = False,
                      source_file: str = "",
                      source_line: int = 0) -> LogEntry:
        """Create a log entry with optional stack trace.

        *source_file* / *source_line* — when provided, override the
        auto-detected caller location so the console "open in editor"
        feature jumps to the right file (e.g. the user script that
        caused the error rather than the engine handler that caught it).
        """
        msg_str = _sanitize_text(message)
        stack_trace = ""
        
        if include_trace:
            # Skip frames from Debug class itself
            stack_trace = _sanitize_text(''.join(traceback.format_stack()[:-2]))
        
        # If caller did NOT supply an explicit source, auto-detect from
        # the call stack (frame 2: _create_entry → log* → caller).
        if not source_file:
            import sys
            frame = sys._getframe(2)
            source_file = _sanitize_text(frame.f_code.co_filename)
            source_line = frame.f_lineno
        else:
            source_file = _sanitize_text(source_file)
        
        return LogEntry(
            message=msg_str,
            log_type=log_type,
            timestamp=datetime.now(),
            stack_trace=stack_trace,
            context=context,
            internal=internal,
            source_file=source_file,
            source_line=source_line,
        )
    
    @staticmethod
    def log(message: Any, context: Any = None):
        """
        Log a message to the Console.
        
        Args:
            message: The message to log (will be converted to string)
            context: Optional context object for reference
        """
        entry = Debug._create_entry(message, LogType.LOG, context)
        DebugConsole.instance().log(entry)
    
    @staticmethod
    def log_warning(message: Any, context: Any = None):
        """
        Log a warning message to the Console.
        
        Args:
            message: The warning message
            context: Optional context object
        """
        entry = Debug._create_entry(message, LogType.WARNING, context, include_trace=True)
        DebugConsole.instance().log(entry)
    
    @staticmethod
    def log_error(message: Any, context: Any = None, *,
                  source_file: str = "", source_line: int = 0):
        """
        Log an error message to the Console.
        
        Args:
            message: The error message
            context: Optional context object
            source_file: Override auto-detected source file path
            source_line: Override auto-detected source line number
        """
        entry = Debug._create_entry(message, LogType.ERROR, context,
                                    include_trace=True,
                                    source_file=source_file,
                                    source_line=source_line)
        DebugConsole.instance().log(entry)
    
    @staticmethod
    def log_exception(exception: Exception, context: Any = None):
        """
        Log an exception with full traceback.
        
        Args:
            exception: The exception to log
            context: Optional context object
        """
        message = _sanitize_text(f"{type(exception).__name__}: {exception}")
        stack_trace = _sanitize_text(''.join(traceback.format_exception(type(exception), exception, exception.__traceback__)))
        
        # Extract source location from the innermost traceback frame
        source_file = ""
        source_line = 0
        tb = exception.__traceback__
        while tb and tb.tb_next:
            tb = tb.tb_next
        if tb:
            source_file = _sanitize_text(tb.tb_frame.f_code.co_filename)
            source_line = tb.tb_lineno
        
        entry = LogEntry(
            message=message,
            log_type=LogType.EXCEPTION,
            timestamp=datetime.now(),
            stack_trace=stack_trace,
            context=context,
            source_file=source_file,
            source_line=source_line,
        )
        DebugConsole.instance().log(entry)
    
    @staticmethod
    def log_suppressed(where: str, exc: BaseException, context: Any = None):
        """Log an exception that was deliberately swallowed by the caller.

        Standardises the legacy
        ``Debug.log(f"[Suppressed] {type(_exc).__name__}: {_exc}")`` pattern
        and routes it through ``log_warning`` so suppressed-but-bug-shaped
        events are visible in the editor console by default. The previous
        ``Debug.log`` (info-level) variant made these invisible behind log
        filters and effectively hid recurring state corruption.

        Args:
            where: Short human-readable label of the suppression site
                (e.g. ``"DeferredTaskRunner.tick"``).
            exc: The caught exception instance.
            context: Optional context object forwarded to the console entry.
        """
        message = f"[Suppressed @ {where}] {type(exc).__name__}: {exc}"
        Debug.log_warning(message, context=context)

    @staticmethod
    def log_assert(condition: bool, message: Any = "Assertion failed", context: Any = None):
        """
        Log an assertion failure if condition is False.
        
        Args:
            condition: The condition to check
            message: Message to log if condition is False
            context: Optional context object
        """
        if not condition:
            entry = Debug._create_entry(message, LogType.ASSERT, context, include_trace=True)
            DebugConsole.instance().log(entry)
    
    @staticmethod
    def clear_console():
        """Clear all entries from the Console."""
        DebugConsole.instance().clear()

    @staticmethod
    def clear_source_entries(source_file: str) -> int:
        """Clear Console diagnostics owned by one source file."""
        return DebugConsole.instance().remove_source_entries(source_file)

    @staticmethod
    def log_internal(message: Any, context: Any = None):
        """Log an internal engine message (hidden from user Console)."""
        entry = Debug._create_entry(message, LogType.LOG, context, internal=True)
        DebugConsole.instance().log(entry)


# Convenience aliases for Unity-style usage
log = Debug.log
log_warning = Debug.log_warning
log_error = Debug.log_error
log_exception = Debug.log_exception


class _DebugProxy:
    """Lowercase ``debug`` proxy — allows ``debug.log(...)`` style calls.

    All attribute lookups are forwarded to the ``Debug`` class, so the
    full API is available::

        from Infernux.debug import debug

        debug.log("hello")
        debug.log_warning("careful")
        debug.log_error("oops")
        debug.log_exception(exc)
        debug.clear_console()
    """

    __slots__ = ()

    def __getattr__(self, name: str):
        return getattr(Debug, name)

    def __repr__(self):
        return "debug"


debug = _DebugProxy()
