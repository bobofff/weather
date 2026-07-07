"""Project-local runtime logging helpers."""

from __future__ import annotations

import json
import traceback
from collections.abc import Mapping
from datetime import datetime
from typing import Any

from weather_quant.paths import LOG_DIR


def _serialize_error(error: BaseException | None) -> dict[str, Any]:
    if error is None:
        return {}
    return {
        "error": str(error),
        "errorType": error.__class__.__name__,
        "traceback": "".join(
            traceback.format_exception(type(error), error, error.__traceback__)
        ).strip(),
    }


def _event_payload(
    *,
    level: str,
    source: str,
    action: str,
    status: str,
    timestamp: datetime,
    details: Mapping[str, Any] | None = None,
    result: Mapping[str, Any] | None = None,
    error: BaseException | None = None,
) -> dict[str, Any]:
    payload = {
        "timestamp": timestamp.isoformat(),
        "level": level.upper(),
        "source": source,
        "action": action,
        "status": status,
        "details": dict(details or {}),
        "result": dict(result or {}),
    }
    payload.update(_serialize_error(error))
    return payload


def _append_json_line(path_name: str, payload: Mapping[str, Any]) -> None:
    with (LOG_DIR / path_name).open("a", encoding="utf-8") as file:
        file.write(json.dumps(payload, ensure_ascii=False, default=str) + "\n")


def log_event(
    *,
    level: str,
    source: str,
    action: str,
    status: str,
    details: Mapping[str, Any] | None = None,
    result: Mapping[str, Any] | None = None,
    error: BaseException | None = None,
) -> None:
    """Append a best-effort JSON log event without coupling to a parent app."""

    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now().astimezone()
        payload = _event_payload(
            level=level,
            source=source,
            action=action,
            status=status,
            timestamp=timestamp,
            details=details,
            result=result,
            error=error,
        )
        daily_name = f"{timestamp.date().isoformat()}.log"
        _append_json_line(daily_name, payload)
        _append_json_line("weather_runtime.jsonl", payload)
    except OSError:
        return


def log_sync_event(
    *,
    source: str,
    action: str,
    status: str,
    details: Mapping[str, Any] | None = None,
    result: Mapping[str, Any] | None = None,
    error: BaseException | None = None,
) -> None:
    """Append a structured runtime event."""

    level = "ERROR" if status.lower() in {"fail", "error"} else "INFO"
    log_event(
        level=level,
        source=source,
        action=action,
        status=status,
        details=details,
        result=result,
        error=error,
    )


def log_external_api_failure(
    *,
    provider: str,
    action: str,
    endpoint: str | None = None,
    details: Mapping[str, Any] | None = None,
    error: BaseException | None = None,
) -> None:
    """Record a failed call to a third-party provider."""

    event_details: dict[str, Any] = dict(details or {})
    event_details["provider"] = provider
    if endpoint:
        event_details["endpoint"] = endpoint
    log_event(
        level="ERROR",
        source="external_api",
        action=action,
        status="fail",
        details=event_details,
        error=error,
    )
