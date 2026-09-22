"""Cognito/AppSync client for the new Kippy app, independent of the legacy API.

Protocol operations adapted from IamDiesel/kippy-homeassistant-lola, commit
048ca22a3693276ca96d93310fdca15346ba3c78 (MIT, Daniel Kahrizi and Thomas Wright).
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from time import monotonic
from typing import Any

from aiohttp import ClientError, ClientSession, ClientTimeout

from ._utils import _decode_json
from .exceptions import KippyAuthError, KippyConnectionError, KippyResponseError

COGNITO_ENDPOINT = "https://cognito-idp.eu-west-1.amazonaws.com/"
COGNITO_CLIENT_ID = "57bn1c33eu2r5libvqvhfnv4qb"
APPSYNC_ENDPOINT = (
    "https://l2nea6uaizdn3j3h2ex7frcqhy.appsync-api.eu-west-1.amazonaws.com/graphql"
)

GET_USER = """query getUser { getUser { code user { id migrated } } }"""
GET_PETS = (
    """query getPets { getPets { code pets { id name species image { url } } } }"""
)
GET_PRODUCTS = """query getProducts($petId: String!) {
  getProducts(petId: $petId) { code products {
    id entityType serialNumber deviceType subscriptionIsActive
    lastKnownStatus { energySavingMode firmwareVersion }
  } }
}"""
GET_GPS = """query getPetlinkGps($id: String!) {
  getPetlinkGps(id: $id) { code petlinkGps {
    settings {
      updateFrequency enableGpsOnDefault activityProfile optimizationDone
      sentinelMigrationDone migrationWaitingForConnection
    }
    lastKnownPosition { lat lng alt radius precision speed positionType date }
    lastKnownStatus {
      battery charging flashlight sound liveTracking geofence inGeofence
      energySavingMode inEnergySavingZone tourRecording offline shutdown
      firmwareVersion date
    }
    newFirmwareVersion { version url }
    logEnabled endOfLifeDevice
  } }
}"""
GET_PETS_AND_PRODUCTS = """query getPetsAndProducts {
  getPetsAndProducts { code
    pets { id name species image { url } }
    products {
      id petId entityType serialNumber deviceType subscriptionIsActive
      endOfLifeDevice lastKnownStatus { energySavingMode firmwareVersion }
    }
  }
}"""
GET_SUBSCRIPTION = """query getSubscriptionByProductId($productId: String!) {
  getSubscriptionByProductId(productId: $productId) { code subscription {
    id status currentTermStart currentTermEnd nextBillingAt
  } }
}"""
GET_ACTIVITY = """query getActivitiesCat(
  $petId: String!, $weekIndex: Int!, $from: Int!, $to: Int!
) {
  getActivitiesCat(petId: $petId, weekIndex: $weekIndex, from: $from, to: $to) {
    code activityReport {
      walk { value } sleep { value } calories { value } steps { value }
      feed { value } jumps { value } onTheMove { value }
      highMovement { value } grooming { value }
    }
  }
}"""
GET_HISTORY = """query getPositionsHistory($petId: String!, $from: String, $to: String) {
  getPositionsHistory(petId: $petId, from: $from, to: $to) {
    code positions { lat lng radius precision positionType date isSkip }
  }
}"""
GET_ACTIVITIES = """query getActivities($petId: String!, $from: Int!, $to: Int!) {
  getActivities(petId: $petId, from: $from, to: $to) {
    code activityReport {
      walk { value goal } sleep { value goal } calories { value goal }
      steps { value goal } play { value goal } run { value goal } onTheMove { value goal }
    }
  }
}"""
GET_ACTIVITIES_BY_HOUR = """query getActivitiesByHour($petId: String!, $from: Int!, $to: Int!) {
  getActivitiesByHour(petId: $petId, from: $from, to: $to) {
    code activities {
      timestamp walk sleep steps calories onTheMove play run feed jumps highMovement grooming
    }
  }
}"""
GET_GEOFENCES = """query getGeofences {
  getGeofences { code geofences { id name position { lat lng } devices } }
}"""
GET_ENERGY_SAVING_ZONES = """query getEnergySavingZones {
  getEnergySavingZones { code energySavingZones {
    id name icon ssid bssid position { lat lng } radius
  } }
}"""
GET_PET_HISTORY = """query getPetHistory($petId: String) {
  getPetHistory(petId: $petId) { code petHistory {
    id eventType date read extra { serialNumber newSerialNumber address }
  } }
}"""
UPDATE_GPS = """mutation updatePetlinkGps($petlinkGps: UpdatePetlinkGpsIn!) {
  updatePetlinkGps(petlinkGps: $petlinkGps) { code message }
}"""
SEND_COMMAND = """mutation sendCommand($command: Command!) {
  sendCommand(command: $command) { code message }
}"""
KEEP_ALIVE = """mutation appKeepAlive($productIds: [String!]!) {
  appKeepAlive(productIds: $productIds) { code message }
}"""
SEND_SETTING = """mutation sendSetting($setting: Setting!) {
  sendSetting(setting: $setting) { code message }
}"""

_AUTH_ERRORS = {"NotAuthorizedException", "UserNotFoundException"}
_ACCOUNT_ERRORS = {"UserNotConfirmedException", "PasswordResetRequiredException"}


class KippyGraphQLApi:
    """Native new-app operations; readers never issue tracker commands.

    The caller owns the session. Return values retain new-API field names and
    IDs; weekly reports are not presented as legacy daily activity data.
    """

    backend = "graphql"

    def __init__(self, session: ClientSession) -> None:
        """Borrow a session with normal verified TLS for AWS endpoints."""
        self.session = session
        self._auth: dict[str, Any] | None = None
        self._credentials: tuple[str, str] | None = None
        self._login_lock = asyncio.Lock()
        self._expires_at = 0.0

    async def _post(
        self, endpoint: str, payload: dict[str, Any], headers: dict[str, str]
    ) -> dict[str, Any]:
        """Bound requests and reject server errors without retaining payloads."""
        try:
            async with self.session.post(
                endpoint,
                json=payload,
                headers=headers,
                ssl=True,
                timeout=ClientTimeout(total=30),
                allow_redirects=False,
                raise_for_status=False,
            ) as response:
                text = await response.text()
                status = response.status
        except (ClientError, TimeoutError) as err:
            raise KippyConnectionError("Request failed or timed out") from err
        data = _decode_json(text)
        code = data.get("__type", "") if data else ""
        code = code.rsplit("#", 1)[-1] if isinstance(code, str) else ""
        # AWS service/throttling failures must never masquerade as bad passwords.
        if status == 429 or status >= 500:
            raise KippyResponseError("Service unavailable or throttled", status=status)
        if code in _AUTH_ERRORS | _ACCOUNT_ERRORS or status == 401:
            raise KippyAuthError(
                "New-app authentication failed", status=status, return_code=code or None
            )
        if not 200 <= status < 300:
            raise KippyResponseError("Server rejected request", status=status)
        if data is None:
            raise KippyResponseError("Response is not a JSON object", status=status)
        return data

    async def login(
        self, email: str, password: str, force: bool = False
    ) -> dict[str, Any]:
        """Authenticate with Cognito; surface MFA/account challenges explicitly."""
        async with self._login_lock:
            if self._auth is not None and not force and monotonic() < self._expires_at:
                return self._auth
            self._auth = None
            result = await self._post(
                COGNITO_ENDPOINT,
                {
                    "AuthFlow": "USER_PASSWORD_AUTH",
                    "ClientId": COGNITO_CLIENT_ID,
                    "AuthParameters": {"USERNAME": email, "PASSWORD": password},
                },
                {
                    "X-Amz-Target": "AWSCognitoIdentityProviderService.InitiateAuth",
                    "Content-Type": "application/x-amz-json-1.1",
                },
            )
            if result.get("ChallengeName"):
                raise KippyAuthError("Additional new-app authentication is required")
            auth = result.get("AuthenticationResult")
            if not isinstance(auth, dict) or not all(
                isinstance(auth.get(key), str) and auth[key]
                for key in ("IdToken", "AccessToken")
            ):
                raise KippyResponseError("Login response is missing tokens")
            lifetime = auth.get("ExpiresIn")
            if type(lifetime) is not int or lifetime <= 0:
                raise KippyResponseError("Login response is missing token lifetime")
            self._expires_at = monotonic() + lifetime - min(30, lifetime / 10)
            self._auth = auth
            self._credentials = (email, password)
            return auth

    async def execute_graphql(
        self, query: str, variables: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Execute once, retrying only an HTTP 401 before GraphQL execution.

        GraphQL errors (including partial data) fail explicitly and are never
        replayed: a mutation may already have had a side effect.
        """
        if self._auth is None or monotonic() >= self._expires_at:
            if self._credentials is None:
                raise KippyAuthError("No credentials available")
            await self.login(*self._credentials)
        for attempt in range(2):
            auth = self._auth
            assert auth is not None
            try:
                result = await self._post(
                    APPSYNC_ENDPOINT,
                    {"query": query, "variables": variables or {}},
                    {
                        "Authorization": f"Bearer {auth['IdToken']}",
                        "Content-Type": "application/json",
                    },
                )
            except KippyAuthError as err:
                if err.status != 401 or attempt or self._credentials is None:
                    raise
                # Reuse the first concurrent refresh rather than logging in per request.
                if self._auth is auth:
                    self._auth = None
                await self.login(*self._credentials)
                continue
            if result.get("errors"):
                raise KippyResponseError("GraphQL operation returned errors")
            data = result.get("data")
            if not isinstance(data, dict):
                raise KippyResponseError("GraphQL response is missing data")
            return data
        raise AssertionError("Unreachable retry state")

    async def _operation(
        self, query: str, operation: str, variables: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Require an operation result instead of treating missing data as empty."""
        data = await self.execute_graphql(query, variables)
        result = data.get(operation)
        if not isinstance(result, dict):
            raise KippyResponseError("GraphQL operation result is missing")
        code = result.get("code")
        if code != "200":
            raise KippyResponseError(
                "GraphQL operation was rejected",
                return_code=code if isinstance(code, (str, int, bool)) else None,
            )
        return result

    async def get_user(self) -> dict[str, Any]:
        """Read the native account ID and migrated flag without personal profile fields."""
        result = await self._operation(GET_USER, "getUser")
        user = result.get("user")
        if not isinstance(user, dict):
            raise KippyResponseError("Account data is missing")
        return user

    async def get_pets(self) -> list[dict[str, Any]]:
        """Return all pets with native string IDs, including pets without trackers."""
        result = await self._operation(GET_PETS, "getPets")
        return _objects(result.get("pets"))

    async def get_products(self, pet_id: str) -> list[dict[str, Any]]:
        """Return every product; do not silently discard unfamiliar device types."""
        result = await self._operation(
            GET_PRODUCTS, "getProducts", {"petId": _identifier(pet_id)}
        )
        return _objects(result.get("products"))

    async def get_pets_and_products(self) -> dict[str, list[dict[str, Any]]]:
        """Read every pet and product in one round trip; products carry ``petId``."""
        result = await self._operation(GET_PETS_AND_PRODUCTS, "getPetsAndProducts")
        return {key: _objects(result.get(key)) for key in ("pets", "products")}

    async def get_subscription(self, product_id: str) -> dict[str, Any]:
        """Read a tracker's plan status and term dates (replaces legacy expiry days)."""
        result = await self._operation(
            GET_SUBSCRIPTION,
            "getSubscriptionByProductId",
            {"productId": _identifier(product_id)},
        )
        subscription = result.get("subscription")
        if not isinstance(subscription, dict):
            raise KippyResponseError("Subscription data is missing")
        return subscription

    async def get_petlink_gps(self, product_id: str) -> dict[str, Any]:
        """Read cached position, status and settings without waking the tracker."""
        result = await self._operation(
            GET_GPS, "getPetlinkGps", {"id": _identifier(product_id)}
        )
        gps = result.get("petlinkGps")
        if not isinstance(gps, dict):
            raise KippyResponseError("Tracker data is missing")
        return gps

    async def get_cat_activity_report(
        self, pet_id: str, start: datetime, end: datetime
    ) -> dict[str, Any]:
        """Read the cat activity report using caller-supplied aware boundaries.

        The backend's aggregation semantics are undocumented. Preserve its report
        verbatim; never manufacture daily measurements or activity aliases.
        """
        _date_range(start, end)
        if start.isocalendar()[:2] != end.astimezone(start.tzinfo).isocalendar()[:2]:
            raise ValueError("Activity range must stay within one ISO week")
        year, week, _ = start.isocalendar()
        result = await self._operation(
            GET_ACTIVITY,
            "getActivitiesCat",
            {
                "petId": _identifier(pet_id),
                "weekIndex": year * 100 + week,
                "from": int(start.timestamp()),
                "to": int(end.timestamp()),
            },
        )
        report = result.get("activityReport")
        if not isinstance(report, dict):
            raise KippyResponseError("Activity report is missing")
        return report

    async def get_activity_report(
        self, pet_id: str, start: datetime, end: datetime
    ) -> dict[str, Any]:
        """Read the general (non-cat-specific) report for explicit date boundaries."""
        _date_range(start, end)
        result = await self._operation(
            GET_ACTIVITIES,
            "getActivities",
            {
                "petId": _identifier(pet_id),
                "from": int(start.timestamp()),
                "to": int(end.timestamp()),
            },
        )
        report = result.get("activityReport")
        if not isinstance(report, dict):
            raise KippyResponseError("Activity report is missing")
        return report

    async def get_activities_by_hour(
        self, pet_id: str, start: datetime, end: datetime
    ) -> list[dict[str, Any]]:
        """Read timestamped activity buckets without fabricating daily totals."""
        _date_range(start, end)
        result = await self._operation(
            GET_ACTIVITIES_BY_HOUR,
            "getActivitiesByHour",
            {
                "petId": _identifier(pet_id),
                "from": int(start.timestamp()),
                "to": int(end.timestamp()),
            },
        )
        return _objects(result.get("activities"))

    async def get_positions_history(
        self, pet_id: str, start: datetime, end: datetime
    ) -> list[dict[str, Any]]:
        """Read location history for explicit aware dates, preserving raw fixes."""
        _date_range(start, end)
        result = await self._operation(
            GET_HISTORY,
            "getPositionsHistory",
            {
                "petId": _identifier(pet_id),
                "from": start.astimezone(UTC)
                .isoformat(timespec="milliseconds")
                .replace("+00:00", "Z"),
                "to": end.astimezone(UTC)
                .isoformat(timespec="milliseconds")
                .replace("+00:00", "Z"),
            },
        )
        return _objects(result.get("positions"))

    async def get_geofences(self) -> list[dict[str, Any]]:
        """Read the account's geofences (polygon ``position`` lists, assigned ``devices``)."""
        result = await self._operation(GET_GEOFENCES, "getGeofences")
        return _objects(result.get("geofences"))

    async def get_energy_saving_zones(self) -> list[dict[str, Any]]:
        """Read Wi-Fi energy-saving zones (``ssid``/``bssid`` with a centre and radius)."""
        result = await self._operation(GET_ENERGY_SAVING_ZONES, "getEnergySavingZones")
        return _objects(result.get("energySavingZones"))

    async def get_pet_history(self, pet_id: str) -> list[dict[str, Any]]:
        """Read the pet event feed (geofence, energy-saving, battery, device events)."""
        result = await self._operation(
            GET_PET_HISTORY, "getPetHistory", {"petId": _identifier(pet_id)}
        )
        return _objects(result.get("petHistory"))

    async def update_petlink_gps(
        self,
        product_id: str,
        *,
        update_frequency: int | None = None,
        enable_gps_on_default: bool | None = None,
        activity_profile: str | None = None,
    ) -> dict[str, Any]:
        """Write tracker settings; only the given fields are sent."""
        settings: dict[str, Any] = {}
        if update_frequency is not None:
            settings["updateFrequency"] = int(update_frequency)
        if enable_gps_on_default is not None:
            settings["enableGpsOnDefault"] = bool(enable_gps_on_default)
        if activity_profile is not None:
            settings["activityProfile"] = _identifier(activity_profile)
        if not settings:
            raise ValueError("At least one setting is required")
        return await self._operation(
            UPDATE_GPS,
            "updatePetlinkGps",
            {"petlinkGps": {"id": _identifier(product_id), "settings": settings}},
        )

    async def start_live_tracking(
        self, product_id: str, duration: int | None = None
    ) -> dict[str, Any]:
        """Request live tracking; status moves REQUESTED -> ON as the tracker reconnects."""
        command: dict[str, Any] = {
            "commandType": "LIVE_TRACKING",
            "id": _identifier(product_id),
        }
        if duration is not None:
            if int(duration) <= 0:
                raise ValueError("Use stop_live_tracking to end a session")
            command["duration"] = int(duration)
        return await self.send_command(command)

    async def stop_live_tracking(self, product_id: str) -> dict[str, Any]:
        """End live tracking; the service reports OFF within seconds (duration 0)."""
        return await self.send_command(
            {
                "commandType": "LIVE_TRACKING",
                "id": _identifier(product_id),
                "duration": 0,
            }
        )

    async def send_command(self, command: dict[str, Any]) -> dict[str, Any]:
        """Send an explicit native Command input and reject non-200 result codes.

        Acceptance by the service does not confirm execution on the tracker.
        """
        _identifier(command.get("id"))
        _identifier(command.get("commandType"))
        return await self._operation(SEND_COMMAND, "sendCommand", {"command": command})

    async def app_keep_alive(self, product_ids: list[str]) -> dict[str, Any]:
        """Explicitly maintain live tracking; ordinary reads do not call this."""
        if not product_ids:
            raise ValueError("At least one product ID is required")
        return await self._operation(
            KEEP_ALIVE,
            "appKeepAlive",
            {"productIds": [_identifier(p) for p in product_ids]},
        )

    async def send_setting(self, setting: dict[str, Any]) -> dict[str, Any]:
        """Send an explicit Setting input without inventing companion defaults.

        Acceptance by the service does not confirm execution on the tracker.
        """
        for field in ("settingType", "operationType"):
            _identifier(setting.get(field))
        if "deviceId" in setting:
            _identifier(setting["deviceId"])
        return await self._operation(SEND_SETTING, "sendSetting", {"setting": setting})


def _identifier(value: Any) -> str:
    """Require a nonempty native ID or enum before sending a request."""
    if not isinstance(value, str) or not value.strip():
        raise ValueError("Expected a nonempty string")
    return value


def _objects(value: Any) -> list[dict[str, Any]]:
    """Validate vendor collection boundaries without discarding malformed entries."""
    if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
        raise KippyResponseError("Expected a list of objects")
    return value


def _date_range(start: datetime, end: datetime) -> None:
    """Reject ambiguous local times and reversed ranges before making requests."""
    if start.utcoffset() is None or end.utcoffset() is None:
        raise ValueError("Timezone-aware dates are required")
    if end.timestamp() < start.timestamp():
        raise ValueError("End must not precede start")
