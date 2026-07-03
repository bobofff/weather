"""Tiny local runtime-log shim for the standalone weather project."""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any

from weather_quant.paths import LOG_DIR


def log_sync_event(
    *,
    source: str,
    action: str,
    status: str,
    details: Mapping[str, Any] | None = None,
    result: Mapping[str, Any] | None = None,
    error: BaseException | None = None,
) -> None:
    """Append a best-effort JSONL event without coupling to a parent app."""

    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        payload = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "source": source,
            "action": action,
            "status": status,
            "details": dict(details or {}),
            "result": dict(result or {}),
            "error": str(error) if error else None,
        }
        with (LOG_DIR / "weather_runtime.jsonl").open("a", encoding="utf-8") as file:
            file.write(json.dumps(payload, ensure_ascii=False, default=str) + "\n")
    except OSError:
        return
