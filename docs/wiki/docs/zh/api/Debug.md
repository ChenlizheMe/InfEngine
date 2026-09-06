# Debug

<div class="class-info">
类位于 <b>Infernux.debug</b>
</div>

## 描述

调试工具类。

<!-- USER CONTENT START --> description

<!-- USER CONTENT END -->

## 静态方法

| 方法 | 描述 |
|------|------|
| `static Debug.log(message: Any, context: Any = ...) → None` | 输出日志消息到控制台。 |
| `static Debug.log_warning(message: Any, context: Any = ...) → None` | 输出警告消息到控制台。 |
| `static Debug.log_error(message: Any, context: Any = ..., source_file: str = ..., source_line: int = ...) → None` | 输出错误消息到控制台。 |
| `static Debug.log_exception(exception: Exception, context: Any = ...) → None` | Log an exception to the console. |
| `static Debug.log_suppressed(where: str, exc: BaseException, context: Any = ...) → None` | Log an exception that was deliberately swallowed by the caller. |
| `static Debug.log_assert(condition: bool, message: Any = ..., context: Any = ...) → None` | Assert a condition and log if it fails. |
| `static Debug.clear_console() → None` | Clear all messages in the debug console. |
| `static Debug.log_internal(message: Any, context: Any = ...) → None` | Log an internal engine message (hidden from user by default). |

<!-- USER CONTENT START --> static_methods

<!-- USER CONTENT END -->

## 示例

<!-- USER CONTENT START --> example
> **示例状态：** 当前尚未为此符号验证 0.4.0 示例。请以上方签名为准；不要根据其他引擎中的同名 API 推测行为。
<!-- USER CONTENT END -->

## 另请参阅

<!-- USER CONTENT START --> see_also

<!-- USER CONTENT END -->
