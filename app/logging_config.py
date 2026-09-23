"""
Structured JSON logging.

Why JSON and not plain text: CloudWatch Logs Insights can parse JSON fields
automatically, so `filter version = "9d6a814"` works out of the box. With plain
text you are stuck grepping substrings, and you cannot aggregate - no "error
rate per version", no "p95 latency for the release we just shipped".

Every line carries the `version` field (the 7-char git SHA baked into the image
at build time). That single field is what makes per-version log filtering
possible no matter which task, instance or Availability Zone produced the line.

No extra dependency: this is ~40 lines of stdlib rather than another pinned
package to keep patched.
"""

import json
import logging
import os
import socket
import sys
from datetime import datetime, timezone

# Cached once per process. These never change during a container's life, and
# calling gethostname() on every log line is wasteful.
_HOSTNAME = socket.gethostname()


class JsonFormatter(logging.Formatter):
    """Render each log record as one JSON object on one line.

    One line per record matters: CloudWatch treats a newline as a record
    boundary, so a pretty-printed multi-line JSON object would arrive as several
    unparseable fragments.
    """

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "timestamp": datetime.now(timezone.utc)
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            # Read at log time, not import time, so tests can override it.
            "version": os.getenv("GIT_SHA", "local"),
            "env": os.getenv("APP_ENV", "local"),
            "host": _HOSTNAME,
        }

        # Structured extras are passed as a single dict to avoid colliding with
        # the reserved attribute names on LogRecord (name, msg, args, levelname...).
        extra = getattr(record, "extra_fields", None)
        if isinstance(extra, dict):
            payload.update(extra)

        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)

        # default=str so an unexpected object never crashes the logger. A log
        # call must never be able to take the application down.
        return json.dumps(payload, default=str)


def configure_logging() -> logging.Logger:
    """Point every logger at stdout with the JSON formatter.

    Containers log to stdout; the ECS awslogs driver ships whatever appears
    there to CloudWatch. There is no log file and no rotation to manage.
    """
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())

    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(os.getenv("LOG_LEVEL", "INFO").upper())

    # uvicorn installs its own handlers at startup. Without this, its lines
    # would arrive as plain text and break the "every line is JSON" guarantee
    # that the Logs Insights queries depend on.
    for name in ("uvicorn", "uvicorn.error"):
        logger = logging.getLogger(name)
        logger.handlers = [handler]
        logger.propagate = False

    # We emit our own richer request log from the middleware in main.py
    # (it includes duration and request id), so uvicorn's access log would be a
    # duplicate of every line at lower quality.
    logging.getLogger("uvicorn.access").handlers = []
    logging.getLogger("uvicorn.access").propagate = False

    return logging.getLogger("app")
