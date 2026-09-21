"""Errors returned by the Kippy client."""


class KippyError(Exception):
    """Base error with optional HTTP and API result metadata."""

    def __init__(
        self,
        message: str,
        *,
        status: int | None = None,
        return_code: int | bool | str | None = None,
    ) -> None:
        """Store safe error metadata without retaining response bodies."""
        super().__init__(message)
        self.status = status
        self.return_code = return_code


class KippyAuthError(KippyError):
    """Credentials are missing, invalid, or expired."""


class KippyConnectionError(KippyError):
    """A network request failed or timed out."""


class KippyResponseError(KippyError):
    """The server rejected the request or returned an invalid response."""
