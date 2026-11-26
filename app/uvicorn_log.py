"""Custom Uvicorn access log filter to suppress repetitive 200 GET /health probes."""

from __future__ import annotations

import logging


class HealthCheckAccessFilter(logging.Filter):
    """Filter out noisy HTTP 200 `GET /health` access log records while keeping errors and all other traffic."""

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        if isinstance(args, tuple) and len(args) >= 5:
            method = str(args[1]).upper()
            path = str(args[2]).split("?", 1)[0]
            try:
                status_code = int(args[4])
            except (TypeError, ValueError):
                status_code = 0
            if method == "GET" and path == "/health" and status_code == 200:
                return False
            return True

        msg = record.getMessage()
        if '"GET /health HTTP/' in msg and msg.rstrip().endswith(" 200"):
            return False
        return True
