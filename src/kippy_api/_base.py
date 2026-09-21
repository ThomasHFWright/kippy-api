"""HTTP requests and authentication for the Kippy API."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import ssl
from collections.abc import Mapping
from typing import Any, Self

from aiohttp import ClientError, ClientSession, ClientTimeout

from ._utils import (
    _decode_json,
    _get_return_code,
    _return_code_error,
    _treat_401_as_success,
)
from .const import (
    APP_IDENTITY,
    APP_IDENTITY_EVO,
    APP_VERSION,
    DEFAULT_HOST,
    DEVICE_NAME,
    LOGIN_PATH,
    PHONE_COUNTRY_CODE,
    PLATFORM_DEVICE,
    REQUEST_HEADERS,
    RETURN_VALUES,
    TIMEZONE,
    TOKEN_DEVICE,
)
from .exceptions import KippyAuthError, KippyConnectionError, KippyResponseError

_LOGGER = logging.getLogger(__name__)
_REQUEST_TIMEOUT = ClientTimeout(total=30)


class BaseKippyApi:
    """API wrapper using a session owned and closed by the caller."""

    def __init__(
        self,
        session: ClientSession,
        host: str = DEFAULT_HOST,
        ssl_context: ssl.SSLContext | None = None,
    ) -> None:
        """Initialize the API client without creating network resources."""
        self._session = session
        self._host = host.rstrip("/")
        self._auth: dict[str, Any] | None = None
        self._credentials: tuple[str, str] | None = None
        self._ssl_context = ssl_context
        self._login_lock = asyncio.Lock()

    @classmethod
    async def async_create(
        cls, session: ClientSession, host: str = DEFAULT_HOST
    ) -> Self:
        """Create a client retaining the original endpoint TLS compatibility."""
        ctx = await asyncio.to_thread(ssl.create_default_context)
        # Keep certificate/hostname verification; retain the existing cipher policy
        # until the vendor endpoint can be validated without this workaround.
        ctx.set_ciphers("DEFAULT@SECLEVEL=1")
        if hasattr(ssl, "OP_LEGACY_SERVER_CONNECT"):
            ctx.options |= ssl.OP_LEGACY_SERVER_CONNECT
        return cls(session, host, ctx)

    @property
    def session(self) -> ClientSession:
        """Return the session owned by the caller."""
        return self._session

    @property
    def app_code(self) -> str | None:
        """Return the cached app code."""
        return self._auth.get("app_code") if self._auth else None

    @property
    def app_verification_code(self) -> str | None:
        """Return the cached app verification code."""
        return self._auth.get("app_verification_code") if self._auth else None

    async def login(
        self, email: str, password: str, force: bool = False
    ) -> dict[str, Any]:
        """Authenticate and cache tokens, serializing concurrent logins."""
        async with self._login_lock:
            if not force and self._auth is not None:
                return self._auth
            self._auth = None
            data = await self._post(
                LOGIN_PATH,
                {
                    "login_email": email,
                    "login_password_hash": hashlib.sha256(
                        password.encode()
                    ).hexdigest(),
                    "login_password_hash_md5": hashlib.md5(
                        password.encode()
                    ).hexdigest(),
                    "app_identity": APP_IDENTITY,
                    "app_identity_evo": APP_IDENTITY_EVO,
                    "platform_device": PLATFORM_DEVICE,
                    "app_version": APP_VERSION,
                    "timezone": TIMEZONE,
                    "phone_country_code": PHONE_COUNTRY_CODE,
                    "token_device": TOKEN_DEVICE,
                    "device_name": DEVICE_NAME,
                },
                REQUEST_HEADERS,
            )
            if not all(data.get(key) for key in ("app_code", "app_verification_code")):
                raise KippyResponseError(
                    "Login response is missing authentication tokens"
                )
            self._auth = data
            self._credentials = (email, password)
            return data

    async def ensure_login(self) -> None:
        """Authenticate if cached tokens are unavailable."""
        if self._auth is not None:
            return
        if self._credentials is None:
            raise KippyAuthError("No credentials available")
        await self.login(*self._credentials)

    def cache_authentication(
        self, auth: Mapping[str, Any], *, credentials: tuple[str, str] | None = None
    ) -> None:
        """Seed authentication for callers restoring a cached session."""
        self._auth = dict(auth)
        if credentials is not None:
            self._credentials = credentials

    async def _authenticated_payload(
        self,
        *,
        identity: str | None = APP_IDENTITY,
        extra: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Build an endpoint payload using cached authentication."""
        await self.ensure_login()
        if not self.app_code or not self.app_verification_code:
            raise KippyAuthError("No authentication tokens available")
        payload = {
            "app_code": self.app_code,
            "app_verification_code": self.app_verification_code,
        }
        if identity is not None:
            payload["app_identity"] = identity
        if extra:
            payload.update(extra)
        return payload

    async def _post(
        self, path: str, payload: dict[str, Any], headers: dict[str, str]
    ) -> dict[str, Any]:
        """Send one bounded request and classify errors without logging payloads."""
        try:
            async with self._session.post(
                f"{self._host}{path}",
                data=json.dumps(payload),
                headers=headers,
                ssl=self._ssl_context or True,
                timeout=_REQUEST_TIMEOUT,
                allow_redirects=False,
                raise_for_status=False,
            ) as response:
                text = await response.text()
                status = response.status
        except (ClientError, TimeoutError) as err:
            raise KippyConnectionError("Request failed or timed out") from err

        _LOGGER.debug("API response status=%s", status)
        data = _decode_json(text)
        code = _get_return_code(data)
        success = data is not None and _treat_401_as_success(path, data)
        if status == 401 and success and data is not None:
            return data
        # Explicit non-authentication result codes take precedence over the
        # vendor's unreliable HTTP 401 status (e.g. inactive subscriptions).
        auth_error = code in (
            RETURN_VALUES.AUTHORIZATION_EXPIRED,
            RETURN_VALUES.INVALID_CREDENTIALS,
        )
        if (200 <= status < 300 or status in (401, 403)) and (
            (status in (401, 403) and code is None)
            or auth_error
            or (path == LOGIN_PATH and code is False)
        ):
            raise KippyAuthError(
                "Authentication failed", status=status, return_code=code
            )
        if not 200 <= status < 300:
            raise KippyResponseError(
                "Server rejected request", status=status, return_code=code
            )
        if data is None:
            raise KippyResponseError("Response is not a JSON object", status=status)
        if not success:
            raise KippyResponseError(
                _return_code_error(code), status=status, return_code=code
            )
        return data

    async def post_with_refresh(
        self, path: str, payload: dict[str, Any], headers: dict[str, str]
    ) -> dict[str, Any]:
        """Retry once only after a confirmed authentication failure."""
        auth = self._auth
        try:
            return await self._post(path, payload, headers)
        except KippyAuthError:
            if self._credentials is None:
                raise
            # Invalidate only the tokens used by this request. Concurrent expired
            # requests share the login lock and reuse the first refreshed session.
            if self._auth is auth:
                self._auth = None
            await self.ensure_login()
        payload.update(
            app_code=self.app_code,
            app_verification_code=self.app_verification_code,
        )
        return await self._post(path, payload, headers)
