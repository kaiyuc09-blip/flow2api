"""Debug logger module for detailed API request/response logging"""

import json
import logging
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Dict, Any, Optional
from urllib.parse import urlsplit, urlunsplit
from .config import config


REDACTED = "[REDACTED]"


def _sensitive_key(key: str) -> bool:
    normalized = re.sub(r"[^a-z0-9]", "", key.lower())
    return normalized in {"at", "st", "key", "auth", "sid", "ssid", "hsid", "sapisid", "apisid"} or any(
        word in normalized for word in (
            "authorization", "cookie", "password", "passwd", "secret",
            "apikey", "clientkey", "token", "credential", "sessionid",
        )
    )


def _redact_data(data: Any) -> Any:
    if isinstance(data, dict):
        return {
            key: REDACTED if (
                _sensitive_key(str(key)) or (
                    str(key).lower() in {"encodedimage", "base64", "imagedata", "data"}
                    and isinstance(value, str)
                )
            ) else _redact_data(value)
            for key, value in data.items()
        }
    if isinstance(data, (list, tuple)):
        return [_redact_data(value) for value in data]
    if isinstance(data, str):
        return _redact_text(data)
    return data


def _redact_text(text: str) -> str:
    try:
        parsed = json.loads(text)
    except (ValueError, TypeError):
        parsed = None
    if isinstance(parsed, (dict, list)):
        return json.dumps(_redact_data(parsed), ensure_ascii=False, indent=2)
    text = re.sub(r'''data:[^\s,;]+;base64,[^\s"'<>]+''', "data:[REDACTED]", text, flags=re.I)
    def redact_url(match):
        try:
            parsed_url = urlsplit(match.group(0))
            authority = parsed_url.netloc.rsplit("@", 1)[-1]
            return urlunsplit((
                parsed_url.scheme, authority, parsed_url.path,
                REDACTED if parsed_url.query else "",
                REDACTED if parsed_url.fragment else "",
            ))
        except ValueError:
            return REDACTED
    # All query values are omitted: signed URL field names vary by provider.
    text = re.sub(r'''\b(?:https?|socks5h?|socks4)://[^\s<>"']+''', redact_url, text, flags=re.I)
    # Protect entire cookie/authorization values, including multiple cookies.
    text = re.sub(
        r"(?im)(\b[\w-]*(?:cookie|authorization)[\w-]*\s*[:=]\s*)[^\n]+",
        lambda match: match.group(1) + REDACTED,
        text,
    )
    text = re.sub(r"(?i)\bBearer\s+[^\s,;\"']+", "Bearer " + REDACTED, text)
    return re.sub(
        r'''(?P<key>\b(?:[\w-]*(?:authorization|cookie|password|passwd|secret|api[_-]?key|client[_-]?key|token|credential|session[_-]?id)[\w-]*|at|st|key|auth|sid|ssid|hsid|sapisid|apisid))(?P<sep>["']?\s*(?:返回|值)?\s*[:：=]\s*)(?P<value>\[REDACTED\]|"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|[^\s,;}\]]+)''',
        lambda match: match.group("key") + match.group("sep") + REDACTED,
        text,
        flags=re.I,
    )


