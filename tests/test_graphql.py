"""New-app transport and account selection without credentials or device writes."""

import asyncio
import json
import logging
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch
from zoneinfo import ZoneInfo

import pytest
from aiohttp import ClientConnectionError, ClientSession, web
from test_api_unit import _CM, _FakeResp

from kippy_api import (
    KippyApi,
    KippyAuthError,
    KippyConnectionError,
    KippyGraphQLApi,
    KippyResponseError,
    async_connect,
    graphql,
)

AUTH = {
    "IdToken": "secret-id-token",
    "AccessToken": "secret-access-token",
    "ExpiresIn": 3600,
}


def client(*responses, logged_in=True):
    """Reuse the protocol suite's fake HTTP boundary."""
    session = MagicMock()
    session.post.side_effect = [_CM(_FakeResp(s, json.dumps(b))) for s, b in responses]
    api = KippyGraphQLApi(session)
    if logged_in:
        api._auth = dict(AUTH)
        api._expires_at = float("inf")
        api._credentials = ("secret-email", "secret-password")
    return api, session


async def test_login_and_native_reads():
    """Read every native collection without filtering product types or mutating."""
    pets = [{"id": "pet-uuid", "species": "CAT"}, {"id": "pet-2"}]
    products = [{"id": "tracker-uuid", "entityType": "FUTURE_TRACKER"}]
    gps = {"lastKnownPosition": None, "settings": {"enableGpsOnDefault": False}}
    api, session = client(
        (200, {"AuthenticationResult": AUTH}),
        (200, {"data": {"getPets": {"code": "200", "pets": pets}}}),
        (200, {"data": {"getProducts": {"code": "200", "products": products}}}),
        (200, {"data": {"getPetlinkGps": {"code": "200", "petlinkGps": gps}}}),
        logged_in=False,
    )
    assert await api.login("secret-email", "secret-password") == AUTH
    assert await api.login("secret-email", "secret-password") == AUTH
    assert await api.get_pets() == pets
    assert await api.get_products("pet-uuid") == products
    assert await api.get_petlink_gps("tracker-uuid") == gps
    assert api.backend == "graphql"
    assert api.session is session
    assert not hasattr(api, "close")
    calls = session.post.call_args_list
    assert len(calls) == 4
    assert calls[0].kwargs["json"]["AuthFlow"] == "USER_PASSWORD_AUTH"
    for call in calls:
        assert call.kwargs["ssl"] is True
        assert call.kwargs["timeout"].total == 30
        assert call.kwargs["allow_redirects"] is False
        assert call.kwargs["raise_for_status"] is False
    for call in calls[1:]:
        assert "mutation" not in call.kwargs["json"]["query"]
        assert call.kwargs["headers"]["Authorization"] == "Bearer secret-id-token"
    assert calls[2].kwargs["json"]["variables"] == {"petId": "pet-uuid"}


@pytest.mark.parametrize(
    "status,body,error",
    [
        (400, {"__type": "UserNotFoundException"}, KippyAuthError),
        (400, {"__type": "prefix#NotAuthorizedException"}, KippyAuthError),
        (400, {"__type": "UserNotConfirmedException"}, KippyAuthError),
        (400, {"__type": "PasswordResetRequiredException"}, KippyAuthError),
        (400, {"__type": "TooManyRequestsException"}, KippyResponseError),
        (500, {"__type": "NotAuthorizedException"}, KippyResponseError),
        (429, {}, KippyResponseError),
        (403, {}, KippyResponseError),
        (401, {}, KippyAuthError),
        (302, {}, KippyResponseError),
        (200, {"ChallengeName": "SMS_MFA"}, KippyAuthError),
        (200, {"AuthenticationResult": {"IdToken": "id"}}, KippyResponseError),
        (200, {"AuthenticationResult": []}, KippyResponseError),
        (200, {}, KippyResponseError),
        (200, None, KippyResponseError),
        (400, {"__type": []}, KippyResponseError),
    ],
)
async def test_login_errors(status, body, error, caplog):
    """Distinguish account issues from outages without logging credentials."""
    api, session = client((status, body), logged_in=False)
    caplog.set_level(logging.DEBUG)
    with pytest.raises(error):
        await api.login("secret-email", "secret-password")
    assert api._auth is None
    assert "secret-" not in caplog.text
    assert session.post.call_count == 1


@pytest.mark.parametrize("failure", [TimeoutError, ClientConnectionError])
async def test_mutation_transport_failure_is_not_replayed(failure):
    api, session = client()
    session.post.side_effect = failure("secret upstream error")
    with pytest.raises(KippyConnectionError):
        await api.send_command({"id": "device", "commandType": "WAKEUP"})
    assert session.post.call_count == 1


