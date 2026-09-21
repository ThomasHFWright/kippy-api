"""HTTP, concurrency and date boundaries independent of Home Assistant."""

import asyncio
import hashlib
import json
import logging
import ssl
from datetime import UTC, datetime
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

import pytest
from aiohttp import ClientConnectionError, ClientSession, web
from test_api_unit import _CM, _FakeResp

from kippy_api import KippyApi, KippyAuthError, KippyConnectionError, KippyResponseError
from kippy_api._utils import (
    _decode_json,
    _get_return_code,
    _redact_json,
    _tz_hours,
    _weeks_param,
)
from kippy_api.const import LOGIN_PATH

AUTH = {"return": 0, "app_code": "code", "app_verification_code": "verification"}


def client(*responses):
    """Create a real client over a controlled HTTP response queue."""
    session = MagicMock()
    session.post.side_effect = [
        _CM(_FakeResp(status, body)) for status, body in responses
    ]
    api = KippyApi(session)
    api.cache_authentication(AUTH, credentials=("email-secret", "password-secret"))
    return api, session


@pytest.mark.parametrize("status", [200, 401])
@pytest.mark.parametrize("code", [0, True, "0", "1"])
async def test_success_codes(status, code):
    """Both wire fields and the known HTTP 401 quirk remain compatible."""
    api, session = client((status, json.dumps({"Result": code, "data": []})))
    assert await api.get_pet_kippy_list() == []
    assert session.post.call_count == 1


@pytest.mark.parametrize("code", [False, 4, 13, 999, "unexpected", {}, []])
@pytest.mark.parametrize("status", [200, 401])
async def test_non_auth_failures_do_not_refresh(code, status):
    """Known rejection codes cannot trigger a credential refresh."""
    api, session = client((status, json.dumps({"return": code})))
    # Invalid containers are equivalent to a missing code; HTTP 401 then is auth.
    if status == 401 and isinstance(code, (list, dict)):
        api._credentials = None
        error = KippyAuthError
    else:
        error = KippyResponseError
    with pytest.raises(error) as caught:
        await api.get_pet_kippy_list()
    assert caught.value.status == status
    assert session.post.call_count == 1


@pytest.mark.parametrize("body", ["[]", "null", '"string"', "{broken", "{}"])
async def test_malformed_responses(body):
    """Malformed successful HTTP responses become library response errors."""
    api, _ = client((200, body))
    with pytest.raises(KippyResponseError):
        await api.get_pet_kippy_list()


@pytest.mark.parametrize("failure", [TimeoutError, ClientConnectionError])
async def test_transport_failure_not_retried(failure):
    """An ambiguous command timeout must not send the command twice."""
    api, session = client()
    session.post.side_effect = failure("network failure")
    with pytest.raises(KippyConnectionError):
        await api.modify_kippy_settings(1, gps_on_default=True)
    assert session.post.call_count == 1
    assert session.post.call_args.kwargs["timeout"].total == 30


async def test_refresh_limit_and_payload():
    """Only one login and one retry occur when credentials keep expiring."""
    new_auth = AUTH | {"app_code": "new-code"}
    api, session = client(
        (200, '{"return": 6}'), (200, json.dumps(new_auth)), (401, "{}")
    )
    with pytest.raises(KippyAuthError):
        await api.get_pet_kippy_list()
    assert session.post.call_count == 3
    payloads = [json.loads(call.kwargs["data"]) for call in session.post.call_args_list]
    assert payloads[0]["app_code"] == "code"
    assert payloads[2]["app_code"] == "new-code"
    assert (
        payloads[1]["login_password_hash"]
        == hashlib.sha256(b"password-secret").hexdigest()
    )
    assert session.post.call_args_list[1].args[0].endswith(LOGIN_PATH)


