import json
import logging
import sys
from datetime import UTC, datetime
from typing import Any


class JSONFormatter(logging.Formatter):
    """Formats log records as single-line JSON objects."""

    def format(self, record: logging.LogRecord) -> str:
        log_entry: dict[str, Any] = {
            "timestamp": datetime.now(UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            log_entry["exception"] = self.formatException(record.exc_info)
        # Include extra attributes if provided, redacting any sensitive auth tokens
        for key, val in record.__dict__.items():
            if key not in {
                "name",
                "msg",
                "args",
                "levelname",
                "levelno",
                "pathname",
                "filename",
                "module",
                "exc_info",
                "exc_text",
                "stack_info",
                "lineno",
                "funcName",
                "created",
                "msecs",
                "relativeCreated",
                "thread",
                "threadName",
                "processName",
                "process",
                "message",
            }:
                if "token" in key.lower() or "secret" in key.lower() or "auth" in key.lower():
                    log_entry[key] = "[REDACTED]"
                else:
                    try:
                        json.dumps(val)
                        log_entry[key] = val
                    except (TypeError, OverflowError):
                        log_entry[key] = str(val)
        return json.dumps(log_entry)


def setup_logging(log_level: str = "INFO") -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JSONFormatter())
    root_logger = logging.getLogger()
    root_logger.setLevel(log_level)
    # Clear existing handlers to prevent duplicate outputs
    root_logger.handlers = [handler]


logger = logging.getLogger("agent_arena")
