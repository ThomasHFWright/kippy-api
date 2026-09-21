"""API endpoint for retrieving activity statistics."""

from __future__ import annotations

from datetime import datetime, tzinfo
from typing import Any

from ._base import BaseKippyApi
from ._utils import _tz_hours, _weeks_param
from .const import (
    ACTIVITY_ID,
    FORMULA_GROUP,
    GET_ACTIVITY_CATEGORIES_PATH,
    REQUEST_HEADERS,
    T_ID,
)
from .exceptions import KippyResponseError


class ActivityEndpoint(BaseKippyApi):
    """Mixin implementing the activity category endpoint."""

    async def get_activity_categories(
        self,
        pet_id: int | str,
        from_date: str,
        to_date: str,
        time_division: int,
        _weeks: int,
        *,
        timezone: tzinfo,
    ) -> dict[str, Any]:
        """Retrieve activity categories for a pet."""

        start = datetime.strptime(from_date, "%Y-%m-%d")
        end = datetime.strptime(to_date, "%Y-%m-%d")

        if not isinstance(timezone, tzinfo):
            raise ValueError("timezone must be a tzinfo instance")
        if end < start:
            raise ValueError("to_date must not precede from_date")
        start_ts = int(start.replace(tzinfo=timezone).timestamp())
        end_ts = int(end.replace(tzinfo=timezone).timestamp())

        tz_hours_value = _tz_hours(start.replace(tzinfo=timezone))
        weeks_value = _weeks_param(start, end)

        time_divisions = {1: "h", 2: "d", 3: "w"}.get(time_division, "h")

        payload = await self._authenticated_payload(
            extra={
                "petID": pet_id,
                "activityID": ACTIVITY_ID.ALL,
                "fromDate": start_ts,
                "toDate": end_ts,
                "timeDivisions": time_divisions,
                "formulaGroup": FORMULA_GROUP.SUM,
                "tID": T_ID,
                "timezone": tz_hours_value,
                "weeks": weeks_value,
            }
        )

        data = await self.post_with_refresh(
            GET_ACTIVITY_CATEGORIES_PATH, payload, REQUEST_HEADERS
        )

        if "data" in data:
            payload = data.get("data") or {}
            if not isinstance(payload, dict):
                raise KippyResponseError("Activity data must be an object")
        else:
            payload = {
                "activities": data.get("ActivitiesData"),
                "avg": data.get("AVGData"),
                "health": data.get("HealthData"),
            }

        return {
            "activities": payload.get("activities"),
            "avg": payload.get("avg"),
            "health": payload.get("health"),
        }