async def test_401_refresh_is_bounded():
    """Retry at most once on HTTP authentication rejection, preserving payload."""
    api, session = client(
        (401, {}),
        (200, {"AuthenticationResult": AUTH | {"IdToken": "new"}}),
        (401, {}),
    )
    with pytest.raises(KippyAuthError):
        await api.get_pets()
    calls = session.post.call_args_list
    assert len(calls) == 3
    assert calls[0].kwargs["json"] == calls[2].kwargs["json"]
    assert calls[2].kwargs["headers"]["Authorization"] == "Bearer new"


async def test_concurrent_401_refresh_shares_login():
    api, _ = client()
    arrived = 0
    logins = 0
    ready = asyncio.Event()

    async def post(endpoint, payload, headers):
        nonlocal arrived, logins
        if endpoint == graphql.COGNITO_ENDPOINT:
            logins += 1
            await asyncio.sleep(0)
            return {"AuthenticationResult": AUTH | {"IdToken": "new"}}
        if headers["Authorization"] == "Bearer secret-id-token":
            arrived += 1
            if arrived == 2:
                ready.set()
            await ready.wait()
            raise KippyAuthError("Expired", status=401)
        return {"data": {"getPets": {"code": "200", "pets": []}}}

    api._post = post
    assert await asyncio.gather(api.get_pets(), api.get_pets()) == [[], []]
    assert logins == 1


async def test_missing_and_uncached_login():
    api, _ = client(logged_in=False)
    with pytest.raises(KippyAuthError):
        await api.get_pets()
    api, _ = client((401, {}))
    api._credentials = None
    with pytest.raises(KippyAuthError):
        await api.get_pets()
    api, _ = client(
        (200, {"AuthenticationResult": AUTH}),
        (200, {"data": {"getPets": {"code": "200", "pets": []}}}),
        logged_in=False,
    )
    api._credentials = ("email", "password")
    assert await api.get_pets() == []


@pytest.mark.parametrize(
    "body",
    [
        {"errors": [{"errorType": "UnauthorizedException", "message": "secret"}]},
        {"data": {"sendCommand": {"code": "200"}}, "errors": [{"message": "secret"}]},
        {},
        {"data": None},
        {"data": []},
        {"data": {"sendCommand": None}},
    ],
)
async def test_graphql_failure_and_partial_mutations_not_replayed(body, caplog):
    api, session = client((200, body))
    caplog.set_level(logging.DEBUG)
    with pytest.raises(KippyResponseError) as error:
        await api.send_command({"id": "device", "commandType": "WAKEUP"})
    assert session.post.call_count == 1
    assert "secret" not in str(error.value) + caplog.text


@pytest.mark.parametrize("value", [None, {}, [None]])
async def test_collection_shapes(value):
    api, _ = client((200, {"data": {"getPets": {"code": "200", "pets": value}}}))
    with pytest.raises(KippyResponseError):
        await api.get_pets()


async def test_missing_gps_and_activity():
    api, _ = client(
        (200, {"data": {"getPetlinkGps": {"code": "200", "petlinkGps": None}}}),
        (200, {"data": {"getActivitiesCat": {"code": "200", "activityReport": None}}}),
    )
    with pytest.raises(KippyResponseError):
        await api.get_petlink_gps("device")
    with pytest.raises(KippyResponseError):
        await api.get_cat_activity_report(
            "pet", datetime(2026, 1, 1, tzinfo=UTC), datetime(2026, 1, 2, tzinfo=UTC)
        )


async def test_explicit_mutation_inputs_and_result_codes():
    """Do not guess missing setting defaults or treat arbitrary result codes as success."""
    api, session = client(
        (200, {"data": {"sendSetting": {"code": 503, "message": "rejected"}}}),
        (200, {"data": {"appKeepAlive": {"code": "200"}}}),
        (200, {"data": {"sendCommand": {"code": "200"}}}),
        (200, {"data": {"sendSetting": {"code": "200"}}}),
    )
    setting = {
        "deviceId": "device",
        "settingType": "UPDATE_FREQUENCY",
        "operationType": "UPDATE",
        "updateObject": '{"enableGpsOnDefault":false}',
    }
    with pytest.raises(KippyResponseError) as caught:
        await api.send_setting(setting)
    assert caught.value.return_code == 503
    assert session.post.call_args.kwargs["json"]["variables"] == {"setting": setting}
    assert await api.app_keep_alive(["device"]) == {"code": "200"}
    assert session.post.call_args.kwargs["json"]["variables"] == {
        "productIds": ["device"]
    }
    command = {
        "id": "device",
        "commandType": "LIVE_TRACKING",
        "duration": 0,
        "modeType": "SENTINEL",
    }
    assert await api.send_command(command) == {"code": "200"}
    assert session.post.call_args.kwargs["json"]["variables"] == {"command": command}

    # The native schema also permits settings addressed by their own ID.
    setting_by_id = {"id": "zone", "settingType": "GEOFENCE", "operationType": "DELETE"}
    assert await api.send_setting(setting_by_id) == {"code": "200"}
    assert session.post.call_args.kwargs["json"]["variables"] == {
        "setting": setting_by_id
    }


