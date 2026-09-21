"""Utility helpers shared across Kippy API modules."""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Any, cast

from .const import RETURN_CODE_ERRORS, RETURN_CODES_SUCCESS, SENSITIVE_LOG_FIELDS


def _redact_tree(data: Any, sensitive: set[str]) -> Any:
    """Recursively redact sensitive fields within ``data``."""

    if isinstance(data, dict):
        return {
            key: ("***" if key in sensitive else _redact_tree(value, sensitive))
            for key, value in data.items()
        }
    if isinstance(data, list):
        return [_redact_tree(item, sensitive) for item in data]
    return data


def _redact(data: dict[str, Any], extra: set[str] | None = None) -> dict[str, Any]:
    """Return a copy of ``data`` with sensitive fields redacted."""

    sensitive = SENSITIVE_LOG_FIELDS | (extra or set())
    return cast(dict[str, Any], _redact_tree(data, sensitive))


def _redact_json(text: str) -> str:
    """Redact sensitive fields from JSON ``text`` if possible."""

    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return "<non-JSON response>"
    return json.dumps(_redact_tree(data, SENSITIVE_LOG_FIELDS))


def _decode_json(text: str) -> dict[str, Any] | None:
    """Decode ``text`` as JSON, returning ``None`` on failure."""

    try:
        data = json.loads(text)
        return data if isinstance(data, dict) else None
    except json.JSONDecodeError:
        return None


def _get_return_code(data: dict[str, Any] | None) -> int | bool | str | None:
    """Extract the API ``return`` code from ``data`` if present."""

    if not isinstance(data, dict):
        return None
    if (code := data.get("return")) is None:
        code = data.get("Result")
    if code is None:
        return None
    if isinstance(code, bool):
        return code
    if isinstance(code, int):
        return code
    if isinstance(code, str):
        try:
            return int(code)
        except ValueError:
            return code
    return None


def _return_code_error(code: Any) -> str:
    """Return a human readable error for ``code``.

    If ``code`` is unknown, include the code in the message.
    """

    if not isinstance(code, (int, str, bool)):
        return "Missing or invalid API return code"
    if (msg := RETURN_CODE_ERRORS.get(code)) is not None:
        return f"{msg} (code {code})"
    return (
        f"Unknown error code {code}"
        if isinstance(code, int)
        else "Unknown API return code"
    )


def _treat_401_as_success(path: str, data: dict[str, Any]) -> bool:
    """Determine if a 401 response should be treated as a success."""

    return_code = _get_return_code(data)
    if isinstance(return_code, bool):
        return return_code
    return return_code in RETURN_CODES_SUCCESS


def _weeks_param(start: datetime, end: datetime) -> str:
    """Return a JSON list of ISO weeks between ``start`` and ``end``."""

    weeks_list: list[dict[str, str]] = []
    current = start - timedelta(days=start.weekday())
    while current <= end:
        year, week, _ = current.isocalendar()
        entry = {"year": str(year), "number": str(week)}
        weeks_list.append(entry)
        current += timedelta(days=7)
    return json.dumps(weeks_list)


def _tz_hours(dt: datetime) -> float:
    """Return timezone offset in hours for ``dt``."""

    tz_offset = dt.utcoffset() or timedelta()
    return tz_offset.total_seconds() / 3600
