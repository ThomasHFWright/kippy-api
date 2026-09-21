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
editable in its environment. Do not release an integration dependency on `0.1.0`
until that version is published and its installation from PyPI is verified.

## Publishing

The workflow does not create a PyPI project or reserve the package name.
Before your first release:

1. Push the reviewed changes to `ThomasHFWright/kippy-api` on GitHub.
2. Configure a PyPI pending trusted publisher for project `kippy-api`, owner
   `ThomasHFWright`, repository `kippy-api`, workflow `publish.yml`, and environment
   `pypi`. The name is proposed and is not reserved by this repository.
3. Create the GitHub `pypi` environment with any desired protection rules.
4. Set the version in `pyproject.toml`, refresh `uv.lock`, and tag the reviewed
   commit `v0.1.0` (or the corresponding version).

The tag workflow verifies version consistency, runs the same tests/build/install
checks, and passes the resulting distributions to a separate job that uses PyPI
Trusted Publishing. Only the publishing job can request an OIDC token. No API
key belongs in an environment file or repository.

Original source copyright (c) 2025 Thomas Wright, MIT license.