@pytest.mark.parametrize(
    "status,body,error",
    [
        (200, '{"return": 108}', KippyAuthError),
        (200, '{"Result": false}', KippyAuthError),
        (401, "{}", KippyAuthError),
        (403, "{}", KippyAuthError),
        (403, '{"return": 4}', KippyResponseError),
        (500, '{"return": 108}', KippyResponseError),
        (500, "<html>failure</html>", KippyResponseError),
        (200, '{"return": 4}', KippyResponseError),
        (200, '{"return": 0}', KippyResponseError),
    ],
)
async def test_login_classification(status, body, error):
    """Login rejects credentials distinctly from invalid server responses."""
    api, session = client((status, body))
    with pytest.raises(error):
        await api.login("email", "password", force=True)
    assert api.app_code is None
    assert session.post.call_count == 1


async def test_login_cache_and_missing_auth():
    """Cached tokens work without retaining credentials; missing tokens fail safely."""
    api, session = client()
    assert await api.login("email", "password") == AUTH
    session.post.assert_not_called()
    api.cache_authentication({})
    with pytest.raises(KippyAuthError):
        await api.get_pet_kippy_list()


async def test_concurrent_refresh_uses_one_login():
    """Two expired requests refresh once and reuse the same new token pair."""
    api, _ = client()
    expired = 0
    both_expired = asyncio.Event()
    logins = 0

    async def post(path, payload, _headers):
        nonlocal expired, logins
        if path == LOGIN_PATH:
            logins += 1
            await asyncio.sleep(0)
            return AUTH | {"app_code": "new-code"}
        if payload["app_code"] == "code":
            expired += 1
            if expired == 2:
                both_expired.set()
            await both_expired.wait()
            raise KippyAuthError("Expired", return_code=6)
        return {"return": 0, "data": []}

    api._post = post
    results = await asyncio.gather(api.get_pet_kippy_list(), api.get_pet_kippy_list())
    assert results == [[], []]
    assert logins == 1


@pytest.mark.parametrize(
    "tz,start,end,offset,hours",
    [
        ("Europe/London", "2025-03-30", "2025-03-31", 0, 23),
        ("Europe/London", "2025-10-26", "2025-10-27", 1, 25),
        ("Asia/Kathmandu", "2025-01-01", "2025-01-02", 5.75, 24),
    ],
)
async def test_activity_timezone(tz, start, end, offset, hours):
    """Calendar midnights reflect DST and fractional UTC offsets."""
    api, session = client(
        (200, '{"return": 0, "ActivitiesData": [1], "AVGData": 2, "HealthData": 3}')
    )
    result = await api.get_activity_categories(
        42, start, end, 2, 1, timezone=ZoneInfo(tz)
    )
    payload = json.loads(session.post.call_args.kwargs["data"])
    assert payload["timezone"] == offset
    assert payload["toDate"] - payload["fromDate"] == hours * 3600
    assert payload["timeDivisions"] == "d"
    assert result == {"activities": [1], "avg": 2, "health": 3}


async def test_activity_shapes_and_validation():
    """Activity accepts nested data and rejects invalid dates/timezones/shapes."""
    api, _ = client(
        (200, '{"return": 0, "data": {"activities": [1]}}'),
        (200, '{"return": 0, "data": [1]}'),
    )
    result = await api.get_activity_categories(
        1, "2025-01-01", "2025-01-02", 3, 1, timezone=UTC
    )
    assert result == {"activities": [1], "avg": None, "health": None}
    with pytest.raises(KippyResponseError):
        await api.get_activity_categories(
            1, "2025-01-01", "2025-01-02", 3, 1, timezone=UTC
        )
    with pytest.raises(ValueError):
        await api.get_activity_categories(
            1, "2025-01-02", "2025-01-01", 1, 1, timezone=UTC
        )
    with pytest.raises(ValueError):
        await api.get_activity_categories(
            1, "2025-01-01", "2025-01-02", 1, 1, timezone=None
        )


@pytest.mark.parametrize("nested", [True, False])
async def test_map_normalization(nested):
    """Normalize both vendor response shapes without filtering location sources."""
    data = {
        "lat": 1.2,
        "lng": 3.4,
        "radius": 5,
        "altitude": 6,
        "localization_tecnology": 1,
    }
    api, session = client(
        (
            200,
            json.dumps(
                {"return": 0, "data": data} if nested else {"return": 0, **data}
            ),
        )
    )
    result = await api.kippymap_action(42, do_sms=False, app_action=2, geofence_id=7)
    assert result["gps_latitude"] == 1.2
    assert result["gps_longitude"] == 3.4
    assert result["gps_accuracy"] == 5
    assert result["gps_altitude"] == 6
    assert result["localization_technology"] == "LBS (Low accuracy)"
    payload = json.loads(session.post.call_args.kwargs["data"])
    assert (payload["do_sms"], payload["app_action"], payload["geofence_id"]) == (
        0,
        2,
        7,
    )