class _CredentialFilter(logging.Filter):
    """Final guard applies to every debug log entry, including free-form text."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = _redact_text(record.getMessage())
        record.args = ()
        return True


class DebugLogger:
    """Debug logger for API requests and responses"""

    def __init__(self):
        self.log_file = Path(os.environ.get("FLOW2API_LOG_PATH") or "logs.txt")
        self.log_file.parent.mkdir(parents=True, exist_ok=True)
        self._setup_logger()

    def _setup_logger(self):
        """Setup file logger"""
        # Create logger
        self.logger = logging.getLogger("debug_logger")
        self.logger.setLevel(logging.DEBUG)
        if not any(isinstance(item, _CredentialFilter) for item in self.logger.filters):
            self.logger.addFilter(_CredentialFilter())

        # Remove existing handlers
        self.logger.handlers.clear()

        # Create file handler
        file_handler = logging.FileHandler(self.log_file, mode="a", encoding="utf-8")
        file_handler.setLevel(logging.DEBUG)

        # Create formatter
        formatter = logging.Formatter("%(message)s", datefmt="%Y-%m-%d %H:%M:%S")
        file_handler.setFormatter(formatter)

        # Add handler
        self.logger.addHandler(file_handler)

        # Prevent propagation to root logger
        self.logger.propagate = False

    def _mask_token(self, token: str) -> str:
        """Credentials must never be logged, even partially or in debug mode."""
        return REDACTED

    def _format_timestamp(self) -> str:
        """Format current timestamp"""
        return datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]

    def _write_separator(self, char: str = "=", length: int = 100):
        """Write separator line"""
        self.logger.info(char * length)

    def _truncate_large_fields(self, data: Any, max_length: int = 200) -> Any:
        """对大字段进行截断处理，特别是 base64 编码的图片数据
        
        Args:
            data: 要处理的数据
            max_length: 字符串字段的最大长度
        
        Returns:
            截断后的数据副本
        """
        if isinstance(data, dict):
            result = {}
            for key, value in data.items():
                # 对特定的大字段进行截断
                if (
                    key in ("encodedImage", "base64", "imageData", "data")
                    and isinstance(value, str)
                    and len(value) > max_length
                ):
                    result[key] = (
                        f"{value[:100]}... (truncated, total {len(value)} chars)"
                    )
                else:
                    result[key] = self._truncate_large_fields(value, max_length)
            return result
        elif isinstance(data, list):
            return [self._truncate_large_fields(item, max_length) for item in data]
        elif isinstance(data, str) and len(data) > 10000:
            # 对超长字符串进行截断（可能是未知的 base64 字段）
            return f"{data[:100]}... (truncated, total {len(data)} chars)"
        return data

    def log_request(
        self,
        method: str,
        url: str,
        headers: Dict[str, str],
        body: Optional[Any] = None,
        files: Optional[Dict] = None,
        proxy: Optional[str] = None,
    ):
        """Log API request details to log.txt"""

        if not config.debug_enabled or not config.debug_log_requests:
            return

        try:
            self._write_separator()
            self.logger.info(f"🔵 [REQUEST] {self._format_timestamp()}")
            self._write_separator("-")

            # Basic info
            self.logger.info(f"Method: {method}")
            self.logger.info(f"URL: {url}")

            # Headers
            self.logger.info("\n📋 Headers:")
            masked_headers = {
                key: REDACTED if _sensitive_key(str(key)) else value
                for key, value in headers.items()
            }

            for key, value in masked_headers.items():
                self.logger.info(f"  {key}: {value}")

            # Body
            if body is not None:
                self.logger.info("\n📦 Request Body:")
                if isinstance(body, (dict, list)):
                    body_str = json.dumps(body, indent=2, ensure_ascii=False)
                    self.logger.info(body_str)
                else:
                    self.logger.info(str(body))

            # Files
            if files:
                self.logger.info("\n📎 Files:")
                try:
                    if hasattr(files, "keys") and callable(
                        getattr(files, "keys", None)
                    ):
                        for key in files.keys():
                            self.logger.info(f"  {key}: <file data>")
                    else:
                        self.logger.info("  <multipart form data>")
                except (AttributeError, TypeError):
                    self.logger.info("  <binary file data>")

            # Proxy
            if proxy:
                self.logger.info(f"\n🌐 Proxy: {proxy}")

            self._write_separator()
            self.logger.info("")  # Empty line

        except Exception as e:
            self.logger.error(f"Error logging request: {e}")

    def log_response(
        self,
        status_code: int,
        headers: Dict[str, str],
        body: Any,
        duration_ms: Optional[float] = None,
    ):
        """Log API response details to log.txt"""

        if not config.debug_enabled or not config.debug_log_responses:
            return

        try:
            self._write_separator()
            self.logger.info(f"🟢 [RESPONSE] {self._format_timestamp()}")
            self._write_separator("-")

            # Status
            status_emoji = "✅" if 200 <= status_code < 300 else "❌"
            self.logger.info(f"Status: {status_code} {status_emoji}")

            # Duration
            if duration_ms is not None:
                self.logger.info(f"Duration: {duration_ms:.2f}ms")

            # Headers
            self.logger.info("\n📋 Response Headers:")
            for key, value in headers.items():
                self.logger.info(f"  {key}: {value}")

            # Body
            self.logger.info("\n📦 Response Body:")
            if isinstance(body, (dict, list)):
                # 对大字段进行截断处理
                body_to_log = self._truncate_large_fields(body)
                body_str = json.dumps(body_to_log, indent=2, ensure_ascii=False)
                self.logger.info(body_str)
            elif isinstance(body, str):
                # Try to parse as JSON
                try:
                    parsed = json.loads(body)
                    # 对大字段进行截断处理
                    parsed = self._truncate_large_fields(parsed)
                    body_str = json.dumps(parsed, indent=2, ensure_ascii=False)
                    self.logger.info(body_str)
                except:
                    # Not JSON, log as text (limit length)
                    if len(body) > 2000:
                        self.logger.info(f"{body[:2000]}... (truncated)")
                    else:
                        self.logger.info(body)
            else:
                self.logger.info(str(body))

            self._write_separator()
            self.logger.info("")  # Empty line

        except Exception as e:
            self.logger.error(f"Error logging response: {e}")

    def log_error(
        self,
        error_message: str,
        status_code: Optional[int] = None,
        response_text: Optional[str] = None,
    ):
        """Log API error details to log.txt"""

        if not config.debug_enabled:
            return

        try:
            self._write_separator()
            self.logger.info(f"🔴 [ERROR] {self._format_timestamp()}")
            self._write_separator("-")

            if status_code:
                self.logger.info(f"Status Code: {status_code}")

            self.logger.info(f"Error Message: {error_message}")

            if response_text:
                self.logger.info("\n📦 Error Response:")
                # Try to parse as JSON
                try:
                    parsed = json.loads(response_text)
                    body_str = json.dumps(parsed, indent=2, ensure_ascii=False)
                    self.logger.info(body_str)
                except:
                    # Not JSON, log as text
                    if len(response_text) > 2000:
                        self.logger.info(f"{response_text[:2000]}... (truncated)")
                    else:
                        self.logger.info(response_text)

            self._write_separator()
            self.logger.info("")  # Empty line

        except Exception as e:
            self.logger.error(f"Error logging error: {e}")

    def log_info(self, message: str):
        """Log general info message to log.txt"""
        if not config.debug_enabled:
            return
        try:
            self.logger.info(f"ℹ️  [{self._format_timestamp()}] {message}")
        except Exception as e:
            self.logger.error(f"Error logging info: {e}")

    def log_warning(self, message: str):
        """Log warning message to log.txt"""
        if not config.debug_enabled:
            return
        try:
            self.logger.warning(f"⚠️  [{self._format_timestamp()}] {message}")
        except Exception as e:
            self.logger.error(f"Error logging warning: {e}")


# Global debug logger instance
debug_logger = DebugLogger()
