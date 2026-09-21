# Kippy API

An asynchronous Python client for Kippy pet trackers, extracted from
[the Kippy Home Assistant integration](https://github.com/ThomasHFWright/kippy-homeassistant).
Python 3.13 or newer is required. `aiohttp` is the only runtime dependency.

## Use

```python
import asyncio
import os
from zoneinfo import ZoneInfo

from aiohttp import ClientSession
from kippy_api import KippyApi


async def main():
    async with ClientSession() as session:
        api = await KippyApi.async_create(session)
        await api.login(os.environ["KIPPY_EMAIL"], os.environ["KIPPY_PASSWORD"])
        pets = await api.get_pet_kippy_list()
        # Pass the reporting timezone explicitly; the machine timezone is ignored.
        if pets:
            await api.get_activity_categories(
                pets[0]["petID"],
                "2026-09-01",
                "2026-09-02",
                2,
                1,
                timezone=ZoneInfo("Europe/London"),
            )


asyncio.run(main())
```

You own the session and close it. The client never closes or replaces it. Existing
endpoint names and dictionary results are preserved: `get_pet_kippy_list`,
`kippymap_action`, `modify_kippy_settings`, and `get_activity_categories`.
Use `do_sms=False` for cached map reads; device commands can change your tracker.

Catch `KippyAuthError` for missing/invalid credentials or exhausted authentication
refresh, `KippyConnectionError` for transport failures, and `KippyResponseError`
for rejected or malformed responses. All inherit `KippyError` and expose optional
`status` and `return_code` metadata. Requests time out after 30 seconds and only
confirmed authentication failures receive one retry after login. Network timeouts
never retry commands. Concurrent token expiry shares a login refresh.

The client preserves known successful HTTP 401 bodies and the vendor's existing
TLS cipher compatibility setting, with certificate and hostname checks enabled.
It logs response status only, never account or location payloads. API result
metadata may be vendor supplied; avoid logging whole response objects.

The package has no Home Assistant imports, polling, entities, translation loading,
or credential file handling. See [the protocol reference](https://github.com/ThomasHFWright/kippyAPIs)
for the reverse-engineered API.

## New app and automatic selection

The original `KippyApi` continues to use the legacy PHP API. For new-app accounts,
use `KippyGraphQLApi`, which exposes native Cognito/AppSync operations. To detect
the account backend:

```python
from kippy_api import KippyGraphQLApi, async_connect

api = await async_connect(session, email, password)  # backend="auto"
if isinstance(api, KippyGraphQLApi):
    pets = await api.get_pets()
    for pet in pets:
        products = await api.get_products(pet["id"])
else:
    pets = await api.get_pet_kippy_list()
```

Auto tries GraphQL first and falls back to legacy only on a definite account
rejection. Migrated accounts may still authenticate to legacy with different IDs.
Use `backend="legacy"` or `backend="graphql"` to select explicitly. Outages, MFA,
account confirmation/reset requirements and empty results never trigger switching.

The new client provides pets/products, cached GPS/status/settings, account migration
status, general and cat activity reports, hourly activity and position history.
All date arguments are timezone-aware datetimes. Device commands, keep-alives and
settings writes are separate explicit methods; reads never send them. GraphQL and
application-level errors raise `KippyResponseError` instead of becoming empty data.

These are native methods, not a drop-in replacement for the legacy integration.
See [validation and migration notes](docs/new-api.md) for tested behavior, method
names, the reference implementation and the Home Assistant migration boundary.

## Development

```sh
uv sync --frozen --group dev
uv run ruff check .
uv run ruff format --check .
uv run mypy
uv run pytest
uv run python -m build
uv run twine check dist/*
```

Tests use synthetic responses and a local HTTP server; they do not require
credentials or contact Kippy. CI also installs the built wheel into an environment
without Home Assistant. `uv.lock` pins development tooling; the library dependency
range remains compatible with the consuming application's aiohttp version.

For development with the adjacent Home Assistant integration, install this folder
editable in its environment.

Original source copyright (c) 2025 Thomas Wright, MIT license.
