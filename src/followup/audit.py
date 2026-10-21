"""Logging: readable console lines for people, JSON lines for the audit trail.

Every module reports through :func:`emit`, so one call produces both a console line and a
structured ``audit.jsonl`` record tagged with the run id.
"""

from __future__ import annotations

import json
import logging
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

LOGGER_NAME = "followup"
log = logging.getLogger(LOGGER_NAME)


def emit(event: str, message: str, *, level: int = logging.INFO, **fields: Any) -> None:
    log.log(level, message, extra={"event": event, "fields": fields})


class _RunId(logging.Filter):
    def __init__(self, run_id: str):
        super().__init__()
        self.run_id = run_id

    def filter(self, record: logging.LogRecord) -> bool:
        record.run_id = self.run_id
        return True


class JsonLinesFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
            "run_id": getattr(record, "run_id", None),
            "level": record.levelname,
            "event": getattr(record, "event", "log"),
            "message": record.getMessage(),
            **getattr(record, "fields", {}),
        }
        return json.dumps(payload, ensure_ascii=False, default=str)


@contextmanager
def run_logging(audit_path: Path, run_id: str) -> Iterator[None]:
    """Console + append-only audit file for the duration of one run."""
    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    run_filter = _RunId(run_id)

    console = logging.StreamHandler(sys.stdout)
    console.setLevel(logging.INFO)
    console.setFormatter(
        logging.Formatter("%(asctime)s | %(levelname)-7s | %(message)s", "%H:%M:%S")
    )
    audit_path.parent.mkdir(parents=True, exist_ok=True)
    audit = logging.FileHandler(audit_path, mode="a", encoding="utf-8")
    audit.setLevel(logging.DEBUG)
    audit.setFormatter(JsonLinesFormatter())

    handlers = [console, audit]
    for handler in handlers:
        handler.addFilter(run_filter)
        logger.addHandler(handler)
    try:
        yield
    finally:
        for handler in handlers:
            logger.removeHandler(handler)
            handler.close()
        logger.propagate = True
