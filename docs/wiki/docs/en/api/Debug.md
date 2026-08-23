# Debug

<div class="class-info">
class in <b>Infernux.debug</b>
</div>

## Description

Utility class for logging messages to the console.

<!-- USER CONTENT START --> description

<!-- USER CONTENT END -->

## Static Methods

| Method | Description |
|------|------|
| `static Debug.log(message: Any, context: Any = ...) → None` | Log a message to the console. |
| `static Debug.log_warning(message: Any, context: Any = ...) → None` | Log a warning message to the console. |
| `static Debug.log_error(message: Any, context: Any = ..., source_file: str = ..., source_line: int = ...) → None` | Log an error message to the console. |
| `static Debug.log_exception(exception: Exception, context: Any = ...) → None` | Log an exception to the console. |
| `static Debug.log_suppressed(where: str, exc: BaseException, context: Any = ...) → None` | Log an exception that was deliberately swallowed by the caller. |
| `static Debug.log_assert(condition: bool, message: Any = ..., context: Any = ...) → None` | Assert a condition and log if it fails. |
| `static Debug.clear_console() → None` | Clear all messages in the debug console. |
| `static Debug.log_internal(message: Any, context: Any = ...) → None` | Log an internal engine message (hidden from user by default). |

<!-- USER CONTENT START --> static_methods

<!-- USER CONTENT END -->

## Example

<!-- USER CONTENT START --> example
> **Example status:** No curated example has been verified for this symbol in 0.3.5. Use the signatures above; do not infer behavior from similarly named APIs in other engines.
<!-- USER CONTENT END -->

## See Also

<!-- USER CONTENT START --> see_also

<!-- USER CONTENT END -->