async def test_pets_multiple_and_settings():
    """Preserve every pet, subscription metadata and device setting serialization."""
    pets = [
        {"petID": 1, "enableGPSOnDefault": "1", "subscription_expired": True},
        {"petID": 2, "enableGPSOnDefault": "false"},
        {"petID": 3, "enableGPSOnDefault": "true"},
    ]
    api, session = client(
        (200, json.dumps({"return": 0, "data": pets})), (200, '{"return": 0}')
    )
    result = await api.get_pet_kippy_list()
    assert [pet["gpsOnDefault"] for pet in result] == [1, 0, 1]
    assert result[0]["subscription_expired"] is True
    await api.modify_kippy_settings(42, energy_saving_mode=True, update_frequency=2.26)
    payload = json.loads(session.post.call_args.kwargs["data"])
    assert payload["energy_saving_mode"] == 1
    assert payload["update_frequency"] == 2.3


@pytest.mark.parametrize("pets", [None, {}, [None]])
async def test_invalid_pet_list(pets):
    """Unexpected pet structures raise a library error, not an attribute error."""
    api, _ = client((200, json.dumps({"return": 0, "data": pets})))
    with pytest.raises(KippyResponseError):
        await api.get_pet_kippy_list()


async def test_tls_and_session_ownership():
    """Creating the API retains verified TLS and leaves its borrowed session open."""
    async with ClientSession() as session:
        api = await KippyApi.async_create(session)
        assert api.session is session
        assert api._ssl_context.check_hostname
        assert api._ssl_context.verify_mode == ssl.CERT_REQUIRED
        assert not hasattr(api, "close")
        assert not session.closed
    assert session.closed


async def test_no_payload_logging(caplog):
    """Even unknown return codes and arbitrary response fields stay out of logs."""
    api, _ = client(
        (200, '{"return": "SECRET-CODE", "lat": "SECRET-LAT", "email": "SECRET-EMAIL"}')
    )
    caplog.set_level(logging.DEBUG, logger="kippy_api")
    with pytest.raises(KippyResponseError) as caught:
        await api.get_pet_kippy_list()
    assert "SECRET" not in caplog.text
    assert "SECRET" not in str(caught.value)
    assert caught.value.return_code == "SECRET-CODE"


async def test_real_http_session():
    """Exercise aiohttp itself, including headers and successful HTTP 401 bodies."""

    async def endpoint(request):
        assert request.headers["Content-Type"] == "text/plain; charset=utf-8"
        payload = json.loads(await request.text())
        assert payload["app_code"] == "code"
        return web.json_response({"Result": True, "data": []}, status=401)

    app = web.Application()
    app.router.add_post("/v2/GetPetKippyList.php", endpoint)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    try:
        port = runner.addresses[0][1]
        async with ClientSession(raise_for_status=True) as session:
            api = KippyApi(session, host=f"http://127.0.0.1:{port}")
            api.cache_authentication(AUTH)
            assert await api.get_pet_kippy_list() == []
            assert not session.closed
    finally:
        await runner.cleanup()


def test_helper_boundaries():
    """JSON validation and ISO week boundaries use the protocol's exact forms."""
    assert _decode_json("[]") is None
    assert _decode_json("null") is None
    assert _get_return_code({"return": 1.5}) is None
    assert _get_return_code({"Result": {}}) is None
    assert _get_return_code({"Result": "unknown"}) == "unknown"
    assert _tz_hours(datetime(2025, 1, 1)) == 0
    assert _redact_json("password=SECRET") == "<non-JSON response>"
    assert json.loads(_weeks_param(datetime(2020, 12, 31), datetime(2021, 1, 5))) == [
        {"year": "2020", "number": "53"},
        {"year": "2021", "number": "1"},
    ]