async def test_inputs_fail_before_http():
    api, session = client()
    for call in (
        api.get_products(""),
        api.get_petlink_gps(None),
        api.send_command({}),
        api.send_setting({}),
        api.app_keep_alive([]),
        api.app_keep_alive([""]),
    ):
        with pytest.raises(ValueError):
            await call
    session.post.assert_not_called()


async def test_dates_iso_week_and_report_semantics():
    """Preserve timestamps and report fields, including ISO year and UTC history."""
    report = {"onTheMove": {"value": 123}, "walk": None}
    api, session = client(
        (
            200,
            {"data": {"getActivitiesCat": {"code": "200", "activityReport": report}}},
        ),
        (200, {"data": {"getPositionsHistory": {"code": "200", "positions": []}}}),
    )
    tz = ZoneInfo("Asia/Kathmandu")
    start, end = datetime(2020, 12, 31, tzinfo=tz), datetime(2021, 1, 1, tzinfo=tz)
    assert await api.get_cat_activity_report("pet", start, end) == report
    assert session.post.call_args.kwargs["json"]["variables"] == {
        "petId": "pet",
        "weekIndex": 202053,
        "from": int(start.timestamp()),
        "to": int(end.timestamp()),
    }
    assert await api.get_positions_history("pet", start, end) == []
    variables = session.post.call_args.kwargs["json"]["variables"]
    assert variables["from"] == "2020-12-30T18:15:00.000Z"
    assert variables["to"] == "2020-12-31T18:15:00.000Z"
    for a, b in (
        (end, start),
        (start.replace(tzinfo=None), end),
        (start, datetime(2021, 1, 5, tzinfo=tz)),
    ):
        with pytest.raises(ValueError):
            await api.get_cat_activity_report("pet", a, b)
    with pytest.raises(ValueError):
        await api.get_positions_history("pet", end, start)
    assert session.post.call_count == 2


@pytest.mark.parametrize("backend", ["auto", "legacy", "graphql"])
async def test_explicit_and_automatic_selection(backend):
    """Prefer modern even when migrated accounts could still log into legacy."""
    legacy = MagicMock(spec=KippyApi)
    legacy.login = AsyncMock()
    modern = MagicMock(spec=KippyGraphQLApi)
    modern.login = AsyncMock()
    with (
        patch(
            "kippy_api.client.KippyApi.async_create", AsyncMock(return_value=legacy)
        ) as create,
        patch("kippy_api.client.KippyGraphQLApi", return_value=modern),
    ):
        result = await async_connect(MagicMock(), "email", "password", backend=backend)
    assert result is (legacy if backend == "legacy" else modern)
    if backend == "legacy":
        modern.login.assert_not_called()
    else:
        create.assert_not_called()


@pytest.mark.parametrize(
    "error,fallback",
    [
        (KippyAuthError("missing", return_code="UserNotFoundException"), True),
        (KippyAuthError("credentials", return_code="NotAuthorizedException"), True),
        (KippyAuthError("unconfirmed", return_code="UserNotConfirmedException"), False),
        (KippyAuthError("reset", return_code="PasswordResetRequiredException"), False),
        (KippyAuthError("challenge"), False),
        (KippyAuthError("auth", status=401), False),
        (KippyConnectionError("offline"), False),
        (KippyResponseError("throttled", status=429), False),
    ],
)
async def test_detection_only_falls_back_on_account_rejection(error, fallback):
    legacy = MagicMock(spec=KippyApi)
    legacy.login = AsyncMock()
    modern = MagicMock(spec=KippyGraphQLApi)
    modern.login = AsyncMock(side_effect=error)
    with (
        patch("kippy_api.client.KippyApi.async_create", AsyncMock(return_value=legacy)),
        patch("kippy_api.client.KippyGraphQLApi", return_value=modern),
    ):
        if fallback:
            assert await async_connect(MagicMock(), "email", "password") is legacy
        else:
            with pytest.raises(type(error)):
                await async_connect(MagicMock(), "email", "password")
            legacy.login.assert_not_called()
        with pytest.raises(type(error)):
            await async_connect(MagicMock(), "email", "password", backend="graphql")
    with pytest.raises(ValueError):
        await async_connect(MagicMock(), "email", "password", backend="bad")


