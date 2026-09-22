"""Concrete API client composed of endpoint mixins."""

from __future__ import annotations

from typing import Literal

from aiohttp import ClientSession

from .activity import ActivityEndpoint
from .exceptions import KippyAuthError
from .graphql import KippyGraphQLApi
from .kippymap import KippyMapEndpoint
from .pets import PetsEndpoint
from .settings import SettingsEndpoint

__all__ = ["KippyApi", "async_connect"]


class KippyApi(
    ActivityEndpoint,
    SettingsEndpoint,
    KippyMapEndpoint,
    PetsEndpoint,
):
    """Full-featured Kippy API client used by the integration."""

    backend = "legacy"


async def async_connect(
    session: ClientSession,
    email: str,
    password: str,
    *,
    backend: Literal["auto", "legacy", "graphql"] = "auto",
) -> KippyApi | KippyGraphQLApi:
    """Prefer the new app; use legacy only after a definite account rejection.

    Migrated accounts can still log into the old service with different pet IDs,
    so legacy-first selection is unreliable. Auto tries Cognito first and only
    falls back on UserNotFoundException or NotAuthorizedException. Outages, MFA,
    unconfirmed accounts and empty data never trigger switching. The returned
    client stays on its backend, including during refresh. Explicit selection
    lets integrations preserve existing IDs until their registry migration runs.
    """
    if backend not in ("auto", "legacy", "graphql"):
        raise ValueError("backend must be auto, legacy or graphql")
    if backend != "legacy":
        modern = KippyGraphQLApi(session)
        try:
            await modern.login(email, password)
        except KippyAuthError as err:
            if backend == "graphql" or err.return_code not in (
                "UserNotFoundException",
                "NotAuthorizedException",
            ):
                raise
        else:
            return modern
    legacy = await KippyApi.async_create(session)
    await legacy.login(email, password)
    return legacy