async def test_real_http_boundary(monkeypatch):
    """Exercise aiohttp JSON serialization and content types with a local server."""

    async def handler(request):
        body = await request.json()
        if request.path == "/login":
            assert body["AuthFlow"] == "USER_PASSWORD_AUTH"
            return web.Response(
                text=json.dumps({"AuthenticationResult": AUTH}),
                content_type="application/x-amz-json-1.1",
            )
        assert request.headers["Authorization"] == "Bearer secret-id-token"
        assert body["variables"] == {}
        return web.json_response({"data": {"getPets": {"code": "200", "pets": []}}})

    app = web.Application()
    app.router.add_post("/{path}", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    try:
        root = f"http://127.0.0.1:{runner.addresses[0][1]}"
        monkeypatch.setattr(graphql, "COGNITO_ENDPOINT", root + "/login")
        monkeypatch.setattr(graphql, "APPSYNC_ENDPOINT", root + "/graphql")
        async with ClientSession(raise_for_status=True) as session:
            api = KippyGraphQLApi(session)
            await api.login("email", "password")
            assert await api.get_pets() == []
            assert not session.closed
    finally:
        await runner.cleanup()


async def test_migration_flag_and_other_activity_methods():
    report = {"walk": {"value": 5, "goal": 10}}
    api, session = client(
        (
            200,
            {
                "data": {
                    "getUser": {"code": "200", "user": {"id": "user", "migrated": True}}
                }
            },
        ),
        (200, {"data": {"getActivities": {"code": "200", "activityReport": report}}}),
        (200, {"data": {"getActivitiesByHour": {"code": "200", "activities": []}}}),
    )
    assert (await api.get_user())["migrated"] is True
    start, end = datetime(2026, 9, 20, tzinfo=UTC), datetime(2026, 9, 21, tzinfo=UTC)
    assert await api.get_activity_report("pet", start, end) == report
    assert session.post.call_args.kwargs["json"]["variables"]["from"] == int(
        start.timestamp()
    )
    assert await api.get_activities_by_hour("pet", start, end) == []


@pytest.mark.parametrize("code", [None, "404", "500", [], True])
async def test_vendor_result_codes_are_not_empty_success(code):
    api, session = client((200, {"data": {"getPets": {"code": code, "pets": []}}}))
    with pytest.raises(KippyResponseError):
        await api.get_pets()
    assert session.post.call_count == 1


async def test_missing_account_and_general_report():
    api, _ = client(
        (200, {"data": {"getUser": {"code": "200", "user": None}}}),
        (200, {"data": {"getActivities": {"code": "200", "activityReport": None}}}),
    )
    with pytest.raises(KippyResponseError):
        await api.get_user()
    with pytest.raises(KippyResponseError):
        await api.get_activity_report(
            "pet", datetime(2026, 1, 1, tzinfo=UTC), datetime(2026, 1, 2, tzinfo=UTC)
        )


@pytest.mark.parametrize("lifetime", [None, 0, -1, True, "3600"])
async def test_invalid_token_lifetime(lifetime):
    api, _ = client(
        (200, {"AuthenticationResult": AUTH | {"ExpiresIn": lifetime}}), logged_in=False
    )
    with pytest.raises(KippyResponseError):
        await api.login("email", "password")


async def test_proactive_renewal_uses_cognito_lifetime():
    api, session = client(
        (200, {"AuthenticationResult": AUTH}),
        (200, {"AuthenticationResult": AUTH | {"IdToken": "renewed"}}),
        (200, {"data": {"getPets": {"code": "200", "pets": []}}}),
        logged_in=False,
    )
    with patch("kippy_api.graphql.monotonic", return_value=100):
        await api.login("email", "password")
        assert api._expires_at == 3670
    with patch("kippy_api.graphql.monotonic", return_value=3670):
        assert await api.get_pets() == []
    assert session.post.call_args_list[1].args[0] == graphql.COGNITO_ENDPOINT
    assert session.post.call_args.kwargs["headers"]["Authorization"] == "Bearer renewed"


async def test_force_login_refreshes_valid_tokens():
    api, session = client((200, {"AuthenticationResult": AUTH | {"IdToken": "forced"}}))
    assert (await api.login("email", "password", force=True))["IdToken"] == "forced"
    assert session.post.call_count == 1
